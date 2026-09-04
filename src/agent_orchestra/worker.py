"""Execute one durable, bounded local review step."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from agent_orchestra.agents import (
    CommandAgentAdapter,
    DeveloperRequest,
    ReviewerRequest,
)
from agent_orchestra.invocations import (
    InvocationIdentity,
    InvocationRecord,
    timestamp,
    write_record,
)
from agent_orchestra.models import Run, RunState, same_diff_digest, utc_now
from agent_orchestra.schemas import (
    CHANGES_REQUESTED_WITHOUT_FINDINGS,
    DUPLICATE_REVIEW_FINDING_IDS,
    DeveloperHandoffMessageSchema,
    ExecutionRecordSchema,
    RemediationRequestMessageSchema,
    ReviewRequestMessageSchema,
    ReviewResultMessageSchema,
)
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agent_orchestra.store import RunStore


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
RUN_NOT_RESUMABLE_CODE = 'run_not_resumable'
RESUME_METADATA_UNSUPPORTED_CODE = 'resume_metadata_unsupported'
RESUME_SCOPE_CHANGED_CODE = 'resume_scope_changed'
RESUME_INTERRUPTED_CODE = 'resume_interrupted'
RESUME_EXECUTION_FAILED_CODE = 'resume_execution_failed'
RUNTIME_METADATA_RUNTIMES = frozenset({'codex', 'claude-code'})


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


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Write one UTF-8 JSON document atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text_atomic(path: Path, content: str) -> None:
    """Write one UTF-8 text artifact atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _archive_unaccepted_response(path: Path, destination: Path) -> None:
    """Preserve a partial response so a retry cannot consume stale output."""

    if path.exists():
        path.replace(destination)


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


def _runtime_metadata_path(identity: InvocationIdentity, path: Path) -> Path | None:
    """Return the sidecar path only for runtimes that report provenance."""

    return path if identity.runtime in RUNTIME_METADATA_RUNTIMES else None


