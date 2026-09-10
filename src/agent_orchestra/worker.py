"""Execute one durable, bounded local review step."""

from __future__ import annotations

import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Never, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from agent_orchestra.adapter.registry import (
    DEFAULT_RUNTIME_REGISTRY,
    RuntimeRegistry,
    RuntimeRegistryError,
    RuntimeRole,
)
from agent_orchestra.agents import (
    CommandAgentAdapter,
    DeveloperRequest,
    ReviewerRequest,
)
from agent_orchestra.evidence import (
    EvidencePathError,
    EvidenceType,
    JobEvidence,
    evidence_root_for_job,
    resolve_evidence_path,
)
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptStatus,
    InvocationEvidenceError,
    InvocationEvidenceStore,
    InvocationIdentity,
    InvocationRecord,
    RecoveryAction,
    recovery_action,
    timestamp,
    transition_attempt,
)
from agent_orchestra.manifests import canonical_message_evidence, evidence_path
from agent_orchestra.models import Run, RunState, same_diff_digest, utc_now
from agent_orchestra.review_batch import ReviewerDecision, aggregate_review_batch
from agent_orchestra.review_fanout import ReviewerDispatch, build_review_fanout
from agent_orchestra.reviewer_paths import (
    ReviewerIdentityError,
    reviewer_invocation_id,
    reviewer_invocation_stem,
    reviewer_task_id,
    validate_reviewer_id,
)
from agent_orchestra.reviewer_plan import reviewer_execution_plan_record
from agent_orchestra.schemas import (
    CHANGES_REQUESTED_WITHOUT_FINDINGS,
    DUPLICATE_REVIEW_FINDING_IDS,
    EXECUTION_RECORD_ADAPTER,
    DeveloperHandoffMessageSchema,
    ExecutionRecord,
    ExecutionRecordSchema,
    RemediationRequestMessageSchema,
    ReviewerBatchResultSchema,
    ReviewerSetExecutionRecordSchema,
    ReviewRequestMessageSchema,
    ReviewResultMessageSchema,
)
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agent_orchestra.reviewer_plan import ReviewerExecutionPlan
    from agent_orchestra.store import JobStore


class WorkerError(RuntimeError):
    """Raised when a queued run cannot complete its review step."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Create an error with an optional stable machine-readable code."""

        super().__init__(message)
        self.code = code


NOT_OBJECT = 'reviewer response must be a JSON object'
INVALID_ENVELOPE = 'reviewer response has missing or unknown envelope fields'
INVALID_IDENTITY = 'reviewer response has an invalid identity or timestamp'
INVALID_PAYLOAD = 'reviewer response has invalid payload fields'
INVALID_VERDICT = 'reviewer response has invalid verdict'
INVALID_ARTIFACT_PATH = 'reviewer response has invalid artifact_path'
MISSING_ARTIFACT = 'reviewer did not create the requested artifact'
APPROVED_WITH_FINDINGS = 'approved review cannot contain findings'
EMPTY_OBJECTIVE = 'objective must not be empty'
EMPTY_COMMAND = 'reviewer command must not be empty'
NO_CHANGES = 'worktree has no local changes'
NO_REMEDIATION_CHANGE = 'developer handoff did not produce a new diff digest'
DEVELOPER_DISAGREEMENT = 'developer disputed every finding without changing the diff'
ITERATION_LIMIT = 'maximum review iteration count exhausted'
INVALID_DEVELOPER_TIMEOUT = 'developer timeout must be positive'
INVALID_ITERATION_LIMIT = 'maximum review iterations must be positive'
WORKTREE_CHANGED = 'worktree changed during read-only review'
EVIDENCE_INSIDE_WORKTREE = 'run evidence directory must be outside the worktree'
INVALID_DEVELOPER_HANDOFF = 'developer handoff is invalid'
INVALID_FINDING_DISPOSITIONS = (
    'developer handoff must contain exactly one disposition for every finding'
)
INVALID_REMEDIATION_REQUEST = 'remediation request is invalid'
REMEDIATION_PATH_ESCAPE = 'remediation request references evidence outside the run'
REMEDIATION_ACTIONS = 'remediation request must not authorize lifecycle actions'
INVALID_REVIEW_REQUEST = 'review request is invalid'
REVIEW_PATH_ESCAPE = 'review request references evidence outside the run'
DUPLICATE_MESSAGE_ID = 'message ID was already persisted for this run'
MIXED_REVIEWER_MESSAGE_PATHS = 'canonical messages mix reviewer batch and legacy paths'
INCOMPLETE_REVIEWER_MESSAGE_BATCH = 'reviewer message sequence is not complete'
SMALL_REVIEWER_MESSAGE_BATCH = 'reviewer message batch requires at least two reviewers'
REVIEWER_BATCH_INCOMPLETE = 'reviewer batch did not complete'
REVIEWER_BATCH_INCOMPLETE_CODE = 'reviewer_batch_incomplete'
RUN_NOT_RESUMABLE_CODE = 'run_not_resumable'
RESUME_METADATA_UNSUPPORTED_CODE = 'resume_metadata_unsupported'
RESUME_REVIEWER_SET_UNSUPPORTED_CODE = 'resume_reviewer_set_unsupported'
RESUME_SCOPE_CHANGED_CODE = 'resume_scope_changed'
RESUME_INTERRUPTED_CODE = 'resume_interrupted'
RESUME_EXECUTION_FAILED_CODE = 'resume_execution_failed'
RESUME_ACTIVATION_UNCERTAIN_CODE = 'resume_activation_uncertain'
RESUME_CANCELLED_CODE = 'resume_cancelled'


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """
    Caller-supplied collaborators invariant across one worker invocation.

    These four values are identical on the run and the resume path and never
    change as a workflow advances, so they travel as one collaborator rather
    than as four parameters through every function in the chain. Adding a
    cross-cutting collaborator becomes a field here instead of a signature edit
    in nine places, which is what #38 had to do for the runtime registry.
    """

    store: JobStore
    runs_directory: Path
    digest_worktree: Callable[[Path, str], str | None]
    registry: RuntimeRegistry


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """
    One workflow's objective, commands, limits, and agent identities.

    The run path takes these from the caller while the resume path rebuilds
    them from the durable execution record, so they are workflow state rather
    than caller configuration and are kept apart from WorkerContext.
    """

    objective: str
    reviewer_command: Sequence[str]
    developer_command: Sequence[str]
    timeout_seconds: int
    developer_timeout_seconds: int | None
    max_iterations: int
    reviewer_identity: InvocationIdentity
    developer_identity: InvocationIdentity

    @classmethod
    def from_execution_record(
        cls,
        execution: ExecutionRecordSchema,
        *,
        reviewer_identity: InvocationIdentity,
        developer_identity: InvocationIdentity,
    ) -> ReviewPlan:
        """
        Rebuild the plan a stopped run was executing from its own evidence.

        The identities are supplied rather than read here because the resume
        path resolves them against the registry only for runs that are not
        approved, and that condition belongs with the caller that knows the
        run state.
        """

        return cls(
            objective=execution.objective,
            reviewer_command=execution.reviewer.command,
            developer_command=execution.developer.command,
            timeout_seconds=execution.reviewer.timeout_seconds,
            developer_timeout_seconds=execution.developer.timeout_seconds,
            max_iterations=execution.max_review_iterations,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
        )


@dataclass(frozen=True, slots=True)
class ReviewerSetReviewPlan:
    """One workflow's immutable reviewer batch and developer configuration."""

    objective: str
    reviewer_plan: ReviewerExecutionPlan
    developer_command: Sequence[str]
    developer_timeout_seconds: int
    max_iterations: int
    developer_identity: InvocationIdentity


@dataclass(frozen=True, slots=True)
class ReviewerDispatchResult:
    """One completed reviewer dispatch and its validated protocol response."""

    decision: ReviewerDecision
    message_id: str | None


def _require_unique_batch_message_ids(
    results: tuple[ReviewerDispatchResult, ...],
) -> None:
    """Reject a reviewer batch that reused a canonical response identity."""

    message_ids = [result.message_id for result in results if result.message_id]
    if len(message_ids) != len(set(message_ids)):
        raise WorkerError(DUPLICATE_MESSAGE_ID)


def _execution_record(
    plan: ReviewPlan | ReviewerSetReviewPlan,
    *,
    run_id: str,
    created_at: str,
) -> ExecutionRecord:
    """Build one strict versioned execution record from a worker plan."""

    common: dict[str, Any] = {
        'run_id': run_id,
        'objective': plan.objective,
        'developer': {
            'command': list(plan.developer_command),
            'identity': {
                'vendor': plan.developer_identity.vendor,
                'model': plan.developer_identity.model,
                'runtime': plan.developer_identity.runtime,
            },
            'timeout_seconds': plan.developer_timeout_seconds,
        },
        'max_review_iterations': plan.max_iterations,
        'created_at': created_at,
    }
    if isinstance(plan, ReviewerSetReviewPlan):
        return ReviewerSetExecutionRecordSchema.model_validate(
            {
                **common,
                'schema_version': 3,
                'reviewer_plan': reviewer_execution_plan_record(plan.reviewer_plan),
            }
        )
    return ExecutionRecordSchema.model_validate(
        {
            **common,
            'schema_version': 2,
            'reviewer': {
                'command': list(plan.reviewer_command),
                'identity': {
                    'vendor': plan.reviewer_identity.vendor,
                    'model': plan.reviewer_identity.model,
                    'runtime': plan.reviewer_identity.runtime,
                },
                'timeout_seconds': plan.timeout_seconds,
            },
        }
    )


def _run_evidence_directory(runs_directory: Path, run_id: str) -> Path:
    """Resolve one run directory while preserving the worker error contract."""

    try:
        path = resolve_evidence_path(runs_directory, run_id)
        path.mkdir(parents=True, exist_ok=True)
        JobEvidence(runs_directory, run_id).recover_index()
        InvocationEvidenceStore(path).recover_completed(run_id)
        return path
    except EvidencePathError as error:
        raise WorkerError(str(error)) from error


def _run_evidence_path(run_directory: Path, *parts: str) -> Path:
    """Resolve one contained path beneath an established run directory."""

    try:
        return resolve_evidence_path(
            evidence_root_for_job(run_directory), run_directory.name, *parts
        )
    except EvidencePathError as error:
        raise WorkerError(str(error)) from error


def _manifest_evidence_path(
    run_directory: Path, evidence_type: str, ordinal: int
) -> Path:
    """Resolve one manifest-rendered path through the run boundary."""

    return _run_evidence_path(
        run_directory, *Path(evidence_path(evidence_type, ordinal=ordinal)).parts
    )


def _contained_job_reference(
    run_directory: Path, value: str | Path, error_message: str
) -> Path:
    """Resolve an evidence reference through the selected run boundary."""

    candidate = Path(value)
    try:
        relative = candidate.relative_to(run_directory)
        return _run_evidence_path(run_directory, *relative.parts)
    except (ValueError, WorkerError) as error:
        raise WorkerError(error_message) from error


def _require_unchanged(actual: str | None, expected: str) -> None:
    """Reject a review when its worktree digest changed during execution."""

    if not same_diff_digest(actual, expected):
        raise WorkerError(WORKTREE_CHANGED)


def _digest(
    digest_worktree: Callable[[Path, str], str | None], worktree: Path, base_sha: str
) -> str | None:
    """Normalize filesystem and Git digest failures as worker errors."""

    try:
        return digest_worktree(worktree, base_sha)
    except (OSError, RuntimeError) as error:
        raise WorkerError(f'cannot compute worktree digest: {error}') from error


def _write_json_atomic(
    path: Path, document: dict[str, Any], evidence_type: EvidenceType
) -> None:
    """Write one UTF-8 JSON document atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        _finalize_temporary_path(temporary, path, evidence_type)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text_atomic(
    path: Path, content: str, *, evidence_type: EvidenceType | None = None
) -> None:
    """Write one UTF-8 text artifact atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if evidence_type is not None:
            _finalize_temporary_path(temporary, path, evidence_type)
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _archive_unaccepted_response(
    path: Path, destination: Path, evidence_type: EvidenceType
) -> None:
    """Preserve a partial response so a retry cannot consume stale output."""

    if path.exists():
        structural = {'messages', 'artifacts', 'logs', 'invocations'}
        job_directory = (
            destination.parent.parent
            if destination.parent.name in structural
            else destination.parent
        )
        JobEvidence.for_directory(job_directory).relocate_finalized(
            path, destination, evidence_type
        )


def _record_finalized_path(path: Path, evidence_type: EvidenceType) -> None:
    """Record one finalized worker artifact in its owning job index."""

    structural = {'messages', 'artifacts', 'logs', 'invocations', 'review-batches'}
    job_directory = (
        path.parent.parent if path.parent.name in structural else path.parent
    )
    JobEvidence.for_directory(job_directory).record_finalized(path, evidence_type)


def _finalize_temporary_path(
    temporary: Path, path: Path, evidence_type: EvidenceType
) -> None:
    """Publish one worker file through the recoverable evidence protocol."""

    structural = {'messages', 'artifacts', 'logs', 'invocations', 'review-batches'}
    job_directory = (
        path.parent.parent if path.parent.name in structural else path.parent
    )
    JobEvidence.for_directory(job_directory).finalize_write(
        temporary, path, evidence_type
    )


def _output_text(value: str | bytes | None) -> str:
    """Normalize captured subprocess output for durable UTF-8 logs."""

    if value is None:
        return ''
    return value.decode(errors='replace') if isinstance(value, bytes) else value


def _exception_runtime_metadata(
    error: BaseException,
) -> tuple[tuple[str, ...], Literal['reported', 'unavailable']]:
    """Return validated provenance preserved by a failed command adapter."""

    models = getattr(error, 'effective_models', ())
    status = getattr(error, 'effective_model_status', 'unavailable')
    if (
        isinstance(models, tuple)
        and all(isinstance(model, str) and model for model in models)
        and len(models) == len(set(models))
        and status in {'reported', 'unavailable'}
        and (status == 'reported') == bool(models)
    ):
        return tuple(models), cast('Literal["reported", "unavailable"]', status)
    return (), 'unavailable'


def _runtime_metadata_path(
    identity: InvocationIdentity,
    path: Path,
    registry: RuntimeRegistry,
) -> Path | None:
    """Return the sidecar path only for runtimes that report provenance."""

    try:
        runtime = registry.require(identity.runtime)
    except ValueError:
        return None
    return path if runtime.reports_runtime_metadata else None