def _invocation_stem(sequence: int, role: str, attempt: int) -> str:
    """Return the stable evidence stem for one invocation attempt."""

    retry_suffix = '' if attempt == 1 else f'-attempt-{attempt:04d}'
    return f'{sequence:06d}-{role}{retry_suffix}'


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
) -> str:
    """Persist separate streams and their adapter-neutral invocation record."""

    invocation_id = invocation_id or str(uuid4())
    log_stem = _invocation_stem(sequence, role, attempt)
    stdout_path = logs / f'{log_stem}.stdout.log'
    stderr_path = logs / f'{log_stem}.stderr.log'
    if stdout is not None or not stdout_path.exists():
        _write_text_atomic(stdout_path, _output_text(stdout))
    if stderr is not None or not stderr_path.exists():
        _write_text_atomic(stderr_path, _output_text(stderr))
    write_record(
        invocations / f'{log_stem}.json',
        InvocationRecord(
            schema_version=3,
            run_id=str(run.id),
            invocation_id=invocation_id,
            role=role,
            agent_vendor=identity.vendor,
            requested_model=identity.model,
            effective_models=effective_models,
            effective_model_status=effective_model_status,
            runtime=identity.runtime,
            iteration=iteration,
            started_at=started_at,
            finished_at=timestamp() if finished else None,
            exit_code=exit_code,
            timed_out=timed_out,
            interrupted=interrupted,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            attempt=attempt,
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
    root = run_directory.resolve()
    evidence_paths = (
        Path(parsed.payload.review_result_path).resolve(),
        Path(parsed.payload.review_artifact_path).resolve(),
    )
    if any(not path.is_relative_to(root) for path in evidence_paths):
        raise WorkerError(REMEDIATION_PATH_ESCAPE)
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
    root = run_directory.resolve()
    artifact_path = Path(parsed.payload.artifact_path).resolve()
    if not artifact_path.is_relative_to(root):
        raise WorkerError(REVIEW_PATH_ESCAPE)
    if parsed.payload.prior_review_path is not None:
        prior_path = Path(parsed.payload.prior_review_path).resolve()
        if not prior_path.is_relative_to(root):
            raise WorkerError(REVIEW_PATH_ESCAPE)
        if not prior_path.is_file():
            raise WorkerError(INVALID_REVIEW_REQUEST)


def _require_unique_message_id(document: dict[str, Any], messages: Path) -> None:
    """Reject a response identifier already present in durable messages."""

    candidate = document.get('message_id')
    for path in messages.glob('*.json'):
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


def _read_execution_record(run_directory: Path, run_id: str) -> ExecutionRecordSchema:
    """Read and validate the durable execution context for a resumable run."""

    path = run_directory / 'execution.json'
    try:
        document = _read_object(path)
        record = ExecutionRecordSchema.model_validate(document)
    except (WorkerError, ValidationError) as error:
        message = 'resume metadata is missing, legacy, or invalid'
        raise WorkerError(message, code=RESUME_METADATA_UNSUPPORTED_CODE) from error
    if record.run_id != run_id:
        message = 'resume metadata does not match the run ID'
        raise WorkerError(message)
    return record


def _read_message_chain(
    run_directory: Path, run_id: str
) -> list[tuple[Path, dict[str, Any]]]:
    """Read and correlate every canonical message for recovery."""

    messages = run_directory / 'messages'
    if messages.is_symlink() or not messages.is_dir():
        message = 'resume message directory is missing or unsafe'
        raise WorkerError(message)
    documents: list[tuple[Path, dict[str, Any]]] = []
    identities: dict[str, tuple[Path, dict[str, Any]]] = {}
    schemas: dict[str, type[BaseModel]] = {
        'review_request': ReviewRequestMessageSchema,
        'review_result': ReviewResultMessageSchema,
        'remediation_request': RemediationRequestMessageSchema,
        'developer_handoff': DeveloperHandoffMessageSchema,
    }
    for expected_sequence, path in enumerate(sorted(messages.glob('*.json')), start=1):
        if path.is_symlink() or not path.resolve().is_relative_to(run_directory):
            message = 'resume message path escapes the run directory'
            raise WorkerError(message)
        try:
            sequence = int(path.name.split('-', 1)[0])
        except ValueError as error:
            raise WorkerError(f'invalid message filename: {path.name}') from error
        if sequence != expected_sequence:
            message = 'resume message sequence is not contiguous'
            raise WorkerError(message)
        document = _read_object(path)
        message_type = document.get('message_type')
        if not isinstance(message_type, str):
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
            resolved_artifact = artifact_path.resolve()
            if (
                document['payload']['artifact_path']
                != parent['payload']['artifact_path']
                or artifact_path.is_symlink()
                or not resolved_artifact.is_relative_to(run_directory)
                or not artifact_path.is_file()
            ):
                raise WorkerError(f'invalid message correlation: {path.name}')
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


def _identity_from_record(
    vendor: str, model: str | None, runtime: str
) -> InvocationIdentity:
    """Convert persisted execution identity fields into the runtime value."""

    return InvocationIdentity(vendor=vendor, model=model, runtime=runtime)


def _next_attempt(run_directory: Path, sequence: int, role: str) -> int:
    """Return the next non-overwriting invocation attempt number."""

    pattern = f'{sequence:06d}-{role}*.json'
    attempts: list[int] = []
    for path in (run_directory / 'invocations').glob(pattern):
        try:
            document = _read_object(path)
            attempts.append(int(document.get('attempt', 1)))
        except (WorkerError, TypeError, ValueError) as error:
            raise WorkerError(f'invalid invocation evidence: {path.name}') from error
    return max(attempts, default=0) + 1


def _run_queued_review(
    *,
    store: RunStore,
    run: Run,
    objective: str,
    reviewer_command: Sequence[str],
    developer_command: Sequence[str],
    runs_directory: Path,
    timeout_seconds: int,
    developer_timeout_seconds: int | None = None,
    max_iterations: int = 3,
    digest_worktree: Callable[[Path, str], str | None],
    reviewer_identity: InvocationIdentity,
    developer_identity: InvocationIdentity,
    continuation_sequence: int | None = None,
    continuation_prior_review_path: Path | None = None,
    retry_review_request: dict[str, Any] | None = None,
    reviewer_attempt: int = 1,
    resume_expected_state: RunState | None = None,
) -> Run:
    """Consume one queued local run through a bounded review-remediation loop."""

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

    run_directory = runs_directory.expanduser().resolve() / str(run.id)
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)

    current_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
    if current_digest is None:
        raise WorkerError(NO_CHANGES)
    messages = run_directory / 'messages'
    artifacts = run_directory / 'artifacts'
    logs = run_directory / 'logs'
    invocations = run_directory / 'invocations'
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
        execution_document: dict[str, Any] = {
            'schema_version': 2,
            'run_id': str(run.id),
            'objective': objective,
            'reviewer': {
                'command': list(reviewer_command),
                'identity': {
                    'vendor': reviewer_identity.vendor,
                    'model': reviewer_identity.model,
                    'runtime': reviewer_identity.runtime,
                },
                'timeout_seconds': timeout_seconds,
            },
            'developer': {
                'command': list(developer_command),
                'identity': {
                    'vendor': developer_identity.vendor,
                    'model': developer_identity.model,
                    'runtime': developer_identity.runtime,
                },
                'timeout_seconds': developer_timeout_seconds,
            },
            'max_review_iterations': max_iterations,
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        }
        try:
            ExecutionRecordSchema.model_validate(execution_document)
        except ValidationError as error:
            raise WorkerError(f'invalid execution record: {error}') from error
        _write_json_atomic(run_directory / 'execution.json', execution_document)
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
            request_path = messages / f'{sequence:06d}-review-request.json'
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
            _write_json_atomic(request_path, request)
        else:
            request = retry_review_request
            retry_review_request = None
            sequence = int(request['sequence'])
            artifact_path = Path(request['payload']['artifact_path'])
            request_path = messages / f'{sequence:06d}-review-request.json'
            _validate_review_request(request, run_directory=run_directory)
        response_path = run_directory / '.review-result.json'
        reviewer_stem = _invocation_stem(sequence, 'reviewer', reviewer_attempt)
        reviewer_metadata_path = run_directory / f'.{reviewer_stem}.runtime.json'
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
                        reviewer_identity, reviewer_metadata_path
                    ),
                )
            )
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
            )
            _archive_unaccepted_response(
                artifact_path,
                logs / f'{sequence + 1:06d}-rejected-review-artifact-attempt-'
                f'{reviewer_attempt:04d}.md',
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
            )
            _archive_unaccepted_response(
                artifact_path,
                logs / f'{sequence + 1:06d}-rejected-review-artifact-attempt-'
                f'{reviewer_attempt:04d}.md',
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
        )
        reviewer_attempt = 1
        if not completed.succeeded:
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise WorkerError(
                f'reviewer exited with code {completed.exit_code}',
                code=RESUME_EXECUTION_FAILED_CODE,
            )

        response_valid = False
        review_result_path = messages / f'{sequence + 1:06d}-review-result.json'
        try:
            response = _read_object(response_path)
            _require_unique_message_id(response, messages)
            verdict = _validate_review_response(
                response, request=request, artifact_path=artifact_path
            )
            _require_unchanged(
                _digest(digest_worktree, run.worktree_path, run.base_sha),
                current_digest,
            )
            response_valid = True
        except WorkerError:
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
                response_path.replace(destination)

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

        developing = transition(decided, RunState.DEVELOPING)
        store.update(developing, expected_state=RunState.CHANGES_REQUESTED)
        sequence += 2
        remediation_path = messages / f'{sequence:06d}-remediation-request.json'
        handoff_temporary = run_directory / '.developer-handoff.json'
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
        _write_json_atomic(remediation_path, remediation)
        developer_stem = _invocation_stem(sequence, 'developer', 1)
        developer_metadata_path = run_directory / f'.{developer_stem}.runtime.json'
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
                        developer_identity, developer_metadata_path
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
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        )
        if not completed.succeeded:
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise WorkerError(
                f'developer exited with code {completed.exit_code}',
                code=RESUME_EXECUTION_FAILED_CODE,
            )
        handoff_valid = False
        handoff_path = messages / f'{sequence + 1:06d}-developer-handoff.json'
        finding_ids = tuple(
            finding['finding_id'] for finding in response['payload']['findings']
        )
        try:
            handoff = _read_object(handoff_temporary)
            _require_unique_message_id(handoff, messages)
            parsed_handoff = _validate_developer_handoff(
                handoff, request=remediation, finding_ids=finding_ids
            )
            handoff_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
            if (
                parsed_handoff.payload.status == 'ready_for_review'
                and handoff_digest is not None
                and same_diff_digest(handoff_digest, current_digest)
                and _is_developer_disagreement(parsed_handoff)
            ):
                handoff_valid = True
                disagreement = transition(developing, RunState.CHANGES_REQUESTED)
                store.update(disagreement, expected_state=RunState.DEVELOPING)
                _write_json_atomic(
                    run_directory / 'decision-required.json',
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
                )
                return disagreement
            recoverable, new_digest = _classify_remediation_progress(
                parsed_handoff.payload.status, handoff_digest, current_digest
            )
            handoff_valid = True
            if recoverable:
                validation_required = replace(
                    transition(developing, RunState.VALIDATION_REQUIRED),
                    diff_digest=new_digest,
                    updated_at=utc_now(),
                )
                store.update(validation_required, expected_state=RunState.DEVELOPING)
                return validation_required
        except WorkerError:
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
                handoff_temporary.replace(destination)
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
    store: RunStore,
    run: Run,
    request: dict[str, Any],
    current_digest: str,
    allow_unchanged_ready: bool,
    reviewer_command: Sequence[str],
    developer_command: Sequence[str],
    runs_directory: Path,
    timeout_seconds: int,
    developer_timeout_seconds: int,
    max_iterations: int,
    digest_worktree: Callable[[Path, str], str | None],
    reviewer_identity: InvocationIdentity,
    developer_identity: InvocationIdentity,
    attempt: int,
    resume_expected_state: RunState | None = None,
) -> Run:
    """Retry one durable remediation request and continue the same run."""

    run_directory = runs_directory.expanduser().resolve() / str(run.id)
    messages = run_directory / 'messages'
    logs = run_directory / 'logs'
    invocations = run_directory / 'invocations'
    sequence = int(request['sequence'])
    response_path = run_directory / '.developer-handoff.json'
    handoff_path = messages / f'{sequence + 1:06d}-developer-handoff.json'
    review_result_path = Path(request['payload']['review_result_path']).resolve()
    review_result = _read_object(review_result_path)
    finding_ids = tuple(
        finding['finding_id'] for finding in review_result['payload']['findings']
    )
    adapter = CommandAgentAdapter(tuple(developer_command))
    developer_stem = _invocation_stem(sequence, 'developer', attempt)
    developer_metadata_path = run_directory / f'.{developer_stem}.runtime.json'
    started_at = timestamp()
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
    if resume_expected_state is not None:
        store.update(run, expected_state=resume_expected_state)
    try:
        completed = adapter.execute(
            DeveloperRequest(
                objective=request['payload']['objective'],
                worktree_path=run.worktree_path,
                iteration=run.iteration,
                allowed_actions=(),
                timeout_seconds=developer_timeout_seconds,
                request_path=messages / f'{sequence:06d}-remediation-request.json',
                response_path=response_path,
                stdout_path=logs / f'{developer_stem}.stdout.log',
                stderr_path=logs / f'{developer_stem}.stderr.log',
                runtime_metadata_path=_runtime_metadata_path(
                    developer_identity, developer_metadata_path
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
        )
        interrupted = transition(run, RunState.INTERRUPTED)
        store.update(interrupted, expected_state=RunState.DEVELOPING)
        raise WorkerError(
            f'developer timed out after {developer_timeout_seconds} seconds',
            code=RESUME_INTERRUPTED_CODE,
        ) from error
    except KeyboardInterrupt as error:
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
        effective_models=completed.effective_models,
        effective_model_status=completed.effective_model_status,
    )
    if not completed.succeeded:
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.DEVELOPING)
        raise WorkerError(
            f'developer exited with code {completed.exit_code}',
            code=RESUME_EXECUTION_FAILED_CODE,
        )

    handoff_valid = False
    try:
        handoff = _read_object(response_path)
        _require_unique_message_id(handoff, messages)
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
            response_path.replace(destination)

    reviewing = replace(
        transition(run, RunState.REVIEWING),
        diff_digest=measured_digest,
        updated_at=utc_now(),
    )
    store.update(reviewing, expected_state=RunState.DEVELOPING)
    return _run_queued_review(
        store=store,
        run=reviewing,
        objective=request['payload']['objective'],
        reviewer_command=reviewer_command,
        developer_command=developer_command,
        runs_directory=runs_directory,
        timeout_seconds=timeout_seconds,
        developer_timeout_seconds=developer_timeout_seconds,
        max_iterations=max_iterations,
        digest_worktree=digest_worktree,
        reviewer_identity=reviewer_identity,
        developer_identity=developer_identity,
        continuation_sequence=sequence + 2,
        continuation_prior_review_path=review_result_path,
    )