def _invocation_stem(sequence: int, role: str, attempt: int) -> str:
    """Return the stable evidence stem for one invocation attempt."""

    retry_suffix = '' if attempt == 1 else f'-attempt-{attempt:04d}'
    return f'{sequence:06d}-{role}{retry_suffix}'


def _persist_attempt_record(path: Path, record: InvocationRecord) -> None:
    """Normalize unsafe or conflicting attempt writes as worker failures."""

    try:
        job_directory = path.parent.parent
        InvocationEvidenceStore(job_directory).write(path, record)
        if record.status == 'completed':
            _record_finalized_path(Path(record.stdout_path), 'process_stdout')
            _record_finalized_path(Path(record.stderr_path), 'process_stderr')
    except InvocationEvidenceError as error:
        code = (
            RESUME_ACTIVATION_UNCERTAIN_CODE
            if str(error) == 'attempt record already exists'
            else None
        )
        raise WorkerError(f'invalid invocation evidence: {error}', code=code) from error


def _record_invocation(
    *,
    run: Run,
    role: Literal['developer', 'reviewer'],
    identity: InvocationIdentity,
    iteration: int,
    sequence: int,
    started_at: str,
    logs: Path,
    invocations: Path,
    stdout: str | bytes | None,
    stderr: str | bytes | None,
    exit_code: int | None,
    timed_out: bool = False,
    interrupted: bool = False,
    invocation_id: str | None = None,
    finished: bool = True,
    attempt: int = 1,
    effective_models: tuple[str, ...] = (),
    effective_model_status: Literal['reported', 'unavailable'] = 'unavailable',
    status: Literal['pending', 'running', 'completed'] | None = None,
    conclusion: Literal['succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted']
    | None = None,
    response_received_at: str | None = None,
    validation_started_at: str | None = None,
    finished_at_value: str | None = None,
    reviewer_id: str | None = None,
) -> str:
    """Persist separate streams and their adapter-neutral invocation record."""

    if reviewer_id is not None:
        if role != 'reviewer':
            message = 'only reviewer invocations can have a reviewer ID'
            raise WorkerError(message)
        try:
            task_id = reviewer_task_id(str(run.id), sequence, reviewer_id)
            invocation_id = invocation_id or reviewer_invocation_id(
                str(run.id), sequence, reviewer_id, attempt
            )
            log_stem = reviewer_invocation_stem(sequence, reviewer_id, attempt)
        except ReviewerIdentityError as error:
            raise WorkerError(str(error)) from error
        schema_version = 5
    else:
        task_id = f'{run.id}:{sequence:06d}-{role}'
        invocation_id = invocation_id or f'{task_id}:attempt-{attempt:04d}'
        log_stem = _invocation_stem(sequence, role, attempt)
        schema_version = 4
    attempt_status = status or ('completed' if finished else 'pending')
    if attempt_status == 'completed' and conclusion is None:
        if timed_out:
            conclusion = 'timed_out'
        elif interrupted:
            conclusion = 'interrupted'
        else:
            conclusion = 'succeeded' if exit_code == 0 else 'failed'
    stdout_path = logs / f'{log_stem}.stdout.log'
    stderr_path = logs / f'{log_stem}.stderr.log'
    if stdout is not None or not stdout_path.exists():
        _write_text_atomic(
            stdout_path,
            _output_text(stdout),
        )
    if stderr is not None or not stderr_path.exists():
        _write_text_atomic(
            stderr_path,
            _output_text(stderr),
        )
    _persist_attempt_record(
        invocations / f'{log_stem}.json',
        InvocationRecord(
            schema_version=schema_version,
            run_id=str(run.id),
            task_id=task_id,
            invocation_id=invocation_id,
            role=role,
            agent_vendor=identity.vendor,
            requested_model=identity.model,
            effective_models=effective_models,
            effective_model_status=effective_model_status,
            runtime=identity.runtime,
            iteration=iteration,
            started_at=started_at,
            finished_at=(finished_at_value or timestamp()) if finished else None,
            exit_code=exit_code,
            timed_out=timed_out,
            interrupted=interrupted,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            attempt=attempt,
            status=attempt_status,
            conclusion=conclusion,
            response_received_at=response_received_at,
            validation_started_at=validation_started_at,
            reviewer_id=reviewer_id,
        ),
    )
    return invocation_id


def _read_object(path: Path) -> dict[str, Any]:
    """Read a JSON object or raise a stable worker error."""

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkerError(f'invalid reviewer response: {error}') from error
    if not isinstance(document, dict):
        raise WorkerError(NOT_OBJECT)
    return document


def _validate_review_response(
    document: dict[str, Any], *, request: dict[str, Any], artifact_path: Path
) -> str:
    """Validate response correlation and return its verdict."""

    try:
        parsed = ReviewResultMessageSchema.model_validate(document)
    except ValidationError as error:
        details = error.errors()
        if any('cannot contain findings' in str(detail['msg']) for detail in details):
            message = APPROVED_WITH_FINDINGS
        elif any(
            CHANGES_REQUESTED_WITHOUT_FINDINGS in str(detail['msg'])
            for detail in details
        ):
            message = CHANGES_REQUESTED_WITHOUT_FINDINGS
        elif any(
            DUPLICATE_REVIEW_FINDING_IDS in str(detail['msg']) for detail in details
        ):
            message = DUPLICATE_REVIEW_FINDING_IDS
        elif any(
            detail['loc'] and detail['loc'][0] in {'message_id', 'created_at'}
            for detail in details
        ):
            message = INVALID_IDENTITY
        elif any(
            len(detail['loc']) > 1
            and detail['loc'][0] == 'payload'
            and detail['loc'][1] == 'verdict'
            for detail in details
        ):
            message = INVALID_VERDICT
        elif any(detail['loc'] and detail['loc'][0] == 'payload' for detail in details):
            message = INVALID_PAYLOAD
        else:
            message = INVALID_ENVELOPE
        raise WorkerError(message) from error
    expected_values = {
        'schema_version': 1,
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': int(request['sequence']) + 1,
        'iteration': request['iteration'],
        'message_type': 'review_result',
        'sender': 'reviewer',
        'recipient': 'orchestrator',
        'scope': request['scope'],
    }
    for key, expected in expected_values.items():
        if document[key] != expected:
            raise WorkerError(f'reviewer response has invalid {key}')
    payload = parsed.payload
    verdict = payload.verdict
    if payload.artifact_path != str(artifact_path):
        raise WorkerError(INVALID_ARTIFACT_PATH)
    if not artifact_path.is_file():
        raise WorkerError(MISSING_ARTIFACT)
    return str(verdict)


def _validate_developer_handoff(
    document: dict[str, Any], *, request: dict[str, Any], finding_ids: tuple[str, ...]
) -> DeveloperHandoffMessageSchema:
    """Validate a correlated handoff and return its canonical message."""

    try:
        parsed = DeveloperHandoffMessageSchema.model_validate(document)
    except ValidationError as error:
        raise WorkerError(INVALID_DEVELOPER_HANDOFF) from error
    expected = {
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': int(request['sequence']) + 1,
        'iteration': request['iteration'],
        'scope': request['scope'],
    }
    for field, value in expected.items():
        if document[field] != value:
            raise WorkerError(f'developer handoff has invalid {field}')
    dispositions = [item.finding_id for item in parsed.payload.dispositions]
    if len(dispositions) != len(set(dispositions)) or sorted(dispositions) != sorted(
        finding_ids
    ):
        raise WorkerError(INVALID_FINDING_DISPOSITIONS)
    return parsed


def _validate_remediation_request(
    document: dict[str, Any], *, run_directory: Path
) -> None:
    """Validate remediation authority and contained evidence references."""

    try:
        parsed = RemediationRequestMessageSchema.model_validate(document)
    except ValidationError as error:
        raise WorkerError(INVALID_REMEDIATION_REQUEST) from error
    if parsed.payload.allowed_actions:
        raise WorkerError(REMEDIATION_ACTIONS)
    evidence_paths = (
        _contained_job_reference(
            run_directory, parsed.payload.review_result_path, REMEDIATION_PATH_ESCAPE
        ),
        _contained_job_reference(
            run_directory, parsed.payload.review_artifact_path, REMEDIATION_PATH_ESCAPE
        ),
    )
    if any(not path.is_file() for path in evidence_paths):
        raise WorkerError(INVALID_REMEDIATION_REQUEST)


def _validate_review_request(document: dict[str, Any], *, run_directory: Path) -> None:
    """Validate review authority and contained artifact references."""

    try:
        parsed = ReviewRequestMessageSchema.model_validate(document)
    except ValidationError as error:
        raise WorkerError(INVALID_REVIEW_REQUEST) from error
    if parsed.payload.allowed_actions:
        raise WorkerError(INVALID_REVIEW_REQUEST)
    _contained_job_reference(
        run_directory, parsed.payload.artifact_path, REVIEW_PATH_ESCAPE
    )
    if parsed.payload.prior_review_path is not None:
        prior_path = _contained_job_reference(
            run_directory, parsed.payload.prior_review_path, REVIEW_PATH_ESCAPE
        )
        if not prior_path.is_file():
            raise WorkerError(INVALID_REVIEW_REQUEST)


def _require_unique_message_id(document: dict[str, Any], run_directory: Path) -> None:
    """Reject a response identifier already present in durable messages."""

    candidate = document.get('message_id')
    for path in run_directory.rglob('*.json'):
        relative = path.relative_to(run_directory).as_posix()
        if canonical_message_evidence(relative) is None:
            continue
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except OSError, json.JSONDecodeError:
            continue
        if isinstance(existing, dict) and existing.get('message_id') == candidate:
            raise WorkerError(DUPLICATE_MESSAGE_ID)


def _classify_remediation_progress(
    status: str, new_digest: str | None, current_digest: str
) -> tuple[bool, str]:
    """Return whether a valid handoff is recoverable and its measured digest."""

    if new_digest is None:
        raise WorkerError(NO_CHANGES)
    if status in {'blocked', 'failed'}:
        return True, new_digest
    if same_diff_digest(new_digest, current_digest):
        raise WorkerError(NO_REMEDIATION_CHANGE)
    return False, new_digest


def _validate_resumed_progress(
    status: str,
    new_digest: str | None,
    current_digest: str,
    *,
    allow_unchanged_ready: bool,
    is_disagreement: bool,
) -> tuple[bool, str]:
    """Validate a retried handoff and return its recovery classification."""

    if new_digest is None:
        raise WorkerError(NO_CHANGES)
    if status in {'blocked', 'failed'}:
        return True, new_digest
    if (
        not allow_unchanged_ready
        and not is_disagreement
        and same_diff_digest(new_digest, current_digest)
    ):
        raise WorkerError(NO_REMEDIATION_CHANGE)
    return False, new_digest


def _is_developer_disagreement(message: DeveloperHandoffMessageSchema) -> bool:
    """Return whether every finding was rejected or blocked without an edit."""

    dispositions = message.payload.dispositions
    return bool(dispositions) and all(
        item.disposition in {'rejected', 'blocked'} for item in dispositions
    )


def _read_execution_record(run_directory: Path, run_id: str) -> ExecutionRecord:
    """Read and validate the durable execution context for a resumable run."""

    path = _run_evidence_path(run_directory, 'execution.json')
    try:
        document = _read_object(path)
        record = EXECUTION_RECORD_ADAPTER.validate_python(document)
    except (WorkerError, ValidationError) as error:
        message = 'resume metadata is missing, legacy, or invalid'
        raise WorkerError(message, code=RESUME_METADATA_UNSUPPORTED_CODE) from error
    if record.run_id != run_id:
        message = 'resume metadata does not match the run ID'
        raise WorkerError(message)
    return record