def _resume_review(
    *,
    store: RunStore,
    run: Run,
    runs_directory: Path,
    digest_worktree: Callable[[Path, str], str | None],
) -> Run:
    """Resume one recoverable run from its canonical execution evidence."""

    if run.state not in {RunState.VALIDATION_REQUIRED, RunState.INTERRUPTED}:
        raise WorkerError(
            f'run is not resumable from {run.state}', code=RUN_NOT_RESUMABLE_CODE
        )
    run_directory = runs_directory.expanduser().resolve() / str(run.id)
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)
    execution = _read_execution_record(run_directory, str(run.id))
    chain = _read_message_chain(run_directory.resolve(), str(run.id))
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
    measured_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
    if measured_digest is None:
        raise WorkerError(NO_CHANGES)

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
            run_directory, int(request['sequence']), str(request['recipient'])
        )
        resumed = replace(run, state=origin, updated_at=utc_now())
        if origin is RunState.REVIEWING:
            return _run_queued_review(
                store=store,
                run=resumed,
                objective=execution.objective,
                reviewer_command=execution.reviewer.command,
                developer_command=execution.developer.command,
                runs_directory=runs_directory,
                timeout_seconds=execution.reviewer.timeout_seconds,
                developer_timeout_seconds=execution.developer.timeout_seconds,
                max_iterations=execution.max_review_iterations,
                digest_worktree=digest_worktree,
                reviewer_identity=reviewer_identity,
                developer_identity=developer_identity,
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
            store=store,
            run=resumed,
            request=request,
            current_digest=run.diff_digest or '',
            allow_unchanged_ready=retrying_recovery_request,
            reviewer_command=execution.reviewer.command,
            developer_command=execution.developer.command,
            runs_directory=runs_directory,
            timeout_seconds=execution.reviewer.timeout_seconds,
            developer_timeout_seconds=execution.developer.timeout_seconds,
            max_iterations=execution.max_review_iterations,
            digest_worktree=digest_worktree,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
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
            run_directory, int(request['sequence']), str(request['recipient'])
        )
        resumed = transition(run, RunState.DEVELOPING)
        return _resume_developer_request(
            store=store,
            run=resumed,
            request=request,
            current_digest=measured_digest,
            allow_unchanged_ready=True,
            reviewer_command=execution.reviewer.command,
            developer_command=execution.developer.command,
            runs_directory=runs_directory,
            timeout_seconds=execution.reviewer.timeout_seconds,
            developer_timeout_seconds=execution.developer.timeout_seconds,
            max_iterations=execution.max_review_iterations,
            digest_worktree=digest_worktree,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
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
    request_path = (
        run_directory / 'messages' / f'{sequence:06d}-remediation-request.json'
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
    _write_json_atomic(request_path, recovery_request)
    resumed = transition(run, RunState.DEVELOPING)
    return _resume_developer_request(
        store=store,
        run=resumed,
        request=recovery_request,
        current_digest=measured_digest,
        allow_unchanged_ready=True,
        reviewer_command=execution.reviewer.command,
        developer_command=execution.developer.command,
        runs_directory=runs_directory,
        timeout_seconds=execution.reviewer.timeout_seconds,
        developer_timeout_seconds=execution.developer.timeout_seconds,
        max_iterations=execution.max_review_iterations,
        digest_worktree=digest_worktree,
        reviewer_identity=reviewer_identity,
        developer_identity=developer_identity,
        attempt=1,
        resume_expected_state=RunState.VALIDATION_REQUIRED,
    )


def resume_review(
    *,
    store: RunStore,
    run: Run,
    runs_directory: Path,
    digest_worktree: Callable[[Path, str], str | None],
) -> Run:
    """Resume one run and persist any recoverable-command failure."""

    run_directory = runs_directory.expanduser().resolve() / str(run.id)
    try:
        return _resume_review(
            store=store,
            run=run,
            runs_directory=runs_directory,
            digest_worktree=digest_worktree,
        )
    except WorkerError as error:
        if not run_directory.is_relative_to(run.worktree_path.resolve()):
            try:
                durable_run = store.get(str(run.id))
                _write_json_atomic(
                    run_directory / 'failure.json',
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
                )
            except OSError:
                pass
        raise


def run_queued_review(
    *,
    store: RunStore,
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
) -> Run:
    """Run the bounded loop and persist every worker failure as durable evidence."""

    run_directory = runs_directory.expanduser().resolve() / str(run.id)
    reviewer_identity = reviewer_identity or InvocationIdentity(
        vendor='unknown', model=None, runtime='custom-command'
    )
    developer_identity = developer_identity or InvocationIdentity(
        vendor='unknown', model=None, runtime='custom-command'
    )
    try:
        return _run_queued_review(
            store=store,
            run=run,
            objective=objective,
            reviewer_command=reviewer_command,
            developer_command=developer_command,
            runs_directory=runs_directory,
            timeout_seconds=timeout_seconds,
            developer_timeout_seconds=developer_timeout_seconds,
            max_iterations=max_iterations,
            digest_worktree=digest_worktree,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
        )
    except WorkerError as error:
        if not run_directory.is_relative_to(run.worktree_path.resolve()):
            try:
                durable_run = store.get(str(run.id))
                _write_json_atomic(
                    run_directory / 'failure.json',
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
                )
            except OSError:
                pass
        raise