def read_message_chain(
    run_directory: Path, run_id: str
) -> list[tuple[Path, dict[str, Any]]]:
    """Read and correlate every canonical message for recovery."""

    documents: list[tuple[Path, dict[str, Any]]] = []
    identities: dict[str, tuple[Path, dict[str, Any]]] = {}
    schemas: dict[str, type[BaseModel]] = {
        'review_request': ReviewRequestMessageSchema,
        'review_result': ReviewResultMessageSchema,
        'remediation_request': RemediationRequestMessageSchema,
        'developer_handoff': DeveloperHandoffMessageSchema,
    }
    candidates: list[tuple[int, str, Path]] = []
    message_directory = run_directory / 'messages'
    unsafe_namespace = 'resume message namespace is unsafe'
    if message_directory.is_symlink():
        raise WorkerError(unsafe_namespace)
    if message_directory.exists() and not message_directory.is_dir():
        raise WorkerError(unsafe_namespace)
    if message_directory.is_dir():
        try:
            with os.scandir(message_directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            message = 'resume message namespace is unreadable'
            raise WorkerError(message) from error
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(run_directory).as_posix()
            try:
                unsafe_entry = entry.is_symlink() or not entry.is_file(
                    follow_symlinks=False
                )
            except OSError as error:
                message = f'unreadable canonical message path: {relative}'
                raise WorkerError(message) from error
            if unsafe_entry:
                raise WorkerError(f'unsafe canonical message path: {relative}')
            identity = canonical_message_evidence(relative)
            if identity is None:
                raise WorkerError(f'unknown canonical message path: {relative}')
            path_message_type, sequence = identity
            candidates.append((sequence, path_message_type, path))
    reviewer_ids = {
        reviewer_id
        for sequence, message_type, path in candidates
        if (
            reviewer_id := _reviewer_id_from_message_path(
                path, sequence=sequence, message_type=message_type
            )
        )
        is not None
    }
    if reviewer_ids:
        return _read_reviewer_message_batch(
            candidates,
            reviewer_ids=reviewer_ids,
            run_directory=run_directory,
            run_id=run_id,
            schemas=schemas,
        )
    for expected_sequence, (sequence, expected_type, path) in enumerate(
        sorted(candidates), start=1
    ):
        _contained_job_reference(
            run_directory, path, 'resume message path escapes the run directory'
        )
        if sequence != expected_sequence:
            message = 'resume message sequence is not contiguous'
            raise WorkerError(message)
        document = _read_object(path)
        message_type = document.get('message_type')
        if not isinstance(message_type, str) or message_type != expected_type:
            raise WorkerError(f'invalid canonical message: {path.name}')
        schema = schemas.get(message_type)
        if schema is None:
            raise WorkerError(f'unsupported recoverable message type: {message_type}')
        try:
            schema.model_validate(document)
        except ValidationError as error:
            raise WorkerError(f'invalid canonical message: {path.name}') from error
        if document['run_id'] != run_id or document['sequence'] != sequence:
            raise WorkerError(f'message does not match recoverable run: {path.name}')
        parent_id = document['in_reply_to']
        parent_entry = identities.get(parent_id) if parent_id is not None else None
        parent = parent_entry[1] if parent_entry is not None else None
        previous = documents[-1][1] if documents else None
        if document['message_type'] == 'review_request':
            if previous is None:
                if (
                    parent_id is not None
                    or document['payload']['prior_review_path'] is not None
                ):
                    raise WorkerError(f'invalid message correlation: {path.name}')
            elif (
                parent_id is not None or previous['message_type'] != 'developer_handoff'
            ):
                raise WorkerError(f'invalid message correlation: {path.name}')
            else:
                remediation_entry = identities.get(previous['in_reply_to'])
                remediation = (
                    remediation_entry[1] if remediation_entry is not None else None
                )
                review_entry = (
                    identities.get(remediation['in_reply_to'])
                    if remediation is not None
                    else None
                )
                if (
                    remediation is None
                    or remediation['message_type'] != 'remediation_request'
                    or review_entry is None
                    or review_entry[1]['message_type'] != 'review_result'
                    or document['payload']['prior_review_path'] != str(review_entry[0])
                ):
                    raise WorkerError(f'invalid message correlation: {path.name}')
        elif document['message_type'] == 'review_result':
            if (
                parent is None
                or previous is not parent
                or parent['message_type'] != 'review_request'
                or document['sequence'] != parent['sequence'] + 1
            ):
                raise WorkerError(f'invalid message correlation: {path.name}')
            artifact_path = Path(document['payload']['artifact_path'])
            if (
                document['payload']['artifact_path']
                != parent['payload']['artifact_path']
                or not artifact_path.is_file()
            ):
                raise WorkerError(f'invalid message correlation: {path.name}')
            _contained_job_reference(
                run_directory,
                artifact_path,
                f'invalid message correlation: {path.name}',
            )
        elif document['message_type'] == 'remediation_request':
            prior_remediation = (
                identities.get(previous['in_reply_to'])
                if previous is not None
                else None
            )
            recovery_request = (
                previous is not None
                and previous['message_type'] == 'developer_handoff'
                and previous['payload']['status'] in {'blocked', 'failed'}
                and prior_remediation is not None
                and prior_remediation[1]['message_type'] == 'remediation_request'
                and prior_remediation[1]['in_reply_to'] == document['in_reply_to']
            )
            if (
                parent is None
                or parent_entry is None
                or parent['message_type'] != 'review_result'
                or parent['payload']['verdict'] != 'changes_requested'
                or document['payload']['review_result_path'] != str(parent_entry[0])
                or document['payload']['review_artifact_path']
                != parent['payload']['artifact_path']
                or (
                    not recovery_request
                    and (
                        previous is not parent
                        or document['sequence'] != parent['sequence'] + 1
                    )
                )
                or (
                    recovery_request
                    and previous is not None
                    and document['sequence'] != previous['sequence'] + 1
                )
            ):
                raise WorkerError(f'invalid message correlation: {path.name}')
        elif (
            parent is None
            or previous is not parent
            or parent['message_type'] != 'remediation_request'
            or document['sequence'] != parent['sequence'] + 1
        ):
            raise WorkerError(f'invalid message correlation: {path.name}')
        if parent is not None and (
            parent['run_id'] != document['run_id']
            or parent['scope'] != document['scope']
            or parent['iteration'] != document['iteration']
        ):
            raise WorkerError(f'invalid message correlation: {path.name}')
        message_id = document['message_id']
        if message_id in identities:
            raise WorkerError(DUPLICATE_MESSAGE_ID)
        identities[message_id] = (path, document)
        documents.append((path, document))
    if not documents:
        message = 'resume message chain is empty'
        raise WorkerError(message)
    return documents


def _reviewer_id_from_message_path(
    path: Path, *, sequence: int, message_type: str
) -> str | None:
    """Return a reviewer qualifier from one canonical message path."""

    prefix = f'{sequence:06d}-'
    suffix = f'-{message_type.replace("_", "-")}.json'
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        raise WorkerError(f'invalid canonical message path: {name}')
    reviewer_id = name[len(prefix) : -len(suffix)]
    if not reviewer_id:
        return None
    try:
        return validate_reviewer_id(reviewer_id)
    except ReviewerIdentityError as error:
        raise WorkerError(f'invalid canonical message path: {name}') from error


def _read_reviewer_message_batch(
    candidates: list[tuple[int, str, Path]],
    *,
    reviewer_ids: set[str],
    run_directory: Path,
    run_id: str,
    schemas: dict[str, type[BaseModel]],
) -> list[tuple[Path, dict[str, Any]]]:
    """Validate one initial reviewer batch as parallel correlated chains."""

    grouped: dict[str, list[tuple[int, str, Path]]] = {
        reviewer_id: [] for reviewer_id in reviewer_ids
    }
    for sequence, message_type, path in candidates:
        reviewer_id = _reviewer_id_from_message_path(
            path, sequence=sequence, message_type=message_type
        )
        if reviewer_id is None:
            raise WorkerError(MIXED_REVIEWER_MESSAGE_PATHS)
        grouped[reviewer_id].append((sequence, message_type, path))
    if len(grouped) < 2:
        raise WorkerError(SMALL_REVIEWER_MESSAGE_BATCH)

    documents: list[tuple[Path, dict[str, Any]]] = []
    identities: set[str] = set()
    expected_scope: dict[str, Any] | None = None
    expected_iteration: int | None = None
    for reviewer_id in sorted(grouped):
        chain = sorted(grouped[reviewer_id])
        if [(sequence, message_type) for sequence, message_type, _ in chain] != [
            (1, 'review_request'),
            (2, 'review_result'),
        ]:
            raise WorkerError(INCOMPLETE_REVIEWER_MESSAGE_BATCH)
        reviewer_documents: list[tuple[Path, dict[str, Any]]] = []
        for sequence, message_type, path in chain:
            _contained_job_reference(
                run_directory, path, 'resume message path escapes the run directory'
            )
            document = _read_object(path)
            schema = schemas[message_type]
            try:
                schema.model_validate(document)
            except ValidationError as error:
                raise WorkerError(f'invalid canonical message: {path.name}') from error
            if (
                document['message_type'] != message_type
                or document['run_id'] != run_id
                or document['sequence'] != sequence
            ):
                raise WorkerError(
                    f'message does not match recoverable run: {path.name}'
                )
            message_id = document['message_id']
            if message_id in identities:
                raise WorkerError(DUPLICATE_MESSAGE_ID)
            identities.add(message_id)
            reviewer_documents.append((path, document))

        request_path, request = reviewer_documents[0]
        result_path, result = reviewer_documents[1]
        if (
            request['in_reply_to'] is not None
            or request['payload']['prior_review_path'] is not None
            or result['in_reply_to'] != request['message_id']
            or result['scope'] != request['scope']
            or result['iteration'] != request['iteration']
            or result['payload']['artifact_path'] != request['payload']['artifact_path']
        ):
            raise WorkerError(f'invalid message correlation: {result_path.name}')
        artifact_path = Path(result['payload']['artifact_path'])
        if not artifact_path.is_file():
            raise WorkerError(f'invalid message correlation: {result_path.name}')
        _contained_job_reference(
            run_directory,
            artifact_path,
            f'invalid message correlation: {result_path.name}',
        )
        if expected_scope is None:
            expected_scope = request['scope']
            expected_iteration = request['iteration']
        elif (
            request['scope'] != expected_scope
            or request['iteration'] != expected_iteration
        ):
            raise WorkerError(f'invalid message correlation: {request_path.name}')
        documents.extend(reviewer_documents)
    return sorted(documents, key=lambda item: item[0].name)


def _identity_from_record(
    vendor: str, model: str | None, runtime: str
) -> InvocationIdentity:
    """Convert persisted execution identity fields into the runtime value."""

    return InvocationIdentity(vendor=vendor, model=model, runtime=runtime)


def _resolve_resume_identity(
    identity: InvocationIdentity,
    role: RuntimeRole,
    registry: RuntimeRegistry,
) -> InvocationIdentity:
    """Validate a persisted runtime role and derive its current vendor."""

    if identity.runtime == 'custom-command':
        return identity
    try:
        runtime = registry.require(identity.runtime, role)
    except RuntimeRegistryError as error:
        raise WorkerError(str(error), code=error.code) from error
    return InvocationIdentity(
        vendor=runtime.vendor,
        model=identity.model,
        runtime=runtime.identifier,
    )


def _latest_task_attempt(
    run_directory: Path, sequence: int, role: str
) -> InvocationRecord | None:
    """Return the latest validated attempt for one durable task."""

    try:
        records = InvocationEvidenceStore(run_directory).read_all(run_directory.name)
    except InvocationEvidenceError as error:
        message = 'invalid invocation evidence'
        raise WorkerError(message) from error
    task_id = f'{run_directory.name}:{sequence:06d}-{role}'
    attempts = [record for record in records if record.task_id == task_id]
    return max(attempts, key=lambda record: record.attempt) if attempts else None


def _attempt_activation_was_persisted(
    run_directory: Path,
    sequence: int,
    role: Literal['developer', 'reviewer'],
    attempt: int,
) -> bool:
    """Return whether activation is durable enough to finalize interruption."""

    latest = _latest_task_attempt(run_directory, sequence, role)
    return (
        latest is not None
        and latest.attempt == attempt
        and latest.status == AttemptStatus.RUNNING.value
    )


def _next_attempt(
    run_directory: Path, sequence: int, role: str, workflow_state: RunState
) -> int:
    """Return the next non-overwriting invocation attempt number."""

    latest = _latest_task_attempt(run_directory, sequence, role)
    action = recovery_action(
        latest,
        workflow_state=workflow_state,
    )
    if action is RecoveryAction.LAUNCH:
        return 1
    if action is not RecoveryAction.NONE or latest is None:
        message = 'cannot retry task with uncertain active attempt'
        raise WorkerError(
            message,
            code=RESUME_ACTIVATION_UNCERTAIN_CODE,
        )
    return latest.attempt + 1


def _run_queued_review(
    *,
    context: WorkerContext,
    plan: ReviewPlan,
    run: Run,
    continuation_sequence: int | None = None,
    continuation_prior_review_path: Path | None = None,
    retry_review_request: dict[str, Any] | None = None,
    reviewer_attempt: int = 1,
    resume_expected_state: RunState | None = None,
) -> Run:
    """Consume one queued local run through a bounded review-remediation loop."""

    store = context.store
    runs_directory = context.runs_directory
    digest_worktree = context.digest_worktree
    registry = context.registry
    objective = plan.objective
    reviewer_command = plan.reviewer_command
    developer_command = plan.developer_command
    timeout_seconds = plan.timeout_seconds
    developer_timeout_seconds = plan.developer_timeout_seconds
    max_iterations = plan.max_iterations
    reviewer_identity = plan.reviewer_identity
    developer_identity = plan.developer_identity

    continuing = run.state is RunState.REVIEWING and continuation_sequence is not None
    if run.state is not RunState.QUEUED and not continuing:
        raise WorkerError(f'run must be queued, found {run.state}')
    if not objective.strip():
        raise WorkerError(EMPTY_OBJECTIVE)
    if not reviewer_command:
        raise WorkerError(EMPTY_COMMAND)
    if timeout_seconds <= 0:
        message = 'timeout must be positive'
        raise WorkerError(message)
    if developer_timeout_seconds is None:
        developer_timeout_seconds = timeout_seconds
    if developer_timeout_seconds <= 0:
        raise WorkerError(INVALID_DEVELOPER_TIMEOUT)
    if max_iterations <= 0:
        raise WorkerError(INVALID_ITERATION_LIMIT)
    if not run.worktree_path.is_dir():
        raise WorkerError(f'worktree not found: {run.worktree_path}')

    reviewer_identity = _resolve_resume_identity(
        reviewer_identity, RuntimeRole.REVIEWER, registry
    )
    if developer_command:
        developer_identity = _resolve_resume_identity(
            developer_identity, RuntimeRole.DEVELOPER, registry
        )

    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)

    current_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
    if current_digest is None:
        raise WorkerError(NO_CHANGES)
    artifacts = _run_evidence_path(run_directory, 'artifacts')
    logs = _run_evidence_path(run_directory, 'logs')
    invocations = _run_evidence_path(run_directory, 'invocations')
    if continuing:
        assert continuation_sequence is not None
        _require_unchanged(current_digest, run.diff_digest or '')
        reviewing = run
        sequence = continuation_sequence
        prior_review_path = continuation_prior_review_path
    else:
        prepared = replace(
            transition(run, RunState.PREPARING),
            diff_digest=current_digest,
            updated_at=utc_now(),
        )
        store.update(prepared, expected_state=RunState.QUEUED)
        try:
            execution = _execution_record(
                replace(
                    plan,
                    developer_timeout_seconds=developer_timeout_seconds,
                    reviewer_identity=reviewer_identity,
                    developer_identity=developer_identity,
                ),
                run_id=str(run.id),
                created_at=datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
            )
        except ValidationError as error:
            raise WorkerError(f'invalid execution record: {error}') from error
        _write_json_atomic(
            _run_evidence_path(run_directory, 'execution.json'),
            execution.model_dump(mode='json'),
            'execution',
        )
        reviewing = transition(prepared, RunState.REVIEWING)
        store.update(reviewing, expected_state=RunState.PREPARING)
        sequence = 1
        prior_review_path = None
    artifacts.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    invocations.mkdir(parents=True, exist_ok=True)
    reviewer_adapter = CommandAgentAdapter(tuple(reviewer_command))
    developer_adapter = CommandAgentAdapter(tuple(developer_command))

    while True:
        if retry_review_request is None:
            artifact_path = artifacts / f'review-{reviewing.iteration:04d}.md'
            request_path = _manifest_evidence_path(
                run_directory, 'review_request', sequence
            )
            request: dict[str, Any] = {
                'schema_version': 1,
                'message_id': str(uuid4()),
                'in_reply_to': None,
                'run_id': str(run.id),
                'sequence': sequence,
                'iteration': reviewing.iteration,
                'message_type': 'review_request',
                'sender': 'orchestrator',
                'recipient': 'reviewer',
                'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
                'scope': {
                    'worktree_path': str(run.worktree_path),
                    'base_sha': run.base_sha,
                    'head_sha': run.head_sha,
                    'diff_digest': current_digest,
                },
                'payload': {
                    'objective': objective,
                    'allowed_actions': [],
                    'timeout_seconds': timeout_seconds,
                    'artifact_path': str(artifact_path),
                    'prior_review_path': (
                        str(prior_review_path)
                        if prior_review_path is not None
                        else None
                    ),
                },
            }
            _validate_review_request(request, run_directory=run_directory)
            _write_json_atomic(request_path, request, 'review_request')
        else:
            request = retry_review_request
            retry_review_request = None
            sequence = int(request['sequence'])
            artifact_path = Path(request['payload']['artifact_path'])
            request_path = _manifest_evidence_path(
                run_directory, 'review_request', sequence
            )
            _validate_review_request(request, run_directory=run_directory)
        response_path = _run_evidence_path(run_directory, '.review-result.json')
        reviewer_stem = _invocation_stem(sequence, 'reviewer', reviewer_attempt)
        reviewer_metadata_path = _run_evidence_path(
            run_directory, f'.{reviewer_stem}.runtime.json'
        )
        started_at = timestamp()
        invocation_id = _record_invocation(
            run=run,
            role='reviewer',
            identity=reviewer_identity,
            iteration=reviewing.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout='',
            stderr='',
            exit_code=None,
            finished=False,
            attempt=reviewer_attempt,
        )
        if resume_expected_state is not None:
            store.update(reviewing, expected_state=resume_expected_state)
            resume_expected_state = None
        try:
            completed = reviewer_adapter.execute(
                ReviewerRequest(
                    objective=objective,
                    worktree_path=run.worktree_path,
                    iteration=reviewing.iteration,
                    allowed_actions=(),
                    timeout_seconds=timeout_seconds,
                    base_sha=run.base_sha,
                    head_sha=run.head_sha,
                    diff_digest=current_digest,
                    artifact_path=artifact_path,
                    request_path=request_path,
                    response_path=response_path,
                    stdout_path=logs / f'{reviewer_stem}.stdout.log',
                    stderr_path=logs / f'{reviewer_stem}.stderr.log',
                    runtime_metadata_path=_runtime_metadata_path(
                        reviewer_identity, reviewer_metadata_path, registry
                    ),
                    on_started=partial(
                        _record_invocation,
                        run=run,
                        role='reviewer',
                        identity=reviewer_identity,
                        iteration=reviewing.iteration,
                        sequence=sequence,
                        started_at=started_at,
                        logs=logs,
                        invocations=invocations,
                        stdout=None,
                        stderr=None,
                        exit_code=None,
                        invocation_id=invocation_id,
                        finished=False,
                        attempt=reviewer_attempt,
                        status='running',
                    ),
                )
            )
            if artifact_path.is_file():
                _record_finalized_path(artifact_path, 'review_artifact')
        except subprocess.TimeoutExpired as error:
            effective_models, effective_model_status = _exception_runtime_metadata(
                error
            )
            _record_invocation(
                run=run,
                role='reviewer',
                identity=reviewer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout=error.stdout,
                stderr=error.stderr,
                exit_code=None,
                timed_out=True,
                invocation_id=invocation_id,
                attempt=reviewer_attempt,
                effective_models=effective_models,
                effective_model_status=effective_model_status,
            )
            _archive_unaccepted_response(
                response_path,
                logs / f'{sequence + 1:06d}-rejected-review-result-attempt-'
                f'{reviewer_attempt:04d}.json',
                'rejected_review_result',
            )
            _archive_unaccepted_response(
                artifact_path,
                logs / f'{sequence + 1:06d}-rejected-review-artifact-attempt-'
                f'{reviewer_attempt:04d}.md',
                'rejected_review_artifact',
            )
            interrupted = transition(reviewing, RunState.INTERRUPTED)
            store.update(interrupted, expected_state=RunState.REVIEWING)
            raise WorkerError(
                f'reviewer timed out after {timeout_seconds} seconds',
                code=RESUME_INTERRUPTED_CODE,
            ) from error
        except KeyboardInterrupt as error:
            effective_models, effective_model_status = _exception_runtime_metadata(
                error
            )
            if _attempt_activation_was_persisted(
                run_directory, sequence, 'reviewer', reviewer_attempt
            ):
                _record_invocation(
                    run=run,
                    role='reviewer',
                    identity=reviewer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    started_at=started_at,
                    logs=logs,
                    invocations=invocations,
                    stdout=None,
                    stderr=None,
                    exit_code=None,
                    interrupted=True,
                    invocation_id=invocation_id,
                    attempt=reviewer_attempt,
                    effective_models=effective_models,
                    effective_model_status=effective_model_status,
                )
            _archive_unaccepted_response(
                response_path,
                logs / f'{sequence + 1:06d}-rejected-review-result-attempt-'
                f'{reviewer_attempt:04d}.json',
                'rejected_review_result',
            )
            _archive_unaccepted_response(
                artifact_path,
                logs / f'{sequence + 1:06d}-rejected-review-artifact-attempt-'
                f'{reviewer_attempt:04d}.md',
                'rejected_review_artifact',
            )
            interrupted = transition(reviewing, RunState.INTERRUPTED)
            store.update(interrupted, expected_state=RunState.REVIEWING)
            raise
        except OSError as error:
            effective_models, effective_model_status = _exception_runtime_metadata(
                error
            )
            _record_invocation(
                run=run,
                role='reviewer',
                identity=reviewer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout='',
                stderr=str(error),
                exit_code=None,
                invocation_id=invocation_id,
                attempt=reviewer_attempt,
                effective_models=effective_models,
                effective_model_status=effective_model_status,
            )
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise WorkerError(
                f'cannot execute reviewer: {error}',
                code=RESUME_EXECUTION_FAILED_CODE,
            ) from error
        process_finished_at = timestamp()
        if not completed.succeeded:
            _record_invocation(
                run=run,
                role='reviewer',
                identity=reviewer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.exit_code,
                invocation_id=invocation_id,
                attempt=reviewer_attempt,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
                conclusion='failed',
                finished_at_value=process_finished_at,
            )
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise WorkerError(
                f'reviewer exited with code {completed.exit_code}',
                code=RESUME_EXECUTION_FAILED_CODE,
            )

        response_valid = False
        review_result_path = _manifest_evidence_path(
            run_directory, 'review_result', sequence + 1
        )
        response_received_at = timestamp() if response_path.is_file() else None
        validation_started_at = timestamp()
        _record_invocation(
            run=run,
            role='reviewer',
            identity=reviewer_identity,
            iteration=reviewing.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.exit_code,
            invocation_id=invocation_id,
            attempt=reviewer_attempt,
            status='running',
            response_received_at=response_received_at,
            validation_started_at=validation_started_at,
            finished_at_value=process_finished_at,
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        )
        try:
            response = _read_object(response_path)
            _require_unique_message_id(response, run_directory)
            verdict = _validate_review_response(
                response, request=request, artifact_path=artifact_path
            )
            _require_unchanged(
                _digest(digest_worktree, run.worktree_path, run.base_sha),
                current_digest,
            )
            response_valid = True
        except WorkerError:
            _record_invocation(
                run=run,
                role='reviewer',
                identity=reviewer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout=None,
                stderr=None,
                exit_code=completed.exit_code,
                invocation_id=invocation_id,
                attempt=reviewer_attempt,
                conclusion='failed',
                response_received_at=response_received_at,
                validation_started_at=validation_started_at,
                finished_at_value=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            )
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise
        finally:
            if response_path.exists():
                destination = (
                    review_result_path
                    if response_valid
                    else logs / f'{sequence + 1:06d}-rejected-review-result.json'
                )
                _finalize_temporary_path(
                    response_path,
                    destination,
                    'review_result' if response_valid else 'rejected_review_result',
                )

        _record_invocation(
            run=run,
            role='reviewer',
            identity=reviewer_identity,
            iteration=reviewing.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=None,
            stderr=None,
            exit_code=completed.exit_code,
            invocation_id=invocation_id,
            attempt=reviewer_attempt,
            conclusion='succeeded',
            response_received_at=response_received_at,
            validation_started_at=validation_started_at,
            finished_at_value=process_finished_at,
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        )
        reviewer_attempt = 1

        if verdict == 'blocked':
            return reviewing
        decided = transition(
            reviewing,
            RunState.APPROVED if verdict == 'approved' else RunState.CHANGES_REQUESTED,
        )
        store.update(decided, expected_state=RunState.REVIEWING)
        if verdict == 'approved':
            awaiting = transition(decided, RunState.AWAITING_COMMIT_AUTHORIZATION)
            store.update(awaiting, expected_state=RunState.APPROVED)
            return awaiting
        if reviewing.iteration >= max_iterations:
            failed = transition(decided, RunState.FAILED)
            store.update(failed, expected_state=RunState.CHANGES_REQUESTED)
            raise WorkerError(ITERATION_LIMIT)
        if not developer_command:
            return decided

        sequence += 2
        remediation_path = _manifest_evidence_path(
            run_directory, 'remediation_request', sequence
        )
        handoff_temporary = _run_evidence_path(run_directory, '.developer-handoff.json')
        remediation: dict[str, Any] = {
            'schema_version': 1,
            'message_id': str(uuid4()),
            'in_reply_to': response['message_id'],
            'run_id': str(run.id),
            'sequence': sequence,
            'iteration': reviewing.iteration,
            'message_type': 'remediation_request',
            'sender': 'orchestrator',
            'recipient': 'developer',
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
            'scope': request['scope'],
            'payload': {
                'objective': objective,
                'allowed_actions': [],
                'timeout_seconds': developer_timeout_seconds,
                'review_result_path': str(review_result_path),
                'review_artifact_path': str(artifact_path),
            },
        }
        _validate_remediation_request(remediation, run_directory=run_directory)
        _write_json_atomic(remediation_path, remediation, 'remediation_request')
        developing = transition(decided, RunState.DEVELOPING)
        store.update(developing, expected_state=RunState.CHANGES_REQUESTED)
        developer_stem = _invocation_stem(sequence, 'developer', 1)
        developer_metadata_path = _run_evidence_path(
            run_directory, f'.{developer_stem}.runtime.json'
        )
        started_at = timestamp()
        invocation_id = _record_invocation(
            run=run,
            role='developer',
            identity=developer_identity,
            iteration=reviewing.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout='',
            stderr='',
            exit_code=None,
            finished=False,
        )
        try:
            completed = developer_adapter.execute(
                DeveloperRequest(
                    objective=objective,
                    worktree_path=run.worktree_path,
                    iteration=reviewing.iteration,
                    allowed_actions=(),
                    timeout_seconds=developer_timeout_seconds,
                    request_path=remediation_path,
                    response_path=handoff_temporary,
                    stdout_path=logs / f'{sequence:06d}-developer.stdout.log',
                    stderr_path=logs / f'{sequence:06d}-developer.stderr.log',
                    runtime_metadata_path=_runtime_metadata_path(
                        developer_identity, developer_metadata_path, registry
                    ),
                    on_started=partial(
                        _record_invocation,
                        run=run,
                        role='developer',
                        identity=developer_identity,
                        iteration=reviewing.iteration,
                        sequence=sequence,
                        started_at=started_at,
                        logs=logs,
                        invocations=invocations,
                        stdout=None,
                        stderr=None,
                        exit_code=None,
                        invocation_id=invocation_id,
                        finished=False,
                        status='running',
                    ),
                )
            )
        except subprocess.TimeoutExpired as error:
            effective_models, effective_model_status = _exception_runtime_metadata(
                error
            )
            _record_invocation(
                run=run,
                role='developer',
                identity=developer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout=error.stdout,
                stderr=error.stderr,
                exit_code=None,
                timed_out=True,
                invocation_id=invocation_id,
                effective_models=effective_models,
                effective_model_status=effective_model_status,
            )
            _archive_unaccepted_response(
                handoff_temporary,
                logs
                / f'{sequence + 1:06d}-rejected-developer-handoff-attempt-0001.json',
                'rejected_developer_handoff',
            )
            interrupted = transition(developing, RunState.INTERRUPTED)
            store.update(interrupted, expected_state=RunState.DEVELOPING)
            raise WorkerError(
                f'developer timed out after {developer_timeout_seconds} seconds',
                code=RESUME_INTERRUPTED_CODE,
            ) from error
        except KeyboardInterrupt as error:
            effective_models, effective_model_status = _exception_runtime_metadata(
                error
            )
            if _attempt_activation_was_persisted(
                run_directory, sequence, 'developer', 1
            ):
                _record_invocation(
                    run=run,
                    role='developer',
                    identity=developer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    started_at=started_at,
                    logs=logs,
                    invocations=invocations,
                    stdout=None,
                    stderr=None,
                    exit_code=None,
                    interrupted=True,
                    invocation_id=invocation_id,
                    effective_models=effective_models,
                    effective_model_status=effective_model_status,
                )
            _archive_unaccepted_response(
                handoff_temporary,
                logs
                / f'{sequence + 1:06d}-rejected-developer-handoff-attempt-0001.json',
                'rejected_developer_handoff',
            )
            interrupted = transition(developing, RunState.INTERRUPTED)
            store.update(interrupted, expected_state=RunState.DEVELOPING)
            raise
        except OSError as error:
            effective_models, effective_model_status = _exception_runtime_metadata(
                error
            )
            _record_invocation(
                run=run,
                role='developer',
                identity=developer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout='',
                stderr=str(error),
                exit_code=None,
                invocation_id=invocation_id,
                effective_models=effective_models,
                effective_model_status=effective_model_status,
            )
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise WorkerError(
                f'cannot execute developer: {error}',
                code=RESUME_EXECUTION_FAILED_CODE,
            ) from error
        process_finished_at = timestamp()
        if not completed.succeeded:
            _record_invocation(
                run=run,
                role='developer',
                identity=developer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.exit_code,
                invocation_id=invocation_id,
                conclusion='failed',
                finished_at_value=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            )
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise WorkerError(
                f'developer exited with code {completed.exit_code}',
                code=RESUME_EXECUTION_FAILED_CODE,
            )
        handoff_valid = False
        handoff_path = _manifest_evidence_path(
            run_directory, 'developer_handoff', sequence + 1
        )
        response_received_at = timestamp() if handoff_temporary.is_file() else None
        validation_started_at = timestamp()
        _record_invocation(
            run=run,
            role='developer',
            identity=developer_identity,
            iteration=reviewing.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.exit_code,
            invocation_id=invocation_id,
            status='running',
            response_received_at=response_received_at,
            validation_started_at=validation_started_at,
            finished_at_value=process_finished_at,
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        )
        finding_ids = tuple(
            finding['finding_id'] for finding in response['payload']['findings']
        )
        try:
            handoff = _read_object(handoff_temporary)
            _require_unique_message_id(handoff, run_directory)
            parsed_handoff = _validate_developer_handoff(
                handoff, request=remediation, finding_ids=finding_ids
            )
            handoff_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
            is_disagreement = (
                parsed_handoff.payload.status == 'ready_for_review'
                and handoff_digest is not None
                and same_diff_digest(handoff_digest, current_digest)
                and _is_developer_disagreement(parsed_handoff)
            )
            if is_disagreement:
                assert handoff_digest is not None
                recoverable, new_digest = False, handoff_digest
            else:
                recoverable, new_digest = _classify_remediation_progress(
                    parsed_handoff.payload.status, handoff_digest, current_digest
                )
            _record_invocation(
                run=run,
                role='developer',
                identity=developer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout=None,
                stderr=None,
                exit_code=completed.exit_code,
                invocation_id=invocation_id,
                conclusion='succeeded',
                response_received_at=response_received_at,
                validation_started_at=validation_started_at,
                finished_at_value=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            )
            handoff_valid = True
            if is_disagreement:
                disagreement = transition(developing, RunState.CHANGES_REQUESTED)
                store.update(disagreement, expected_state=RunState.DEVELOPING)
                _write_json_atomic(
                    _run_evidence_path(run_directory, 'decision-required.json'),
                    {
                        'schema_version': 1,
                        'run_id': str(run.id),
                        'state': str(disagreement.state),
                        'reason': {
                            'code': 'developer_disagreement',
                            'message': DEVELOPER_DISAGREEMENT,
                        },
                        'developer_handoff_path': str(handoff_path),
                        'created_at': datetime.now(UTC)
                        .isoformat()
                        .replace('+00:00', 'Z'),
                    },
                    'decision_required',
                )
                return disagreement
            if recoverable:
                validation_required = replace(
                    transition(developing, RunState.VALIDATION_REQUIRED),
                    diff_digest=new_digest,
                    updated_at=utc_now(),
                )
                store.update(validation_required, expected_state=RunState.DEVELOPING)
                return validation_required
        except WorkerError:
            _record_invocation(
                run=run,
                role='developer',
                identity=developer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout=None,
                stderr=None,
                exit_code=completed.exit_code,
                invocation_id=invocation_id,
                conclusion='failed',
                response_received_at=response_received_at,
                validation_started_at=validation_started_at,
                finished_at_value=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            )
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise
        finally:
            if handoff_temporary.exists():
                destination = (
                    handoff_path
                    if handoff_valid
                    else logs
                    / f'{sequence + 1:06d}-rejected-developer-handoff-attempt-0001.json'
                )
                _finalize_temporary_path(
                    handoff_temporary,
                    destination,
                    'developer_handoff'
                    if handoff_valid
                    else 'rejected_developer_handoff',
                )
        current_digest = new_digest
        prior_review_path = review_result_path
        reviewing = replace(
            transition(developing, RunState.REVIEWING),
            diff_digest=current_digest,
            updated_at=utc_now(),
        )
        store.update(reviewing, expected_state=RunState.DEVELOPING)
        sequence += 2


def _resume_developer_request(
    *,
    context: WorkerContext,
    plan: ReviewPlan,
    run: Run,
    request: dict[str, Any],
    current_digest: str,
    allow_unchanged_ready: bool,
    attempt: int,
    resume_expected_state: RunState | None = None,
) -> Run:
    """Retry one durable remediation request and continue the same run."""

    store = context.store
    runs_directory = context.runs_directory
    digest_worktree = context.digest_worktree
    registry = context.registry
    developer_command = plan.developer_command
    # This path always carries an integer, taken from the durable execution
    # record, and its parameter was typed as such before the plan existed.
    # Normalizing here mirrors what _run_queued_review does with the same
    # caller-facing optional value.
    developer_timeout_seconds = (
        plan.timeout_seconds
        if plan.developer_timeout_seconds is None
        else plan.developer_timeout_seconds
    )
    developer_identity = plan.developer_identity

    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    logs = _run_evidence_path(run_directory, 'logs')
    invocations = _run_evidence_path(run_directory, 'invocations')
    sequence = int(request['sequence'])
    response_path = _run_evidence_path(run_directory, '.developer-handoff.json')
    handoff_path = _manifest_evidence_path(
        run_directory, 'developer_handoff', sequence + 1
    )
    review_result_path = Path(request['payload']['review_result_path']).resolve()
    review_result = _read_object(review_result_path)
    finding_ids = tuple(
        finding['finding_id'] for finding in review_result['payload']['findings']
    )
    adapter = CommandAgentAdapter(tuple(developer_command))
    developer_stem = _invocation_stem(sequence, 'developer', attempt)
    developer_metadata_path = _run_evidence_path(
        run_directory, f'.{developer_stem}.runtime.json'
    )
    started_at = timestamp()
    if resume_expected_state is not None:
        store.update(run, expected_state=resume_expected_state)
    invocation_id = _record_invocation(
        run=run,
        role='developer',
        identity=developer_identity,
        iteration=run.iteration,
        sequence=sequence,
        started_at=started_at,
        logs=logs,
        invocations=invocations,
        stdout='',
        stderr='',
        exit_code=None,
        finished=False,
        attempt=attempt,
    )
    try:
        completed = adapter.execute(
            DeveloperRequest(
                objective=request['payload']['objective'],
                worktree_path=run.worktree_path,
                iteration=run.iteration,
                allowed_actions=(),
                timeout_seconds=developer_timeout_seconds,
                request_path=_manifest_evidence_path(
                    run_directory, 'remediation_request', sequence
                ),
                response_path=response_path,
                stdout_path=logs / f'{developer_stem}.stdout.log',
                stderr_path=logs / f'{developer_stem}.stderr.log',
                runtime_metadata_path=_runtime_metadata_path(
                    developer_identity, developer_metadata_path, registry
                ),
                on_started=partial(
                    _record_invocation,
                    run=run,
                    role='developer',
                    identity=developer_identity,
                    iteration=run.iteration,
                    sequence=sequence,
                    started_at=started_at,
                    logs=logs,
                    invocations=invocations,
                    stdout=None,
                    stderr=None,
                    exit_code=None,
                    invocation_id=invocation_id,
                    finished=False,
                    attempt=attempt,
                    status='running',
                ),
            )
        )
    except subprocess.TimeoutExpired as error:
        effective_models, effective_model_status = _exception_runtime_metadata(error)
        _record_invocation(
            run=run,
            role='developer',
            identity=developer_identity,
            iteration=run.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=error.stdout,
            stderr=error.stderr,
            exit_code=None,
            timed_out=True,
            invocation_id=invocation_id,
            attempt=attempt,
            effective_models=effective_models,
            effective_model_status=effective_model_status,
        )
        _archive_unaccepted_response(
            response_path,
            logs / f'{sequence + 1:06d}-rejected-developer-handoff-attempt-'
            f'{attempt:04d}.json',
            'rejected_developer_handoff',
        )
        interrupted = transition(run, RunState.INTERRUPTED)
        store.update(interrupted, expected_state=RunState.DEVELOPING)
        raise WorkerError(
            f'developer timed out after {developer_timeout_seconds} seconds',
            code=RESUME_INTERRUPTED_CODE,
        ) from error
    except KeyboardInterrupt as error:
        effective_models, effective_model_status = _exception_runtime_metadata(error)
        if _attempt_activation_was_persisted(
            run_directory, sequence, 'developer', attempt
        ):
            _record_invocation(
                run=run,
                role='developer',
                identity=developer_identity,
                iteration=run.iteration,
                sequence=sequence,
                started_at=started_at,
                logs=logs,
                invocations=invocations,
                stdout=None,
                stderr=None,
                exit_code=None,
                interrupted=True,
                invocation_id=invocation_id,
                attempt=attempt,
                effective_models=effective_models,
                effective_model_status=effective_model_status,
            )
        _archive_unaccepted_response(
            response_path,
            logs / f'{sequence + 1:06d}-rejected-developer-handoff-attempt-'
            f'{attempt:04d}.json',
            'rejected_developer_handoff',
        )
        interrupted = transition(run, RunState.INTERRUPTED)
        store.update(interrupted, expected_state=RunState.DEVELOPING)
        raise
    except OSError as error:
        effective_models, effective_model_status = _exception_runtime_metadata(error)
        _record_invocation(
            run=run,
            role='developer',
            identity=developer_identity,
            iteration=run.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=None,
            stderr=str(error),
            exit_code=None,
            invocation_id=invocation_id,
            attempt=attempt,
            effective_models=effective_models,
            effective_model_status=effective_model_status,
        )
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.DEVELOPING)
        raise WorkerError(
            f'cannot execute developer: {error}',
            code=RESUME_EXECUTION_FAILED_CODE,
        ) from error
    process_finished_at = timestamp()
    if not completed.succeeded:
        _record_invocation(
            run=run,
            role='developer',
            identity=developer_identity,
            iteration=run.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.exit_code,
            invocation_id=invocation_id,
            attempt=attempt,
            conclusion='failed',
            finished_at_value=process_finished_at,
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        )
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.DEVELOPING)
        raise WorkerError(
            f'developer exited with code {completed.exit_code}',
            code=RESUME_EXECUTION_FAILED_CODE,
        )

    handoff_valid = False
    response_received_at = timestamp() if response_path.is_file() else None
    validation_started_at = timestamp()
    _record_invocation(
        run=run,
        role='developer',
        identity=developer_identity,
        iteration=run.iteration,
        sequence=sequence,
        started_at=started_at,
        logs=logs,
        invocations=invocations,
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=completed.exit_code,
        invocation_id=invocation_id,
        attempt=attempt,
        status='running',
        response_received_at=response_received_at,
        validation_started_at=validation_started_at,
        finished_at_value=process_finished_at,
        effective_models=completed.effective_models,
        effective_model_status=completed.effective_model_status,
    )
    try:
        handoff = _read_object(response_path)
        _require_unique_message_id(handoff, run_directory)
        parsed = _validate_developer_handoff(
            handoff, request=request, finding_ids=finding_ids
        )
        recoverable, measured_digest = _validate_resumed_progress(
            parsed.payload.status,
            _digest(digest_worktree, run.worktree_path, run.base_sha),
            current_digest,
            allow_unchanged_ready=allow_unchanged_ready,
            is_disagreement=_is_developer_disagreement(parsed),
        )
        _record_invocation(
            run=run,
            role='developer',
            identity=developer_identity,
            iteration=run.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=None,
            stderr=None,
            exit_code=completed.exit_code,
            invocation_id=invocation_id,
            attempt=attempt,
            conclusion='succeeded',
            response_received_at=response_received_at,
            validation_started_at=validation_started_at,
            finished_at_value=process_finished_at,
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        )
        handoff_valid = True
        if recoverable:
            validation_required = replace(
                transition(run, RunState.VALIDATION_REQUIRED),
                diff_digest=measured_digest,
                updated_at=utc_now(),
            )
            store.update(validation_required, expected_state=RunState.DEVELOPING)
            return validation_required
        if (
            not allow_unchanged_ready
            and same_diff_digest(measured_digest, current_digest)
            and _is_developer_disagreement(parsed)
        ):
            disagreement = transition(run, RunState.CHANGES_REQUESTED)
            store.update(disagreement, expected_state=RunState.DEVELOPING)
            return disagreement
    except WorkerError:
        _record_invocation(
            run=run,
            role='developer',
            identity=developer_identity,
            iteration=run.iteration,
            sequence=sequence,
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=None,
            stderr=None,
            exit_code=completed.exit_code,
            invocation_id=invocation_id,
            attempt=attempt,
            conclusion='failed',
            response_received_at=response_received_at,
            validation_started_at=validation_started_at,
            finished_at_value=process_finished_at,
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        )
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.DEVELOPING)
        raise
    finally:
        if response_path.exists():
            destination = (
                handoff_path
                if handoff_valid
                else logs / f'{sequence + 1:06d}-rejected-developer-handoff-attempt-'
                f'{attempt:04d}.json'
            )
            _finalize_temporary_path(
                response_path,
                destination,
                'developer_handoff' if handoff_valid else 'rejected_developer_handoff',
            )

    reviewing = replace(
        transition(run, RunState.REVIEWING),
        diff_digest=measured_digest,
        updated_at=utc_now(),
    )
    store.update(reviewing, expected_state=RunState.DEVELOPING)
    return _run_queued_review(
        context=context,
        # The objective is taken from the durable remediation request rather
        # than from the plan, exactly as before this collaborator existed. The
        # two agree for every request this worker writes, but the request is
        # read back from evidence that can predate the execution record.
        plan=replace(plan, objective=request['payload']['objective']),
        run=reviewing,
        continuation_sequence=sequence + 2,
        continuation_prior_review_path=review_result_path,
    )


def _attempt_record_path(
    run_directory: Path, sequence: int, role: str, attempt: int
) -> Path:
    """Return the durable record path for one task attempt."""

    return _run_evidence_path(
        run_directory,
        'invocations',
        f'{_invocation_stem(sequence, role, attempt)}.json',
    )


def _recovery_response_path(temporary: Path, canonical: Path) -> tuple[Path, bool]:
    """Select one unambiguous response artifact and whether it is temporary."""

    if temporary.is_file() and canonical.is_file():
        message = 'recovery found duplicate response artifacts'
        raise WorkerError(message)
    if canonical.is_file():
        return canonical, False
    return temporary, True


def _prepare_recovered_validation(
    *,
    run_directory: Path,
    sequence: int,
    role: Literal['developer', 'reviewer'],
    record: InvocationRecord,
    response_present: bool,
) -> InvocationRecord:
    """Persist missing process and validation milestones without relaunching."""

    finished_at = (
        record.finished_at
        or record.response_received_at
        or record.validation_started_at
        or timestamp()
    )
    response_received_at = record.response_received_at
    if response_received_at is None and response_present:
        response_received_at = timestamp()
    validation_started_at = record.validation_started_at or timestamp()
    updated = replace(
        record,
        finished_at=finished_at,
        response_received_at=response_received_at,
        validation_started_at=validation_started_at,
    )
    _persist_attempt_record(
        _attempt_record_path(run_directory, sequence, role, record.attempt), updated
    )
    return updated


def _complete_recovered_validation(
    *,
    run_directory: Path,
    sequence: int,
    role: Literal['developer', 'reviewer'],
    record: InvocationRecord,
    conclusion: AttemptConclusion,
) -> InvocationRecord:
    """Persist an idempotently revalidated terminal attempt."""

    if record.status == 'completed':
        if record.conclusion != conclusion:
            message = 'completed attempt contradicts recovered validation'
            raise WorkerError(message)
        return record
    completed = transition_attempt(
        record,
        AttemptStatus.COMPLETED,
        conclusion=conclusion,
        finished_at=record.finished_at,
        response_received_at=record.response_received_at,
        validation_started_at=record.validation_started_at,
    )
    _persist_attempt_record(
        _attempt_record_path(run_directory, sequence, role, record.attempt), completed
    )
    return completed


def _raise_recovered_conclusion(
    *, store: JobStore, run: Run, role: str, record: InvocationRecord
) -> Never:
    """Apply a terminal unsuccessful attempt conclusion to the workflow."""

    if record.conclusion == 'failed':
        target = RunState.FAILED
        code = RESUME_EXECUTION_FAILED_CODE
    elif record.conclusion in {'timed_out', 'interrupted'}:
        target = RunState.INTERRUPTED
        code = RESUME_INTERRUPTED_CODE
    elif record.conclusion == 'cancelled':
        target = RunState.CANCELLED
        code = RESUME_CANCELLED_CODE
    else:
        message = 'recovered attempt has no applicable conclusion'
        raise WorkerError(message)
    recovered = transition(run, target)
    store.update(recovered, expected_state=run.state)
    raise WorkerError(
        f'{role} attempt completed with {record.conclusion}',
        code=code,
    )


def _resume_reviewer_validation(
    *,
    context: WorkerContext,
    run: Run,
    request: dict[str, Any],
    record: InvocationRecord,
    action: RecoveryAction,
    execution: ExecutionRecordSchema,
    reviewer_identity: InvocationIdentity,
    developer_identity: InvocationIdentity,
) -> Run:
    """Revalidate a durable reviewer response and continue without relaunching."""
    store = context.store
    runs_directory = context.runs_directory
    digest_worktree = context.digest_worktree

    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    sequence = int(request['sequence'])
    temporary = _run_evidence_path(run_directory, '.review-result.json')
    canonical = _manifest_evidence_path(run_directory, 'review_result', sequence + 1)
    response_path, is_temporary = _recovery_response_path(temporary, canonical)
    if action is not RecoveryAction.APPLY_CONCLUSION:
        record = _prepare_recovered_validation(
            run_directory=run_directory,
            sequence=sequence,
            role='reviewer',
            record=record,
            response_present=response_path.is_file(),
        )
    if record.conclusion not in {None, 'succeeded'}:
        _raise_recovered_conclusion(
            store=store, run=run, role='reviewer', record=record
        )
    artifact_path = Path(request['payload']['artifact_path'])
    try:
        response = _read_object(response_path)
        if is_temporary:
            _require_unique_message_id(response, run_directory)
        verdict = _validate_review_response(
            response, request=request, artifact_path=artifact_path
        )
        _require_unchanged(
            _digest(digest_worktree, run.worktree_path, run.base_sha),
            run.diff_digest or '',
        )
    except WorkerError:
        if record.status != 'completed':
            _complete_recovered_validation(
                run_directory=run_directory,
                sequence=sequence,
                role='reviewer',
                record=record,
                conclusion=AttemptConclusion.FAILED,
            )
        if response_path.exists():
            destination = _run_evidence_path(
                run_directory,
                'logs',
                f'{sequence + 1:06d}-rejected-review-result-attempt-'
                f'{record.attempt:04d}.json',
            )
            _finalize_temporary_path(
                response_path, destination, 'rejected_review_result'
            )
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.REVIEWING)
        raise
    if is_temporary:
        _finalize_temporary_path(response_path, canonical, 'review_result')
    _complete_recovered_validation(
        run_directory=run_directory,
        sequence=sequence,
        role='reviewer',
        record=record,
        conclusion=AttemptConclusion.SUCCEEDED,
    )
    if verdict == 'blocked':
        return run
    decided = transition(
        run,
        RunState.APPROVED if verdict == 'approved' else RunState.CHANGES_REQUESTED,
    )
    store.update(decided, expected_state=RunState.REVIEWING)
    if verdict == 'approved':
        awaiting = transition(decided, RunState.AWAITING_COMMIT_AUTHORIZATION)
        store.update(awaiting, expected_state=RunState.APPROVED)
        return awaiting
    if run.iteration >= execution.max_review_iterations:
        failed = transition(decided, RunState.FAILED)
        store.update(failed, expected_state=RunState.CHANGES_REQUESTED)
        raise WorkerError(ITERATION_LIMIT)
    if not execution.developer.command:
        return decided
    next_sequence = sequence + 2
    review_artifact_path = Path(request['payload']['artifact_path'])
    remediation_path = _manifest_evidence_path(
        run_directory, 'remediation_request', next_sequence
    )
    remediation: dict[str, Any] = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': response['message_id'],
        'run_id': str(run.id),
        'sequence': next_sequence,
        'iteration': run.iteration,
        'message_type': 'remediation_request',
        'sender': 'orchestrator',
        'recipient': 'developer',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': request['scope'],
        'payload': {
            'objective': execution.objective,
            'allowed_actions': [],
            'timeout_seconds': execution.developer.timeout_seconds,
            'review_result_path': str(canonical),
            'review_artifact_path': str(review_artifact_path),
        },
    }
    _validate_remediation_request(remediation, run_directory=run_directory)
    _write_json_atomic(remediation_path, remediation, 'remediation_request')
    developing = transition(decided, RunState.DEVELOPING)
    return _resume_developer_request(
        context=context,
        plan=ReviewPlan.from_execution_record(
            execution,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
        ),
        run=developing,
        request=remediation,
        current_digest=run.diff_digest or '',
        allow_unchanged_ready=False,
        attempt=1,
        resume_expected_state=RunState.CHANGES_REQUESTED,
    )


def _resume_developer_validation(
    *,
    context: WorkerContext,
    run: Run,
    request: dict[str, Any],
    record: InvocationRecord,
    action: RecoveryAction,
    execution: ExecutionRecordSchema,
    reviewer_identity: InvocationIdentity,
    developer_identity: InvocationIdentity,
) -> Run:
    """Revalidate a durable developer response and continue without relaunching."""
    store = context.store
    runs_directory = context.runs_directory
    digest_worktree = context.digest_worktree

    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    sequence = int(request['sequence'])
    temporary = _run_evidence_path(run_directory, '.developer-handoff.json')
    canonical = _manifest_evidence_path(
        run_directory, 'developer_handoff', sequence + 1
    )
    response_path, is_temporary = _recovery_response_path(temporary, canonical)
    if action is not RecoveryAction.APPLY_CONCLUSION:
        record = _prepare_recovered_validation(
            run_directory=run_directory,
            sequence=sequence,
            role='developer',
            record=record,
            response_present=response_path.is_file(),
        )
    if record.conclusion not in {None, 'succeeded'}:
        _raise_recovered_conclusion(
            store=store, run=run, role='developer', record=record
        )
    review_result_path = Path(request['payload']['review_result_path']).resolve()
    review_result = _read_object(review_result_path)
    finding_ids = tuple(
        finding['finding_id'] for finding in review_result['payload']['findings']
    )
    try:
        handoff = _read_object(response_path)
        if is_temporary:
            _require_unique_message_id(handoff, run_directory)
        parsed = _validate_developer_handoff(
            handoff, request=request, finding_ids=finding_ids
        )
        measured_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
        is_disagreement = (
            parsed.payload.status == 'ready_for_review'
            and measured_digest is not None
            and same_diff_digest(measured_digest, run.diff_digest)
            and _is_developer_disagreement(parsed)
        )
        if is_disagreement:
            assert measured_digest is not None
            recoverable, new_digest = False, measured_digest
        else:
            recoverable, new_digest = _classify_remediation_progress(
                parsed.payload.status, measured_digest, run.diff_digest or ''
            )
    except WorkerError:
        if record.status != 'completed':
            _complete_recovered_validation(
                run_directory=run_directory,
                sequence=sequence,
                role='developer',
                record=record,
                conclusion=AttemptConclusion.FAILED,
            )
        if response_path.exists():
            destination = _run_evidence_path(
                run_directory,
                'logs',
                f'{sequence + 1:06d}-rejected-developer-handoff-attempt-'
                f'{record.attempt:04d}.json',
            )
            _finalize_temporary_path(
                response_path, destination, 'rejected_developer_handoff'
            )
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.DEVELOPING)
        raise
    if is_temporary:
        _finalize_temporary_path(response_path, canonical, 'developer_handoff')
    _complete_recovered_validation(
        run_directory=run_directory,
        sequence=sequence,
        role='developer',
        record=record,
        conclusion=AttemptConclusion.SUCCEEDED,
    )
    if is_disagreement:
        disagreement = transition(run, RunState.CHANGES_REQUESTED)
        store.update(disagreement, expected_state=RunState.DEVELOPING)
        return disagreement
    if recoverable:
        validation_required = replace(
            transition(run, RunState.VALIDATION_REQUIRED),
            diff_digest=new_digest,
            updated_at=utc_now(),
        )
        store.update(validation_required, expected_state=RunState.DEVELOPING)
        return validation_required
    reviewing = replace(
        transition(run, RunState.REVIEWING),
        diff_digest=new_digest,
        updated_at=utc_now(),
    )
    store.update(reviewing, expected_state=RunState.DEVELOPING)
    return _run_queued_review(
        context=context,
        plan=ReviewPlan.from_execution_record(
            execution,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
        ),
        run=reviewing,
        continuation_sequence=sequence + 2,
        continuation_prior_review_path=review_result_path,
    )


def _resume_active_attempt(
    *,
    context: WorkerContext,
    run: Run,
    chain: tuple[tuple[Path, dict[str, Any]], ...],
    execution: ExecutionRecordSchema,
    reviewer_identity: InvocationIdentity,
    developer_identity: InvocationIdentity,
) -> Run:
    """Recover an active workflow state from its latest durable task evidence."""
    runs_directory = context.runs_directory

    expected_type = (
        'review_request' if run.state is RunState.REVIEWING else 'remediation_request'
    )
    matching = [
        document
        for _path, document in chain
        if document.get('message_type') == expected_type
        and document.get('iteration') == run.iteration
    ]
    if not matching:
        message = 'active run has no matching durable request'
        raise WorkerError(message)
    request = matching[-1]
    role = 'reviewer' if run.state is RunState.REVIEWING else 'developer'
    sequence = int(request['sequence'])
    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    latest = _latest_task_attempt(run_directory, sequence, role)
    temporary = _run_evidence_path(
        run_directory,
        '.review-result.json' if role == 'reviewer' else '.developer-handoff.json',
    )
    canonical = _manifest_evidence_path(
        run_directory,
        'review_result' if role == 'reviewer' else 'developer_handoff',
        sequence + 1,
    )
    response_present = temporary.is_file() or canonical.is_file()
    action = recovery_action(
        latest,
        response_artifact_present=response_present,
        workflow_state=run.state,
    )
    if action is RecoveryAction.LAUNCH:
        if role == 'reviewer':
            return _run_queued_review(
                context=context,
                plan=ReviewPlan.from_execution_record(
                    execution,
                    reviewer_identity=reviewer_identity,
                    developer_identity=developer_identity,
                ),
                run=run,
                continuation_sequence=sequence,
                continuation_prior_review_path=None,
                retry_review_request=request,
            )
        return _resume_developer_request(
            context=context,
            plan=ReviewPlan.from_execution_record(
                execution,
                reviewer_identity=reviewer_identity,
                developer_identity=developer_identity,
            ),
            run=run,
            request=request,
            current_digest=run.diff_digest or '',
            allow_unchanged_ready=False,
            attempt=1,
        )
    if action is RecoveryAction.FAIL_ACTIVATION_UNCERTAIN or latest is None:
        message = 'cannot resume task with uncertain active attempt'
        raise WorkerError(
            message,
            code=RESUME_ACTIVATION_UNCERTAIN_CODE,
        )
    if action is RecoveryAction.NONE:
        return run
    if action not in {
        RecoveryAction.APPLY_CONCLUSION,
        RecoveryAction.PERSIST_RESPONSE_AND_VALIDATE,
        RecoveryAction.VALIDATE_RESPONSE,
    }:
        message = f'unsupported recovery action {action}'
        raise WorkerError(message)
    resume_validation = (
        _resume_reviewer_validation
        if role == 'reviewer'
        else _resume_developer_validation
    )
    return resume_validation(
        context=context,
        run=run,
        request=request,
        record=latest,
        action=action,
        execution=execution,
        reviewer_identity=reviewer_identity,
        developer_identity=developer_identity,
    )


def _resume_intermediate_state(
    *,
    context: WorkerContext,
    run: Run,
    run_directory: Path,
    chain: list[tuple[Path, dict[str, Any]]],
    execution: ExecutionRecordSchema,
    reviewer_identity: InvocationIdentity,
    developer_identity: InvocationIdentity,
    measured_digest: str,
) -> Run:
    """Continue one crash-stopped review decision without rerunning review."""
    store = context.store

    if run.state is RunState.APPROVED:
        last_message = chain[-1][1]
        if (
            last_message['message_type'] != 'review_result'
            or last_message['payload']['verdict'] != 'approved'
        ):
            message = 'approved run has no matching durable review result'
            raise WorkerError(message)
        if not same_diff_digest(measured_digest, run.diff_digest):
            message = 'resume scope changed since review approval'
            raise WorkerError(message, code=RESUME_SCOPE_CHANGED_CODE)
        awaiting = transition(run, RunState.AWAITING_COMMIT_AUTHORIZATION)
        store.update(awaiting, expected_state=RunState.APPROVED)
        return awaiting

    if not same_diff_digest(measured_digest, run.diff_digest):
        message = 'resume scope changed before remediation'
        raise WorkerError(message, code=RESUME_SCOPE_CHANGED_CODE)
    last_path, last_message = chain[-1]
    if (
        last_message['message_type'] == 'review_result'
        and last_message['payload']['verdict'] == 'changes_requested'
        and run.iteration >= execution.max_review_iterations
    ):
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.CHANGES_REQUESTED)
        raise WorkerError(ITERATION_LIMIT)
    if not execution.developer.command:
        raise WorkerError(
            f'job is not resumable from {run.state}',
            code=RUN_NOT_RESUMABLE_CODE,
        )
    if last_message['message_type'] == 'remediation_request':
        request = last_message
    elif (
        last_message['message_type'] == 'review_result'
        and last_message['payload']['verdict'] == 'changes_requested'
    ):
        sequence = int(last_message['sequence']) + 1
        request = {
            'schema_version': 1,
            'message_id': str(uuid4()),
            'in_reply_to': last_message['message_id'],
            'run_id': str(run.id),
            'sequence': sequence,
            'iteration': run.iteration,
            'message_type': 'remediation_request',
            'sender': 'orchestrator',
            'recipient': 'developer',
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
            'scope': last_message['scope'],
            'payload': {
                'objective': execution.objective,
                'allowed_actions': [],
                'timeout_seconds': execution.developer.timeout_seconds,
                'review_result_path': str(last_path),
                'review_artifact_path': last_message['payload']['artifact_path'],
            },
        }
        _validate_remediation_request(request, run_directory=run_directory)
        _write_json_atomic(
            _run_evidence_path(
                run_directory,
                *Path(evidence_path('remediation_request', ordinal=sequence)).parts,
            ),
            request,
            'remediation_request',
        )
    else:
        message = 'changes-requested run has no recoverable review decision'
        raise WorkerError(message)
    developing = transition(run, RunState.DEVELOPING)
    return _resume_developer_request(
        context=context,
        plan=ReviewPlan.from_execution_record(
            execution,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
        ),
        run=developing,
        request=request,
        current_digest=measured_digest,
        allow_unchanged_ready=False,
        attempt=_next_attempt(
            run_directory,
            int(request['sequence']),
            str(request['recipient']),
            run.state,
        ),
        resume_expected_state=RunState.CHANGES_REQUESTED,
    )


def _resume_review(
    *,
    context: WorkerContext,
    run: Run,
) -> Run:
    """Resume one recoverable run from its canonical execution evidence."""
    store = context.store
    runs_directory = context.runs_directory
    digest_worktree = context.digest_worktree
    registry = context.registry

    if run.state not in {
        RunState.VALIDATION_REQUIRED,
        RunState.INTERRUPTED,
        RunState.REVIEWING,
        RunState.DEVELOPING,
        RunState.CHANGES_REQUESTED,
        RunState.APPROVED,
    }:
        raise WorkerError(
            f'job is not resumable from {run.state}', code=RUN_NOT_RESUMABLE_CODE
        )
    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)
    execution = _read_execution_record(run_directory, str(run.id))
    if isinstance(execution, ReviewerSetExecutionRecordSchema):
        message = 'reviewer-set execution resume is not implemented'
        raise WorkerError(message, code=RESUME_REVIEWER_SET_UNSUPPORTED_CODE)
    chain = read_message_chain(run_directory.resolve(), str(run.id))
    if not execution.reviewer.command:
        message = 'resume reviewer command is missing'
        raise WorkerError(message)
    reviewer_identity = _identity_from_record(
        execution.reviewer.identity.vendor,
        execution.reviewer.identity.model,
        execution.reviewer.identity.runtime,
    )
    developer_identity = _identity_from_record(
        execution.developer.identity.vendor,
        execution.developer.identity.model,
        execution.developer.identity.runtime,
    )
    if run.state is not RunState.APPROVED:
        reviewer_identity = _resolve_resume_identity(
            reviewer_identity, RuntimeRole.REVIEWER, registry
        )
        if execution.developer.command:
            developer_identity = _resolve_resume_identity(
                developer_identity, RuntimeRole.DEVELOPER, registry
            )
    measured_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
    if measured_digest is None:
        raise WorkerError(NO_CHANGES)

    if run.state in {RunState.APPROVED, RunState.CHANGES_REQUESTED}:
        return _resume_intermediate_state(
            context=context,
            run=run,
            run_directory=run_directory,
            chain=chain,
            execution=execution,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
            measured_digest=measured_digest,
        )

    if run.state in {RunState.REVIEWING, RunState.DEVELOPING}:
        if run.state is RunState.REVIEWING and not same_diff_digest(
            measured_digest, run.diff_digest
        ):
            message = 'resume scope changed during active task recovery'
            raise WorkerError(message, code=RESUME_SCOPE_CHANGED_CODE)
        return _resume_active_attempt(
            context=context,
            run=run,
            chain=tuple(chain),
            execution=execution,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
        )

    if run.state is RunState.INTERRUPTED:
        origin = store.interrupted_origin(str(run.id))
        _, request = chain[-1]
        expected_type = (
            'review_request' if origin is RunState.REVIEWING else 'remediation_request'
        )
        if (
            origin not in {RunState.REVIEWING, RunState.DEVELOPING}
            or request.get('message_type') != expected_type
        ):
            message = 'interrupted evidence does not match its origin state'
            raise WorkerError(message)
        if origin is RunState.REVIEWING and not same_diff_digest(
            measured_digest, run.diff_digest
        ):
            message = 'resume scope changed since the interrupted review'
            raise WorkerError(message, code=RESUME_SCOPE_CHANGED_CODE)
        if origin is RunState.DEVELOPING and not execution.developer.command:
            message = 'resume developer command is missing'
            raise WorkerError(message)
        attempt = _next_attempt(
            run_directory,
            int(request['sequence']),
            str(request['recipient']),
            run.state,
        )
        resumed = replace(run, state=origin, updated_at=utc_now())
        if origin is RunState.REVIEWING:
            return _run_queued_review(
                context=context,
                plan=ReviewPlan.from_execution_record(
                    execution,
                    reviewer_identity=reviewer_identity,
                    developer_identity=developer_identity,
                ),
                run=resumed,
                continuation_sequence=int(request['sequence']),
                continuation_prior_review_path=None,
                retry_review_request=request,
                reviewer_attempt=attempt,
                resume_expected_state=RunState.INTERRUPTED,
            )
        previous_message = chain[-2][1] if len(chain) > 1 else None
        retrying_recovery_request = (
            previous_message is not None
            and previous_message['message_type'] == 'developer_handoff'
            and previous_message['payload']['status'] in {'blocked', 'failed'}
        )
        return _resume_developer_request(
            context=context,
            plan=ReviewPlan.from_execution_record(
                execution,
                reviewer_identity=reviewer_identity,
                developer_identity=developer_identity,
            ),
            run=resumed,
            request=request,
            current_digest=run.diff_digest or '',
            allow_unchanged_ready=retrying_recovery_request,
            attempt=attempt,
            resume_expected_state=RunState.INTERRUPTED,
        )

    if not same_diff_digest(measured_digest, run.diff_digest):
        message = 'resume scope changed since validation became required'
        raise WorkerError(message, code=RESUME_SCOPE_CHANGED_CODE)
    if not execution.developer.command:
        message = 'resume developer command is missing'
        raise WorkerError(message)
    if chain[-1][1]['message_type'] == 'remediation_request':
        request = chain[-1][1]
        attempt = _next_attempt(
            run_directory,
            int(request['sequence']),
            str(request['recipient']),
            run.state,
        )
        resumed = transition(run, RunState.DEVELOPING)
        return _resume_developer_request(
            context=context,
            plan=ReviewPlan.from_execution_record(
                execution,
                reviewer_identity=reviewer_identity,
                developer_identity=developer_identity,
            ),
            run=resumed,
            request=request,
            current_digest=measured_digest,
            allow_unchanged_ready=True,
            attempt=attempt,
            resume_expected_state=RunState.VALIDATION_REQUIRED,
        )
    if chain[-1][1]['message_type'] != 'developer_handoff':
        message = 'validation-required evidence is incomplete'
        raise WorkerError(message)
    last_handoff = DeveloperHandoffMessageSchema.model_validate(chain[-1][1])
    entries_by_id = {
        document['message_id']: (path, document) for path, document in chain
    }
    remediation_entry = entries_by_id.get(last_handoff.in_reply_to)
    if remediation_entry is None:
        message = 'validation-required evidence is incomplete'
        raise WorkerError(message)
    review_result_entry = entries_by_id.get(remediation_entry[1]['in_reply_to'])
    if review_result_entry is None:
        message = 'validation-required evidence is incomplete'
        raise WorkerError(message)
    review_result_path, review_result = review_result_entry
    if last_handoff.payload.status not in {'blocked', 'failed'}:
        message = 'validation-required handoff is not recoverable'
        raise WorkerError(message)
    sequence = len(chain) + 1
    request_path = _run_evidence_path(
        run_directory,
        *Path(evidence_path('remediation_request', ordinal=sequence)).parts,
    )
    recovery_request: dict[str, Any] = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': review_result['message_id'],
        'run_id': str(run.id),
        'sequence': sequence,
        'iteration': run.iteration,
        'message_type': 'remediation_request',
        'sender': 'orchestrator',
        'recipient': 'developer',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': review_result['scope'],
        'payload': {
            'objective': execution.objective,
            'allowed_actions': [],
            'timeout_seconds': execution.developer.timeout_seconds,
            'review_result_path': str(review_result_path),
            'review_artifact_path': review_result['payload']['artifact_path'],
        },
    }
    _validate_remediation_request(recovery_request, run_directory=run_directory)
    _write_json_atomic(request_path, recovery_request, 'remediation_request')
    resumed = transition(run, RunState.DEVELOPING)
    return _resume_developer_request(
        context=context,
        plan=ReviewPlan.from_execution_record(
            execution,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
        ),
        run=resumed,
        request=recovery_request,
        current_digest=measured_digest,
        allow_unchanged_ready=True,
        attempt=1,
        resume_expected_state=RunState.VALIDATION_REQUIRED,
    )


def resume_review(
    *,
    store: JobStore,
    run: Run,
    runs_directory: Path,
    digest_worktree: Callable[[Path, str], str | None],
    registry: RuntimeRegistry = DEFAULT_RUNTIME_REGISTRY,
) -> Run:
    """Resume one run and persist any recoverable-command failure."""

    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    try:
        return _resume_review(
            context=WorkerContext(
                store=store,
                runs_directory=runs_directory,
                digest_worktree=digest_worktree,
                registry=registry,
            ),
            run=run,
        )
    except WorkerError as error:
        if not run_directory.is_relative_to(run.worktree_path.resolve()):
            try:
                durable_run = store.get(str(run.id))
                _write_json_atomic(
                    _run_evidence_path(run_directory, 'failure.json'),
                    {
                        'schema_version': 1,
                        'run_id': str(run.id),
                        'state': str(durable_run.state),
                        'error': {
                            'code': error.code or 'worker_error',
                            'message': str(error),
                        },
                        'created_at': datetime.now(UTC)
                        .isoformat()
                        .replace('+00:00', 'Z'),
                    },
                    'failure',
                )
            except OSError:
                pass
        raise


def _reviewer_dispatch_path(run_directory: Path, relative: str) -> Path:
    """Resolve one reviewer-owned relative path beneath the run directory."""

    return _run_evidence_path(run_directory, *Path(relative).parts)


def _execute_reviewer_dispatch(
    *,
    context: WorkerContext,
    run: Run,
    reviewing: Run,
    objective: str,
    current_digest: str,
    dispatch: ReviewerDispatch,
    sequence: int,
    attempt: int,
) -> ReviewerDispatchResult:
    """Execute and validate one reviewer without changing workflow state."""

    run_directory = _run_evidence_directory(context.runs_directory, str(run.id))
    logs = _run_evidence_path(run_directory, 'logs')
    invocations = _run_evidence_path(run_directory, 'invocations')
    request_path = _reviewer_dispatch_path(run_directory, dispatch.paths.request)
    response_path = _reviewer_dispatch_path(
        run_directory, dispatch.paths.temporary_result
    )
    result_path = _reviewer_dispatch_path(run_directory, dispatch.paths.result)
    artifact_path = _reviewer_dispatch_path(run_directory, dispatch.paths.artifact)
    stdout_path = _reviewer_dispatch_path(run_directory, dispatch.paths.stdout)
    stderr_path = _reviewer_dispatch_path(run_directory, dispatch.paths.stderr)
    metadata_path = _reviewer_dispatch_path(
        run_directory, dispatch.paths.runtime_metadata
    )
    request: dict[str, Any] = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': None,
        'run_id': str(run.id),
        'sequence': sequence,
        'iteration': reviewing.iteration,
        'message_type': 'review_request',
        'sender': 'orchestrator',
        'recipient': 'reviewer',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': {
            'worktree_path': str(run.worktree_path),
            'base_sha': run.base_sha,
            'head_sha': run.head_sha,
            'diff_digest': current_digest,
        },
        'payload': {
            'objective': objective,
            'allowed_actions': [],
            'timeout_seconds': dispatch.timeout_seconds,
            'artifact_path': str(artifact_path),
            'prior_review_path': None,
        },
    }
    _validate_review_request(request, run_directory=run_directory)
    _write_json_atomic(request_path, request, 'review_request')
    started_at = timestamp()
    invocation_id = _record_invocation(
        run=run,
        role='reviewer',
        reviewer_id=dispatch.reviewer_id,
        identity=dispatch.identity,
        iteration=reviewing.iteration,
        sequence=request['sequence'],
        started_at=started_at,
        logs=logs,
        invocations=invocations,
        stdout='',
        stderr='',
        exit_code=None,
        finished=False,
        attempt=attempt,
        invocation_id=dispatch.invocation_id,
    )
    adapter = CommandAgentAdapter(dispatch.command)
    try:
        completed = adapter.execute(
            ReviewerRequest(
                objective=objective,
                worktree_path=run.worktree_path,
                iteration=reviewing.iteration,
                allowed_actions=(),
                timeout_seconds=dispatch.timeout_seconds,
                base_sha=run.base_sha,
                head_sha=run.head_sha,
                diff_digest=current_digest,
                artifact_path=artifact_path,
                request_path=request_path,
                response_path=response_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                runtime_metadata_path=_runtime_metadata_path(
                    dispatch.identity, metadata_path, context.registry
                ),
                on_started=partial(
                    _record_invocation,
                    run=run,
                    role='reviewer',
                    reviewer_id=dispatch.reviewer_id,
                    identity=dispatch.identity,
                    iteration=reviewing.iteration,
                    sequence=request['sequence'],
                    started_at=started_at,
                    logs=logs,
                    invocations=invocations,
                    stdout=None,
                    stderr=None,
                    exit_code=None,
                    invocation_id=invocation_id,
                    finished=False,
                    attempt=attempt,
                    status='running',
                ),
            )
        )
    except subprocess.TimeoutExpired as error:
        models, model_status = _exception_runtime_metadata(error)
        _record_invocation(
            run=run,
            role='reviewer',
            reviewer_id=dispatch.reviewer_id,
            identity=dispatch.identity,
            iteration=reviewing.iteration,
            sequence=request['sequence'],
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=error.stdout,
            stderr=error.stderr,
            exit_code=None,
            timed_out=True,
            invocation_id=invocation_id,
            attempt=attempt,
            effective_models=models,
            effective_model_status=model_status,
        )
        _archive_unaccepted_response(
            response_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-result-attempt-{attempt:04d}.json',
            'rejected_review_result',
        )
        _archive_unaccepted_response(
            artifact_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-artifact-attempt-{attempt:04d}.md',
            'rejected_review_artifact',
        )
        return ReviewerDispatchResult(
            ReviewerDecision(dispatch.reviewer_id, 'incomplete'), None
        )
    except OSError as error:
        _record_invocation(
            run=run,
            role='reviewer',
            reviewer_id=dispatch.reviewer_id,
            identity=dispatch.identity,
            iteration=reviewing.iteration,
            sequence=request['sequence'],
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout='',
            stderr=str(error),
            exit_code=None,
            invocation_id=invocation_id,
            attempt=attempt,
            conclusion='failed',
        )
        _archive_unaccepted_response(
            response_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-result-attempt-{attempt:04d}.json',
            'rejected_review_result',
        )
        _archive_unaccepted_response(
            artifact_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-artifact-attempt-{attempt:04d}.md',
            'rejected_review_artifact',
        )
        return ReviewerDispatchResult(
            ReviewerDecision(dispatch.reviewer_id, 'incomplete'), None
        )
    except BaseException as error:
        _record_invocation(
            run=run,
            role='reviewer',
            reviewer_id=dispatch.reviewer_id,
            identity=dispatch.identity,
            iteration=reviewing.iteration,
            sequence=request['sequence'],
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout='',
            stderr=str(error),
            exit_code=None,
            invocation_id=invocation_id,
            attempt=attempt,
            conclusion='failed',
        )
        _archive_unaccepted_response(
            response_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-result-attempt-{attempt:04d}.json',
            'rejected_review_result',
        )
        _archive_unaccepted_response(
            artifact_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-artifact-attempt-{attempt:04d}.md',
            'rejected_review_artifact',
        )
        raise
    finished_at = timestamp()
    if not completed.succeeded:
        _record_invocation(
            run=run,
            role='reviewer',
            reviewer_id=dispatch.reviewer_id,
            identity=dispatch.identity,
            iteration=reviewing.iteration,
            sequence=request['sequence'],
            started_at=started_at,
            logs=logs,
            invocations=invocations,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.exit_code,
            invocation_id=invocation_id,
            attempt=attempt,
            conclusion='failed',
            finished_at_value=finished_at,
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        )
        _archive_unaccepted_response(
            response_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-result-attempt-{attempt:04d}.json',
            'rejected_review_result',
        )
        _archive_unaccepted_response(
            artifact_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-artifact-attempt-{attempt:04d}.md',
            'rejected_review_artifact',
        )
        return ReviewerDispatchResult(
            ReviewerDecision(dispatch.reviewer_id, 'incomplete'), None
        )

    if artifact_path.is_file():
        _record_finalized_path(artifact_path, 'review_artifact')
    received_at = timestamp() if response_path.is_file() else None
    validation_started_at = timestamp()
    try:
        response = _read_object(response_path)
        _require_unique_message_id(response, run_directory)
        verdict = _validate_review_response(
            response, request=request, artifact_path=artifact_path
        )
    except WorkerError:
        verdict = 'blocked'
        response = {}
    valid = verdict != 'blocked' or bool(response)
    if response_path.exists():
        destination = (
            result_path
            if valid
            else logs / f'{dispatch.reviewer_id}-rejected-review-result.json'
        )
        _finalize_temporary_path(
            response_path,
            destination,
            'review_result' if valid else 'rejected_review_result',
        )
    _record_invocation(
        run=run,
        role='reviewer',
        reviewer_id=dispatch.reviewer_id,
        identity=dispatch.identity,
        iteration=reviewing.iteration,
        sequence=request['sequence'],
        started_at=started_at,
        logs=logs,
        invocations=invocations,
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=completed.exit_code,
        invocation_id=invocation_id,
        attempt=attempt,
        conclusion='succeeded' if valid else 'failed',
        response_received_at=received_at,
        validation_started_at=validation_started_at if received_at else None,
        finished_at_value=finished_at,
        effective_models=completed.effective_models,
        effective_model_status=completed.effective_model_status,
    )
    message_id = response.get('message_id')
    return ReviewerDispatchResult(
        ReviewerDecision(
            dispatch.reviewer_id, cast('Any', verdict if valid else 'incomplete')
        ),
        message_id if isinstance(message_id, str) else None,
    )


def _run_queued_reviewer_set(
    *,
    store: JobStore,
    run: Run,
    objective: str,
    reviewer_plan: ReviewerExecutionPlan,
    developer_command: Sequence[str],
    runs_directory: Path,
    developer_timeout_seconds: int,
    max_iterations: int,
    digest_worktree: Callable[[Path, str], str | None],
    developer_identity: InvocationIdentity,
    registry: RuntimeRegistry = DEFAULT_RUNTIME_REGISTRY,
) -> Run:
    """Run one concurrent required-reviewer batch for a queued immutable diff."""

    if run.state is not RunState.QUEUED:
        raise WorkerError(f'run must be queued, found {run.state}')
    if not objective.strip():
        raise WorkerError(EMPTY_OBJECTIVE)
    if developer_timeout_seconds <= 0:
        raise WorkerError(INVALID_DEVELOPER_TIMEOUT)
    if max_iterations <= 0:
        raise WorkerError(INVALID_ITERATION_LIMIT)
    context = WorkerContext(store, runs_directory, digest_worktree, registry)
    resolved_reviewers = tuple(
        replace(
            reviewer,
            identity=_resolve_resume_identity(
                reviewer.identity, RuntimeRole.REVIEWER, registry
            ),
        )
        for reviewer in reviewer_plan.reviewers
    )
    reviewer_plan = replace(reviewer_plan, reviewers=resolved_reviewers)
    developer_identity = _resolve_resume_identity(
        developer_identity, RuntimeRole.DEVELOPER, registry
    )
    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)
    current_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
    if current_digest is None:
        raise WorkerError(NO_CHANGES)
    prepared = replace(
        transition(run, RunState.PREPARING),
        diff_digest=current_digest,
        updated_at=utc_now(),
    )
    store.update(prepared, expected_state=RunState.QUEUED)
    plan = ReviewerSetReviewPlan(
        objective,
        reviewer_plan,
        developer_command,
        developer_timeout_seconds,
        max_iterations,
        developer_identity,
    )
    try:
        execution = _execution_record(
            plan,
            run_id=str(run.id),
            created_at=datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        )
        _write_json_atomic(
            _run_evidence_path(run_directory, 'execution.json'),
            execution.model_dump(mode='json'),
            'execution',
        )
        reviewing = transition(prepared, RunState.REVIEWING)
        store.update(reviewing, expected_state=RunState.PREPARING)
    except BaseException:
        failed = transition(prepared, RunState.FAILED)
        store.update(failed, expected_state=RunState.PREPARING)
        raise
    try:
        for directory in ('artifacts', 'logs', 'invocations'):
            _run_evidence_path(run_directory, directory).mkdir(
                parents=True, exist_ok=True
            )
        dispatches = build_review_fanout(
            reviewer_plan,
            run_id=str(run.id),
            sequence=1,
            iteration=reviewing.iteration,
            attempt=1,
        )
        with ThreadPoolExecutor(max_workers=len(dispatches)) as executor:
            results = tuple(
                executor.map(
                    lambda dispatch: _execute_reviewer_dispatch(
                        context=context,
                        run=run,
                        reviewing=reviewing,
                        objective=objective,
                        current_digest=current_digest,
                        dispatch=dispatch,
                        sequence=1,
                        attempt=1,
                    ),
                    dispatches,
                )
            )
        _require_unchanged(
            _digest(digest_worktree, run.worktree_path, run.base_sha), current_digest
        )
        _require_unique_batch_message_ids(results)
        decision = aggregate_review_batch(tuple(result.decision for result in results))
        batch_result = ReviewerBatchResultSchema.model_validate(
            {
                'schema_version': 1,
                'run_id': str(run.id),
                'iteration': reviewing.iteration,
                'reviewer_set_id': reviewer_plan.reviewer_set_id,
                'aggregation_policy': 'all_required',
                'diff_digest': current_digest,
                'verdict': decision.verdict,
                'reviewers': [
                    {
                        'reviewer_id': dispatch.reviewer_id,
                        'outcome': result.decision.outcome,
                        'result_path': (
                            dispatch.paths.result
                            if result.message_id is not None
                            else None
                        ),
                    }
                    for dispatch, result in zip(dispatches, results, strict=True)
                ],
                'changes_requested_by': list(decision.changes_requested_by),
                'blocked_by': list(decision.blocked_by),
                'incomplete_reviewers': list(decision.incomplete_reviewers),
            }
        )
        _write_json_atomic(
            _run_evidence_path(
                run_directory,
                *Path(
                    evidence_path('review_batch_result', ordinal=reviewing.iteration)
                ).parts,
            ),
            batch_result.model_dump(mode='json'),
            'review_batch_result',
        )
    except BaseException:
        failed = transition(reviewing, RunState.FAILED)
        store.update(failed, expected_state=RunState.REVIEWING)
        raise
    if decision.verdict == 'blocked':
        failed = transition(reviewing, RunState.FAILED)
        store.update(failed, expected_state=RunState.REVIEWING)
        raise WorkerError(
            REVIEWER_BATCH_INCOMPLETE, code=REVIEWER_BATCH_INCOMPLETE_CODE
        )
    decided = transition(
        reviewing,
        RunState.APPROVED
        if decision.verdict == 'approved'
        else RunState.CHANGES_REQUESTED,
    )
    store.update(decided, expected_state=RunState.REVIEWING)
    if decision.verdict == 'changes_requested':
        return decided
    awaiting = transition(decided, RunState.AWAITING_COMMIT_AUTHORIZATION)
    store.update(awaiting, expected_state=RunState.APPROVED)
    return awaiting


def run_queued_reviewer_set(
    *,
    store: JobStore,
    run: Run,
    objective: str,
    reviewer_plan: ReviewerExecutionPlan,
    developer_command: Sequence[str],
    runs_directory: Path,
    developer_timeout_seconds: int,
    max_iterations: int,
    digest_worktree: Callable[[Path, str], str | None],
    developer_identity: InvocationIdentity,
    registry: RuntimeRegistry = DEFAULT_RUNTIME_REGISTRY,
) -> Run:
    """Run a reviewer batch and persist every worker failure as durable evidence."""

    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    try:
        return _run_queued_reviewer_set(
            store=store,
            run=run,
            objective=objective,
            reviewer_plan=reviewer_plan,
            developer_command=developer_command,
            runs_directory=runs_directory,
            developer_timeout_seconds=developer_timeout_seconds,
            max_iterations=max_iterations,
            digest_worktree=digest_worktree,
            developer_identity=developer_identity,
            registry=registry,
        )
    except WorkerError as error:
        if not run_directory.is_relative_to(run.worktree_path.resolve()):
            try:
                durable_run = store.get(str(run.id))
                _write_json_atomic(
                    _run_evidence_path(run_directory, 'failure.json'),
                    {
                        'schema_version': 1,
                        'run_id': str(run.id),
                        'state': str(durable_run.state),
                        'error': {
                            'code': error.code or 'worker_error',
                            'message': str(error),
                        },
                        'created_at': datetime.now(UTC)
                        .isoformat()
                        .replace('+00:00', 'Z'),
                    },
                    'failure',
                )
            except OSError:
                pass
        raise


def run_queued_review(
    *,
    store: JobStore,
    run: Run,
    objective: str,
    reviewer_command: Sequence[str],
    developer_command: Sequence[str],
    runs_directory: Path,
    timeout_seconds: int,
    developer_timeout_seconds: int | None = None,
    max_iterations: int = 3,
    digest_worktree: Callable[[Path, str], str | None],
    reviewer_identity: InvocationIdentity | None = None,
    developer_identity: InvocationIdentity | None = None,
    registry: RuntimeRegistry = DEFAULT_RUNTIME_REGISTRY,
) -> Run:
    """Run the bounded loop and persist every worker failure as durable evidence."""

    run_directory = _run_evidence_directory(runs_directory, str(run.id))
    reviewer_identity = reviewer_identity or InvocationIdentity(
        vendor='unknown', model=None, runtime='custom-command'
    )
    developer_identity = developer_identity or InvocationIdentity(
        vendor='unknown', model=None, runtime='custom-command'
    )
    try:
        return _run_queued_review(
            context=WorkerContext(
                store=store,
                runs_directory=runs_directory,
                digest_worktree=digest_worktree,
                registry=registry,
            ),
            plan=ReviewPlan(
                objective=objective,
                reviewer_command=reviewer_command,
                developer_command=developer_command,
                timeout_seconds=timeout_seconds,
                developer_timeout_seconds=developer_timeout_seconds,
                max_iterations=max_iterations,
                reviewer_identity=reviewer_identity,
                developer_identity=developer_identity,
            ),
            run=run,
        )
    except WorkerError as error:
        if not run_directory.is_relative_to(run.worktree_path.resolve()):
            try:
                durable_run = store.get(str(run.id))
                _write_json_atomic(
                    _run_evidence_path(run_directory, 'failure.json'),
                    {
                        'schema_version': 1,
                        'run_id': str(run.id),
                        'state': str(durable_run.state),
                        'error': {
                            'code': error.code or 'worker_error',
                            'message': str(error),
                        },
                        'created_at': datetime.now(UTC)
                        .isoformat()
                        .replace('+00:00', 'Z'),
                    },
                    'failure',
                )
            except OSError:
                pass
        raise
