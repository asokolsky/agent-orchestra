"""Execute one durable, bounded local review step."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Never, cast
from uuid import uuid4

from pydantic import ValidationError

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
    RESUME_ACTIVATION_UNCERTAIN_CODE,
    RESUME_CANCELLED_CODE,
    RESUME_EXECUTION_FAILED_CODE,
    RESUME_INTERRUPTED_CODE,
    RESUME_METADATA_UNSUPPORTED_CODE,
    RESUME_SCOPE_CHANGED_CODE,
    REVIEWER_BATCH_INCOMPLETE_CODE,
    RUN_NOT_RESUMABLE_CODE,
    WorkerError,
    archive_unaccepted_response,
    finalize_temporary_path,
    invocation_stem,
    manifest_evidence_path,
    read_json_object,
    record_finalized_path,
    require_unchanged,
    reviewer_dispatch_path,
    run_evidence_path,
    worktree_digest,
    write_json_atomic,
)
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptIdentity,
    AttemptLifecycle,
    AttemptStatus,
    InvocationEvidenceError,
    InvocationEvidenceStore,
    InvocationIdentity,
    InvocationRecord,
    ProcessOutcome,
    RecoveryAction,
    attempt_activation_was_persisted,
    attempt_record_path,
    latest_task_attempt,
    next_attempt,
    persist_attempt_record,
    prepare_run_evidence_directory,
    record_invocation,
    recovery_action,
    timestamp,
    transition_attempt,
)
from agent_orchestra.manifests import canonical_message_evidence, evidence_path
from agent_orchestra.messages import (
    NO_CHANGES,
    classify_remediation_progress,
    is_developer_disagreement,
    read_message_chain,
    require_unique_batch_message_ids,
    require_unique_message_id,
    reviewer_id_from_message_path,
    validate_developer_handoff,
    validate_remediation_request,
    validate_resumed_progress,
    validate_review_request,
    validate_review_response,
)
from agent_orchestra.models import Run, RunState, same_diff_digest, utc_now
from agent_orchestra.review_batch import (
    ReviewerDecision,
    ReviewerDispatchResult,
    aggregate_review_batch,
)
from agent_orchestra.review_fanout import ReviewerDispatch, build_review_fanout
from agent_orchestra.reviewer_paths import (
    ReviewerIdentityError,
    reviewer_task_id,
)
from agent_orchestra.reviewer_plan import (
    ReviewerExecution,
    ReviewerExecutionPlan,
    reviewer_execution_plan_record,
)
from agent_orchestra.runtime_metadata import (
    exception_runtime_metadata,
    runtime_metadata_path,
)
from agent_orchestra.schemas import (
    EXECUTION_RECORD_ADAPTER,
    DeveloperHandoffMessageSchema,
    ExecutionRecord,
    ExecutionRecordSchema,
    ReviewerBatchResultSchema,
    ReviewerSetExecutionRecordSchema,
)
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agent_orchestra.store import JobStore


EMPTY_OBJECTIVE = 'objective must not be empty'
EMPTY_COMMAND = 'reviewer command must not be empty'
DEVELOPER_DISAGREEMENT = 'developer disputed every finding without changing the diff'
ITERATION_LIMIT = 'maximum review iteration count exhausted'
INVALID_DEVELOPER_TIMEOUT = 'developer timeout must be positive'
INVALID_ITERATION_LIMIT = 'maximum review iterations must be positive'
EVIDENCE_INSIDE_WORKTREE = 'run evidence directory must be outside the worktree'
REVIEWER_BATCH_INCOMPLETE = 'reviewer batch did not complete'


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


def _read_execution_record(run_directory: Path, run_id: str) -> ExecutionRecord:
    """Read and validate the durable execution context for a resumable run."""

    path = run_evidence_path(run_directory, 'execution.json')
    try:
        document = read_json_object(path)
        record = EXECUTION_RECORD_ADAPTER.validate_python(document)
    except (WorkerError, ValidationError) as error:
        message = 'resume metadata is missing, legacy, or invalid'
        raise WorkerError(message, code=RESUME_METADATA_UNSUPPORTED_CODE) from error
    if record.run_id != run_id:
        message = 'resume metadata does not match the run ID'
        raise WorkerError(message)
    return record


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


def _latest_reviewer_attempt(
    run_directory: Path, sequence: int, reviewer_id: str
) -> InvocationRecord | None:
    """Return the latest validated attempt for one reviewer-set member."""

    try:
        records = InvocationEvidenceStore(run_directory).read_all(run_directory.name)
        task_id = reviewer_task_id(run_directory.name, sequence, reviewer_id)
    except (InvocationEvidenceError, ReviewerIdentityError) as error:
        message = 'invalid reviewer invocation evidence'
        raise WorkerError(message) from error
    attempts = [record for record in records if record.task_id == task_id]
    return max(attempts, key=lambda record: record.attempt) if attempts else None


def _next_reviewer_attempt(run_directory: Path, sequence: int, reviewer_id: str) -> int:
    """Return the next non-overwriting attempt for one incomplete reviewer."""

    latest = _latest_reviewer_attempt(run_directory, sequence, reviewer_id)
    if latest is None:
        return 1
    if latest.status is not AttemptStatus.COMPLETED:
        message = 'cannot retry reviewer with uncertain active attempt'
        raise WorkerError(message, code=RESUME_ACTIVATION_UNCERTAIN_CODE)
    return latest.attempt + 1


def _require_completed_reviewer_attempt(record: InvocationRecord) -> None:
    """Reject active reviewer evidence whose activation remains uncertain."""

    if record.status is not AttemptStatus.COMPLETED:
        message = 'cannot recover reviewer with uncertain active attempt'
        raise WorkerError(message, code=RESUME_ACTIVATION_UNCERTAIN_CODE)


def _require_successful_reviewer_attempt(
    run_directory: Path, sequence: int, reviewer_id: str
) -> None:
    """Require successful invocation evidence for one canonical result."""

    latest = _latest_reviewer_attempt(run_directory, sequence, reviewer_id)
    if (
        latest is None
        or latest.status is not AttemptStatus.COMPLETED
        or latest.conclusion is not AttemptConclusion.SUCCEEDED
    ):
        message = 'canonical reviewer result lacks a completed successful attempt'
        raise WorkerError(message, code=RESUME_ACTIVATION_UNCERTAIN_CODE)


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

    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)

    current_digest = worktree_digest(digest_worktree, run.worktree_path, run.base_sha)
    if current_digest is None:
        raise WorkerError(NO_CHANGES)
    artifacts = run_evidence_path(run_directory, 'artifacts')
    logs = run_evidence_path(run_directory, 'logs')
    if continuing:
        assert continuation_sequence is not None
        require_unchanged(current_digest, run.diff_digest or '')
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
        write_json_atomic(
            run_evidence_path(run_directory, 'execution.json'),
            execution.model_dump(mode='json'),
            'execution',
        )
        reviewing = transition(prepared, RunState.REVIEWING)
        store.update(reviewing, expected_state=RunState.PREPARING)
        sequence = 1
        prior_review_path = None
    artifacts.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    run_evidence_path(run_directory, 'invocations').mkdir(parents=True, exist_ok=True)
    reviewer_adapter = CommandAgentAdapter(tuple(reviewer_command))
    developer_adapter = CommandAgentAdapter(tuple(developer_command))

    while True:
        if retry_review_request is None:
            artifact_path = artifacts / f'review-{reviewing.iteration:04d}.md'
            request_path = manifest_evidence_path(
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
            validate_review_request(request, run_directory=run_directory)
            write_json_atomic(request_path, request, 'review_request')
        else:
            request = retry_review_request
            retry_review_request = None
            sequence = int(request['sequence'])
            artifact_path = Path(request['payload']['artifact_path'])
            request_path = manifest_evidence_path(
                run_directory, 'review_request', sequence
            )
            validate_review_request(request, run_directory=run_directory)
        response_path = run_evidence_path(run_directory, '.review-result.json')
        reviewer_stem = invocation_stem(sequence, 'reviewer', reviewer_attempt)
        reviewer_metadata_path = run_evidence_path(
            run_directory, f'.{reviewer_stem}.runtime.json'
        )
        started_at = timestamp()
        invocation_id = record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.REVIEWER,
                agent=reviewer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                attempt=reviewer_attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout='',
                stderr='',
                exit_code=None,
                finished=False,
            ),
            run_directory=run_directory,
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
                    runtime_metadata_path=runtime_metadata_path(
                        reviewer_identity, reviewer_metadata_path, registry
                    ),
                    on_started=partial(
                        record_invocation,
                        AttemptIdentity(
                            run_id=str(run.id),
                            role=RuntimeRole.REVIEWER,
                            agent=reviewer_identity,
                            iteration=reviewing.iteration,
                            sequence=sequence,
                            attempt=reviewer_attempt,
                            invocation_id=invocation_id,
                        ),
                        ProcessOutcome(
                            started_at=started_at,
                            stdout=None,
                            stderr=None,
                            exit_code=None,
                            finished=False,
                        ),
                        run_directory=run_directory,
                        lifecycle=AttemptLifecycle(status=AttemptStatus.RUNNING),
                    ),
                )
            )
            if artifact_path.is_file():
                record_finalized_path(artifact_path, 'review_artifact')
        except subprocess.TimeoutExpired as error:
            effective_models, effective_model_status = exception_runtime_metadata(error)
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.REVIEWER,
                    agent=reviewer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                    attempt=reviewer_attempt,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout=error.stdout,
                    stderr=error.stderr,
                    exit_code=None,
                    timed_out=True,
                    effective_models=effective_models,
                    effective_model_status=effective_model_status,
                ),
                run_directory=run_directory,
            )
            archive_unaccepted_response(
                response_path,
                logs / f'{sequence + 1:06d}-rejected-review-result-attempt-'
                f'{reviewer_attempt:04d}.json',
                'rejected_review_result',
            )
            archive_unaccepted_response(
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
            effective_models, effective_model_status = exception_runtime_metadata(error)
            if attempt_activation_was_persisted(
                run_directory, sequence, RuntimeRole.REVIEWER, reviewer_attempt
            ):
                record_invocation(
                    AttemptIdentity(
                        run_id=str(run.id),
                        role=RuntimeRole.REVIEWER,
                        agent=reviewer_identity,
                        iteration=reviewing.iteration,
                        sequence=sequence,
                        invocation_id=invocation_id,
                        attempt=reviewer_attempt,
                    ),
                    ProcessOutcome(
                        started_at=started_at,
                        stdout=None,
                        stderr=None,
                        exit_code=None,
                        interrupted=True,
                        effective_models=effective_models,
                        effective_model_status=effective_model_status,
                    ),
                    run_directory=run_directory,
                )
            archive_unaccepted_response(
                response_path,
                logs / f'{sequence + 1:06d}-rejected-review-result-attempt-'
                f'{reviewer_attempt:04d}.json',
                'rejected_review_result',
            )
            archive_unaccepted_response(
                artifact_path,
                logs / f'{sequence + 1:06d}-rejected-review-artifact-attempt-'
                f'{reviewer_attempt:04d}.md',
                'rejected_review_artifact',
            )
            interrupted = transition(reviewing, RunState.INTERRUPTED)
            store.update(interrupted, expected_state=RunState.REVIEWING)
            raise
        except OSError as error:
            effective_models, effective_model_status = exception_runtime_metadata(error)
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.REVIEWER,
                    agent=reviewer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                    attempt=reviewer_attempt,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout='',
                    stderr=str(error),
                    exit_code=None,
                    effective_models=effective_models,
                    effective_model_status=effective_model_status,
                ),
                run_directory=run_directory,
            )
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise WorkerError(
                f'cannot execute reviewer: {error}',
                code=RESUME_EXECUTION_FAILED_CODE,
            ) from error
        process_finished_at = timestamp()
        if not completed.succeeded:
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.REVIEWER,
                    agent=reviewer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                    attempt=reviewer_attempt,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    exit_code=completed.exit_code,
                    effective_models=completed.effective_models,
                    effective_model_status=completed.effective_model_status,
                    finished_at=process_finished_at,
                ),
                run_directory=run_directory,
                lifecycle=AttemptLifecycle(conclusion=AttemptConclusion.FAILED),
            )
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise WorkerError(
                f'reviewer exited with code {completed.exit_code}',
                code=RESUME_EXECUTION_FAILED_CODE,
            )

        response_valid = False
        review_result_path = manifest_evidence_path(
            run_directory, 'review_result', sequence + 1
        )
        response_received_at = timestamp() if response_path.is_file() else None
        validation_started_at = timestamp()
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.REVIEWER,
                agent=reviewer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                invocation_id=invocation_id,
                attempt=reviewer_attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.exit_code,
                finished_at=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(
                status=AttemptStatus.RUNNING,
                response_received_at=response_received_at,
                validation_started_at=validation_started_at,
            ),
        )
        try:
            response = read_json_object(response_path)
            require_unique_message_id(response, run_directory)
            verdict = validate_review_response(
                response, request=request, artifact_path=artifact_path
            )
            require_unchanged(
                worktree_digest(digest_worktree, run.worktree_path, run.base_sha),
                current_digest,
            )
            response_valid = True
        except WorkerError:
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.REVIEWER,
                    agent=reviewer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                    attempt=reviewer_attempt,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout=None,
                    stderr=None,
                    exit_code=completed.exit_code,
                    finished_at=process_finished_at,
                    effective_models=completed.effective_models,
                    effective_model_status=completed.effective_model_status,
                ),
                run_directory=run_directory,
                lifecycle=AttemptLifecycle(
                    conclusion=AttemptConclusion.FAILED,
                    response_received_at=response_received_at,
                    validation_started_at=validation_started_at,
                ),
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
                finalize_temporary_path(
                    response_path,
                    destination,
                    'review_result' if response_valid else 'rejected_review_result',
                )

        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.REVIEWER,
                agent=reviewer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                invocation_id=invocation_id,
                attempt=reviewer_attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=None,
                stderr=None,
                exit_code=completed.exit_code,
                finished_at=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(
                conclusion=AttemptConclusion.SUCCEEDED,
                response_received_at=response_received_at,
                validation_started_at=validation_started_at,
            ),
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
        remediation_path = manifest_evidence_path(
            run_directory, 'remediation_request', sequence
        )
        handoff_temporary = run_evidence_path(run_directory, '.developer-handoff.json')
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
        validate_remediation_request(remediation, run_directory=run_directory)
        write_json_atomic(remediation_path, remediation, 'remediation_request')
        developing = transition(decided, RunState.DEVELOPING)
        store.update(developing, expected_state=RunState.CHANGES_REQUESTED)
        developer_stem = invocation_stem(sequence, 'developer', 1)
        developer_metadata_path = run_evidence_path(
            run_directory, f'.{developer_stem}.runtime.json'
        )
        started_at = timestamp()
        invocation_id = record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.DEVELOPER,
                agent=developer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout='',
                stderr='',
                exit_code=None,
                finished=False,
            ),
            run_directory=run_directory,
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
                    runtime_metadata_path=runtime_metadata_path(
                        developer_identity, developer_metadata_path, registry
                    ),
                    on_started=partial(
                        record_invocation,
                        AttemptIdentity(
                            run_id=str(run.id),
                            role=RuntimeRole.DEVELOPER,
                            agent=developer_identity,
                            iteration=reviewing.iteration,
                            sequence=sequence,
                            invocation_id=invocation_id,
                        ),
                        ProcessOutcome(
                            started_at=started_at,
                            stdout=None,
                            stderr=None,
                            exit_code=None,
                            finished=False,
                        ),
                        run_directory=run_directory,
                        lifecycle=AttemptLifecycle(status=AttemptStatus.RUNNING),
                    ),
                )
            )
        except subprocess.TimeoutExpired as error:
            effective_models, effective_model_status = exception_runtime_metadata(error)
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.DEVELOPER,
                    agent=developer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout=error.stdout,
                    stderr=error.stderr,
                    exit_code=None,
                    timed_out=True,
                    effective_models=effective_models,
                    effective_model_status=effective_model_status,
                ),
                run_directory=run_directory,
            )
            archive_unaccepted_response(
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
            effective_models, effective_model_status = exception_runtime_metadata(error)
            if attempt_activation_was_persisted(
                run_directory, sequence, RuntimeRole.DEVELOPER, 1
            ):
                record_invocation(
                    AttemptIdentity(
                        run_id=str(run.id),
                        role=RuntimeRole.DEVELOPER,
                        agent=developer_identity,
                        iteration=reviewing.iteration,
                        sequence=sequence,
                        invocation_id=invocation_id,
                    ),
                    ProcessOutcome(
                        started_at=started_at,
                        stdout=None,
                        stderr=None,
                        exit_code=None,
                        interrupted=True,
                        effective_models=effective_models,
                        effective_model_status=effective_model_status,
                    ),
                    run_directory=run_directory,
                )
            archive_unaccepted_response(
                handoff_temporary,
                logs
                / f'{sequence + 1:06d}-rejected-developer-handoff-attempt-0001.json',
                'rejected_developer_handoff',
            )
            interrupted = transition(developing, RunState.INTERRUPTED)
            store.update(interrupted, expected_state=RunState.DEVELOPING)
            raise
        except OSError as error:
            effective_models, effective_model_status = exception_runtime_metadata(error)
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.DEVELOPER,
                    agent=developer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout='',
                    stderr=str(error),
                    exit_code=None,
                    effective_models=effective_models,
                    effective_model_status=effective_model_status,
                ),
                run_directory=run_directory,
            )
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise WorkerError(
                f'cannot execute developer: {error}',
                code=RESUME_EXECUTION_FAILED_CODE,
            ) from error
        process_finished_at = timestamp()
        if not completed.succeeded:
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.DEVELOPER,
                    agent=developer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    exit_code=completed.exit_code,
                    finished_at=process_finished_at,
                    effective_models=completed.effective_models,
                    effective_model_status=completed.effective_model_status,
                ),
                run_directory=run_directory,
                lifecycle=AttemptLifecycle(conclusion=AttemptConclusion.FAILED),
            )
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise WorkerError(
                f'developer exited with code {completed.exit_code}',
                code=RESUME_EXECUTION_FAILED_CODE,
            )
        handoff_valid = False
        handoff_path = manifest_evidence_path(
            run_directory, 'developer_handoff', sequence + 1
        )
        response_received_at = timestamp() if handoff_temporary.is_file() else None
        validation_started_at = timestamp()
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.DEVELOPER,
                agent=developer_identity,
                iteration=reviewing.iteration,
                sequence=sequence,
                invocation_id=invocation_id,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.exit_code,
                finished_at=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(
                status=AttemptStatus.RUNNING,
                response_received_at=response_received_at,
                validation_started_at=validation_started_at,
            ),
        )
        finding_ids = tuple(
            finding['finding_id'] for finding in response['payload']['findings']
        )
        try:
            handoff = read_json_object(handoff_temporary)
            require_unique_message_id(handoff, run_directory)
            parsed_handoff = validate_developer_handoff(
                handoff, request=remediation, finding_ids=finding_ids
            )
            handoff_digest = worktree_digest(
                digest_worktree, run.worktree_path, run.base_sha
            )
            is_disagreement = (
                parsed_handoff.payload.status == 'ready_for_review'
                and handoff_digest is not None
                and same_diff_digest(handoff_digest, current_digest)
                and is_developer_disagreement(parsed_handoff)
            )
            if is_disagreement:
                assert handoff_digest is not None
                recoverable, new_digest = False, handoff_digest
            else:
                recoverable, new_digest = classify_remediation_progress(
                    parsed_handoff.payload.status, handoff_digest, current_digest
                )
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.DEVELOPER,
                    agent=developer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout=None,
                    stderr=None,
                    exit_code=completed.exit_code,
                    finished_at=process_finished_at,
                    effective_models=completed.effective_models,
                    effective_model_status=completed.effective_model_status,
                ),
                run_directory=run_directory,
                lifecycle=AttemptLifecycle(
                    conclusion=AttemptConclusion.SUCCEEDED,
                    response_received_at=response_received_at,
                    validation_started_at=validation_started_at,
                ),
            )
            handoff_valid = True
            if is_disagreement:
                disagreement = transition(developing, RunState.CHANGES_REQUESTED)
                store.update(disagreement, expected_state=RunState.DEVELOPING)
                write_json_atomic(
                    run_evidence_path(run_directory, 'decision-required.json'),
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
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.DEVELOPER,
                    agent=developer_identity,
                    iteration=reviewing.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout=None,
                    stderr=None,
                    exit_code=completed.exit_code,
                    finished_at=process_finished_at,
                    effective_models=completed.effective_models,
                    effective_model_status=completed.effective_model_status,
                ),
                run_directory=run_directory,
                lifecycle=AttemptLifecycle(
                    conclusion=AttemptConclusion.FAILED,
                    response_received_at=response_received_at,
                    validation_started_at=validation_started_at,
                ),
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
                finalize_temporary_path(
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

    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
    logs = run_evidence_path(run_directory, 'logs')
    sequence = int(request['sequence'])
    response_path = run_evidence_path(run_directory, '.developer-handoff.json')
    handoff_path = manifest_evidence_path(
        run_directory, 'developer_handoff', sequence + 1
    )
    review_result_path = Path(request['payload']['review_result_path']).resolve()
    review_result = read_json_object(review_result_path)
    finding_ids = tuple(
        finding['finding_id'] for finding in review_result['payload']['findings']
    )
    adapter = CommandAgentAdapter(tuple(developer_command))
    developer_stem = invocation_stem(sequence, 'developer', attempt)
    developer_metadata_path = run_evidence_path(
        run_directory, f'.{developer_stem}.runtime.json'
    )
    started_at = timestamp()
    if resume_expected_state is not None:
        store.update(run, expected_state=resume_expected_state)
    invocation_id = record_invocation(
        AttemptIdentity(
            run_id=str(run.id),
            role=RuntimeRole.DEVELOPER,
            agent=developer_identity,
            iteration=run.iteration,
            sequence=sequence,
            attempt=attempt,
        ),
        ProcessOutcome(
            started_at=started_at, stdout='', stderr='', exit_code=None, finished=False
        ),
        run_directory=run_directory,
    )
    try:
        completed = adapter.execute(
            DeveloperRequest(
                objective=request['payload']['objective'],
                worktree_path=run.worktree_path,
                iteration=run.iteration,
                allowed_actions=(),
                timeout_seconds=developer_timeout_seconds,
                request_path=manifest_evidence_path(
                    run_directory, 'remediation_request', sequence
                ),
                response_path=response_path,
                stdout_path=logs / f'{developer_stem}.stdout.log',
                stderr_path=logs / f'{developer_stem}.stderr.log',
                runtime_metadata_path=runtime_metadata_path(
                    developer_identity, developer_metadata_path, registry
                ),
                on_started=partial(
                    record_invocation,
                    AttemptIdentity(
                        run_id=str(run.id),
                        role=RuntimeRole.DEVELOPER,
                        agent=developer_identity,
                        iteration=run.iteration,
                        sequence=sequence,
                        invocation_id=invocation_id,
                        attempt=attempt,
                    ),
                    ProcessOutcome(
                        started_at=started_at,
                        stdout=None,
                        stderr=None,
                        exit_code=None,
                        finished=False,
                    ),
                    run_directory=run_directory,
                    lifecycle=AttemptLifecycle(status=AttemptStatus.RUNNING),
                ),
            )
        )
    except subprocess.TimeoutExpired as error:
        effective_models, effective_model_status = exception_runtime_metadata(error)
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.DEVELOPER,
                agent=developer_identity,
                iteration=run.iteration,
                sequence=sequence,
                invocation_id=invocation_id,
                attempt=attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=error.stdout,
                stderr=error.stderr,
                exit_code=None,
                timed_out=True,
                effective_models=effective_models,
                effective_model_status=effective_model_status,
            ),
            run_directory=run_directory,
        )
        archive_unaccepted_response(
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
        effective_models, effective_model_status = exception_runtime_metadata(error)
        if attempt_activation_was_persisted(
            run_directory, sequence, RuntimeRole.DEVELOPER, attempt
        ):
            record_invocation(
                AttemptIdentity(
                    run_id=str(run.id),
                    role=RuntimeRole.DEVELOPER,
                    agent=developer_identity,
                    iteration=run.iteration,
                    sequence=sequence,
                    invocation_id=invocation_id,
                    attempt=attempt,
                ),
                ProcessOutcome(
                    started_at=started_at,
                    stdout=None,
                    stderr=None,
                    exit_code=None,
                    interrupted=True,
                    effective_models=effective_models,
                    effective_model_status=effective_model_status,
                ),
                run_directory=run_directory,
            )
        archive_unaccepted_response(
            response_path,
            logs / f'{sequence + 1:06d}-rejected-developer-handoff-attempt-'
            f'{attempt:04d}.json',
            'rejected_developer_handoff',
        )
        interrupted = transition(run, RunState.INTERRUPTED)
        store.update(interrupted, expected_state=RunState.DEVELOPING)
        raise
    except OSError as error:
        effective_models, effective_model_status = exception_runtime_metadata(error)
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.DEVELOPER,
                agent=developer_identity,
                iteration=run.iteration,
                sequence=sequence,
                invocation_id=invocation_id,
                attempt=attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=None,
                stderr=str(error),
                exit_code=None,
                effective_models=effective_models,
                effective_model_status=effective_model_status,
            ),
            run_directory=run_directory,
        )
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.DEVELOPING)
        raise WorkerError(
            f'cannot execute developer: {error}',
            code=RESUME_EXECUTION_FAILED_CODE,
        ) from error
    process_finished_at = timestamp()
    if not completed.succeeded:
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.DEVELOPER,
                agent=developer_identity,
                iteration=run.iteration,
                sequence=sequence,
                invocation_id=invocation_id,
                attempt=attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.exit_code,
                finished_at=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(conclusion=AttemptConclusion.FAILED),
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
    record_invocation(
        AttemptIdentity(
            run_id=str(run.id),
            role=RuntimeRole.DEVELOPER,
            agent=developer_identity,
            iteration=run.iteration,
            sequence=sequence,
            invocation_id=invocation_id,
            attempt=attempt,
        ),
        ProcessOutcome(
            started_at=started_at,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.exit_code,
            finished_at=process_finished_at,
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        ),
        run_directory=run_directory,
        lifecycle=AttemptLifecycle(
            status=AttemptStatus.RUNNING,
            response_received_at=response_received_at,
            validation_started_at=validation_started_at,
        ),
    )
    try:
        handoff = read_json_object(response_path)
        require_unique_message_id(handoff, run_directory)
        parsed = validate_developer_handoff(
            handoff, request=request, finding_ids=finding_ids
        )
        recoverable, measured_digest = validate_resumed_progress(
            parsed.payload.status,
            worktree_digest(digest_worktree, run.worktree_path, run.base_sha),
            current_digest,
            allow_unchanged_ready=allow_unchanged_ready,
            is_disagreement=is_developer_disagreement(parsed),
        )
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.DEVELOPER,
                agent=developer_identity,
                iteration=run.iteration,
                sequence=sequence,
                invocation_id=invocation_id,
                attempt=attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=None,
                stderr=None,
                exit_code=completed.exit_code,
                finished_at=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(
                conclusion=AttemptConclusion.SUCCEEDED,
                response_received_at=response_received_at,
                validation_started_at=validation_started_at,
            ),
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
            and is_developer_disagreement(parsed)
        ):
            disagreement = transition(run, RunState.CHANGES_REQUESTED)
            store.update(disagreement, expected_state=RunState.DEVELOPING)
            return disagreement
    except WorkerError:
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.DEVELOPER,
                agent=developer_identity,
                iteration=run.iteration,
                sequence=sequence,
                invocation_id=invocation_id,
                attempt=attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=None,
                stderr=None,
                exit_code=completed.exit_code,
                finished_at=process_finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(
                conclusion=AttemptConclusion.FAILED,
                response_received_at=response_received_at,
                validation_started_at=validation_started_at,
            ),
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
            finalize_temporary_path(
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
    role: Literal[RuntimeRole.DEVELOPER, RuntimeRole.REVIEWER],
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
    persist_attempt_record(
        attempt_record_path(run_directory, sequence, role, record.attempt), updated
    )
    return updated


def _complete_recovered_validation(
    *,
    run_directory: Path,
    sequence: int,
    role: Literal[RuntimeRole.DEVELOPER, RuntimeRole.REVIEWER],
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
    persist_attempt_record(
        attempt_record_path(run_directory, sequence, role, record.attempt), completed
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

    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
    sequence = int(request['sequence'])
    temporary = run_evidence_path(run_directory, '.review-result.json')
    canonical = manifest_evidence_path(run_directory, 'review_result', sequence + 1)
    response_path, is_temporary = _recovery_response_path(temporary, canonical)
    if action is not RecoveryAction.APPLY_CONCLUSION:
        record = _prepare_recovered_validation(
            run_directory=run_directory,
            sequence=sequence,
            role=RuntimeRole.REVIEWER,
            record=record,
            response_present=response_path.is_file(),
        )
    if record.conclusion not in {None, 'succeeded'}:
        _raise_recovered_conclusion(
            store=store, run=run, role=RuntimeRole.REVIEWER, record=record
        )
    artifact_path = Path(request['payload']['artifact_path'])
    try:
        response = read_json_object(response_path)
        if is_temporary:
            require_unique_message_id(response, run_directory)
        verdict = validate_review_response(
            response, request=request, artifact_path=artifact_path
        )
        require_unchanged(
            worktree_digest(digest_worktree, run.worktree_path, run.base_sha),
            run.diff_digest or '',
        )
    except WorkerError:
        if record.status != 'completed':
            _complete_recovered_validation(
                run_directory=run_directory,
                sequence=sequence,
                role=RuntimeRole.REVIEWER,
                record=record,
                conclusion=AttemptConclusion.FAILED,
            )
        if response_path.exists():
            destination = run_evidence_path(
                run_directory,
                'logs',
                f'{sequence + 1:06d}-rejected-review-result-attempt-'
                f'{record.attempt:04d}.json',
            )
            finalize_temporary_path(
                response_path, destination, 'rejected_review_result'
            )
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.REVIEWING)
        raise
    if is_temporary:
        finalize_temporary_path(response_path, canonical, 'review_result')
    _complete_recovered_validation(
        run_directory=run_directory,
        sequence=sequence,
        role=RuntimeRole.REVIEWER,
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
    remediation_path = manifest_evidence_path(
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
    validate_remediation_request(remediation, run_directory=run_directory)
    write_json_atomic(remediation_path, remediation, 'remediation_request')
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

    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
    sequence = int(request['sequence'])
    temporary = run_evidence_path(run_directory, '.developer-handoff.json')
    canonical = manifest_evidence_path(run_directory, 'developer_handoff', sequence + 1)
    response_path, is_temporary = _recovery_response_path(temporary, canonical)
    if action is not RecoveryAction.APPLY_CONCLUSION:
        record = _prepare_recovered_validation(
            run_directory=run_directory,
            sequence=sequence,
            role=RuntimeRole.DEVELOPER,
            record=record,
            response_present=response_path.is_file(),
        )
    if record.conclusion not in {None, 'succeeded'}:
        _raise_recovered_conclusion(
            store=store, run=run, role=RuntimeRole.DEVELOPER, record=record
        )
    review_result_path = Path(request['payload']['review_result_path']).resolve()
    review_result = read_json_object(review_result_path)
    finding_ids = tuple(
        finding['finding_id'] for finding in review_result['payload']['findings']
    )
    try:
        handoff = read_json_object(response_path)
        if is_temporary:
            require_unique_message_id(handoff, run_directory)
        parsed = validate_developer_handoff(
            handoff, request=request, finding_ids=finding_ids
        )
        measured_digest = worktree_digest(
            digest_worktree, run.worktree_path, run.base_sha
        )
        is_disagreement = (
            parsed.payload.status == 'ready_for_review'
            and measured_digest is not None
            and same_diff_digest(measured_digest, run.diff_digest)
            and is_developer_disagreement(parsed)
        )
        if is_disagreement:
            assert measured_digest is not None
            recoverable, new_digest = False, measured_digest
        else:
            recoverable, new_digest = classify_remediation_progress(
                parsed.payload.status, measured_digest, run.diff_digest or ''
            )
    except WorkerError:
        if record.status != 'completed':
            _complete_recovered_validation(
                run_directory=run_directory,
                sequence=sequence,
                role=RuntimeRole.DEVELOPER,
                record=record,
                conclusion=AttemptConclusion.FAILED,
            )
        if response_path.exists():
            destination = run_evidence_path(
                run_directory,
                'logs',
                f'{sequence + 1:06d}-rejected-developer-handoff-attempt-'
                f'{record.attempt:04d}.json',
            )
            finalize_temporary_path(
                response_path, destination, 'rejected_developer_handoff'
            )
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.DEVELOPING)
        raise
    if is_temporary:
        finalize_temporary_path(response_path, canonical, 'developer_handoff')
    _complete_recovered_validation(
        run_directory=run_directory,
        sequence=sequence,
        role=RuntimeRole.DEVELOPER,
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
    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
    latest = latest_task_attempt(run_directory, sequence, role)
    temporary = run_evidence_path(
        run_directory,
        '.review-result.json' if role == 'reviewer' else '.developer-handoff.json',
    )
    canonical = manifest_evidence_path(
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
        validate_remediation_request(request, run_directory=run_directory)
        write_json_atomic(
            run_evidence_path(
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
        attempt=next_attempt(
            run_directory,
            int(request['sequence']),
            str(request['recipient']),
            run.state,
        ),
        resume_expected_state=RunState.CHANGES_REQUESTED,
    )


def _reviewer_plan_from_execution(
    execution: ReviewerSetExecutionRecordSchema, registry: RuntimeRegistry
) -> ReviewerExecutionPlan:
    """Rebuild and validate one persisted reviewer-set execution plan."""

    return ReviewerExecutionPlan(
        execution.reviewer_plan.reviewer_set_id,
        tuple(
            ReviewerExecution(
                reviewer_id=reviewer.reviewer_id,
                command=tuple(reviewer.command),
                identity=_resolve_resume_identity(
                    _identity_from_record(
                        reviewer.identity.vendor,
                        reviewer.identity.model,
                        reviewer.identity.runtime,
                    ),
                    RuntimeRole.REVIEWER,
                    registry,
                ),
                timeout_seconds=reviewer.timeout_seconds,
            )
            for reviewer in execution.reviewer_plan.reviewers
        ),
    )


def _reviewer_batch_sequence(
    run_directory: Path,
    *,
    run: Run,
    reviewer_plan: ReviewerExecutionPlan,
) -> int:
    """Return the current batch sequence from canonical reviewer requests."""

    expected_ids = {reviewer.reviewer_id for reviewer in reviewer_plan.reviewers}
    sequences: dict[str, int] = {}
    message_directory = run_evidence_path(run_directory, 'messages')
    try:
        entries = tuple(message_directory.iterdir())
    except OSError as error:
        message = 'reviewer-set request evidence is unreadable'
        raise WorkerError(message) from error
    for path in entries:
        identity = canonical_message_evidence(
            path.relative_to(run_directory).as_posix()
        )
        if identity is None or identity[0] != 'review_request':
            continue
        sequence = identity[1]
        reviewer_id = reviewer_id_from_message_path(
            path, sequence=sequence, message_type='review_request'
        )
        if reviewer_id is None:
            continue
        request = read_json_object(path)
        if request.get('iteration') != run.iteration:
            continue
        if reviewer_id not in expected_ids or reviewer_id in sequences:
            message = 'reviewer-set request evidence does not match its execution plan'
            raise WorkerError(message)
        sequences[reviewer_id] = sequence
    if set(sequences) != expected_ids or len(set(sequences.values())) != 1:
        message = 'reviewer-set request evidence has no single durable sequence'
        raise WorkerError(message)
    return next(iter(sequences.values()))


def _completed_reviewer_result(
    run_directory: Path,
    dispatch: ReviewerDispatch,
    *,
    run: Run,
    objective: str,
    current_digest: str,
) -> tuple[ReviewerDispatchResult, dict[str, Any]] | None:
    """Return one validated canonical peer result, if it already exists."""

    request_path = reviewer_dispatch_path(run_directory, dispatch.paths.request)
    result_path = reviewer_dispatch_path(run_directory, dispatch.paths.result)
    if not result_path.is_file():
        return None
    request = read_json_object(request_path)
    _require_successful_reviewer_attempt(
        run_directory, int(request['sequence']), dispatch.reviewer_id
    )
    result = read_json_object(result_path)
    artifact_path = reviewer_dispatch_path(run_directory, dispatch.paths.artifact)
    validate_review_request(request, run_directory=run_directory)
    _validate_reviewer_set_request_scope(
        request,
        run=run,
        objective=objective,
        current_digest=current_digest,
        sequence=int(request['sequence']),
        dispatch=dispatch,
        artifact_path=artifact_path,
    )
    verdict = validate_review_response(
        result, request=request, artifact_path=artifact_path
    )
    message_id = result.get('message_id')
    if not isinstance(message_id, str):
        message = 'review result message ID is missing'
        raise WorkerError(message)
    return (
        ReviewerDispatchResult(
            ReviewerDecision(dispatch.reviewer_id, cast('Any', verdict)), message_id
        ),
        request,
    )


def _validate_reviewer_set_request_scope(
    request: dict[str, Any],
    *,
    run: Run,
    objective: str,
    current_digest: str,
    sequence: int,
    dispatch: ReviewerDispatch,
    artifact_path: Path,
) -> None:
    """Bind one persisted reviewer request to its durable run and plan member."""

    expected = {
        'run_id': str(run.id),
        'sequence': sequence,
        'iteration': run.iteration,
        'scope': {
            'worktree_path': str(run.worktree_path),
            'base_sha': run.base_sha,
            'head_sha': run.head_sha,
            'diff_digest': current_digest,
        },
    }
    if any(request.get(field) != value for field, value in expected.items()):
        message = 'reviewer-set request does not match the durable run scope'
        raise WorkerError(message)
    payload = request['payload']
    if (
        payload['objective'] != objective
        or payload['timeout_seconds'] != dispatch.timeout_seconds
        or payload['artifact_path'] != str(artifact_path)
    ):
        message = 'reviewer-set request does not match its execution plan'
        raise WorkerError(message)


def _resume_reviewer_set(
    *,
    context: WorkerContext,
    run: Run,
    run_directory: Path,
    execution: ReviewerSetExecutionRecordSchema,
) -> Run:
    """Resume only incomplete members of one immutable reviewer batch."""

    interrupted_review = run.state is RunState.INTERRUPTED and (
        context.store.interrupted_origin(str(run.id)) is RunState.REVIEWING
    )
    active_review = run.state is RunState.REVIEWING
    if not interrupted_review and not active_review:
        raise WorkerError(
            f'reviewer-set job is not resumable from {run.state}',
            code=RUN_NOT_RESUMABLE_CODE,
        )
    current_digest = worktree_digest(
        context.digest_worktree, run.worktree_path, run.base_sha
    )
    if current_digest is None or not same_diff_digest(current_digest, run.diff_digest):
        message = 'resume scope changed since the interrupted reviewer batch'
        raise WorkerError(message, code=RESUME_SCOPE_CHANGED_CODE)
    reviewer_plan = _reviewer_plan_from_execution(execution, context.registry)
    sequence = _reviewer_batch_sequence(
        run_directory, run=run, reviewer_plan=reviewer_plan
    )
    reviewing = replace(run, state=RunState.REVIEWING, updated_at=utc_now())
    base_dispatches = build_review_fanout(
        reviewer_plan,
        run_id=str(run.id),
        sequence=sequence,
        iteration=reviewing.iteration,
        attempt=1,
    )
    results_by_id: dict[str, ReviewerDispatchResult] = {}
    retry_requests: dict[str, dict[str, Any]] = {}
    retry_attempts: dict[str, int] = {}
    retry_dispatches: list[ReviewerDispatch] = []
    activated = False
    try:
        for base_dispatch in base_dispatches:
            completed = _completed_reviewer_result(
                run_directory,
                base_dispatch,
                run=run,
                objective=execution.objective,
                current_digest=current_digest,
            )
            if completed is not None:
                result, _ = completed
                results_by_id[base_dispatch.reviewer_id] = result
                continue
            request = read_json_object(
                reviewer_dispatch_path(run_directory, base_dispatch.paths.request)
            )
            artifact_path = reviewer_dispatch_path(
                run_directory, base_dispatch.paths.artifact
            )
            validate_review_request(request, run_directory=run_directory)
            _validate_reviewer_set_request_scope(
                request,
                run=run,
                objective=execution.objective,
                current_digest=current_digest,
                sequence=sequence,
                dispatch=base_dispatch,
                artifact_path=artifact_path,
            )
            latest = _latest_reviewer_attempt(
                run_directory, sequence, base_dispatch.reviewer_id
            )
            if active_review and latest is not None:
                _require_completed_reviewer_attempt(latest)
                results_by_id[base_dispatch.reviewer_id] = ReviewerDispatchResult(
                    ReviewerDecision(base_dispatch.reviewer_id, 'incomplete'), None
                )
                continue
            attempt = _next_reviewer_attempt(
                run_directory, sequence, base_dispatch.reviewer_id
            )
            attempt_dispatches = build_review_fanout(
                reviewer_plan,
                run_id=str(run.id),
                sequence=sequence,
                iteration=reviewing.iteration,
                attempt=attempt,
            )
            retry_dispatch = next(
                item
                for item in attempt_dispatches
                if item.reviewer_id == base_dispatch.reviewer_id
            )
            retry_dispatches.append(retry_dispatch)
            retry_requests[retry_dispatch.reviewer_id] = request
            retry_attempts[retry_dispatch.reviewer_id] = attempt
        if interrupted_review:
            context.store.update(reviewing, expected_state=RunState.INTERRUPTED)
        activated = True
        if retry_dispatches:
            with ThreadPoolExecutor(max_workers=len(retry_dispatches)) as executor:
                retry_results = tuple(
                    executor.map(
                        lambda dispatch: _execute_reviewer_dispatch(
                            context=context,
                            run=run,
                            reviewing=reviewing,
                            objective=execution.objective,
                            current_digest=current_digest,
                            dispatch=dispatch,
                            sequence=sequence,
                            attempt=retry_attempts[dispatch.reviewer_id],
                            retry_request=retry_requests[dispatch.reviewer_id],
                        ),
                        retry_dispatches,
                    )
                )
            results_by_id.update(
                (dispatch.reviewer_id, result)
                for dispatch, result in zip(
                    retry_dispatches, retry_results, strict=True
                )
            )
        results = tuple(
            results_by_id[dispatch.reviewer_id] for dispatch in base_dispatches
        )
        return _finish_reviewer_batch(
            context=context,
            run=run,
            reviewing=reviewing,
            reviewer_plan=reviewer_plan,
            current_digest=current_digest,
            dispatches=base_dispatches,
            results=results,
        )
    except WorkerError as error:
        if not activated:
            raise
        if error.code == REVIEWER_BATCH_INCOMPLETE_CODE:
            raise
        failed = transition(reviewing, RunState.FAILED)
        context.store.update(failed, expected_state=RunState.REVIEWING)
        raise
    except BaseException:
        if not activated:
            raise
        failed = transition(reviewing, RunState.FAILED)
        context.store.update(failed, expected_state=RunState.REVIEWING)
        raise


def _resume_review(  # noqa: PLR0911
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
    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)
    execution = _read_execution_record(run_directory, str(run.id))
    if isinstance(execution, ReviewerSetExecutionRecordSchema):
        return _resume_reviewer_set(
            context=context,
            run=run,
            run_directory=run_directory,
            execution=execution,
        )
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
    measured_digest = worktree_digest(digest_worktree, run.worktree_path, run.base_sha)
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
        attempt = next_attempt(
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
        attempt = next_attempt(
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
    request_path = run_evidence_path(
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
    validate_remediation_request(recovery_request, run_directory=run_directory)
    write_json_atomic(request_path, recovery_request, 'remediation_request')
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

    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
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
                write_json_atomic(
                    run_evidence_path(run_directory, 'failure.json'),
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
    retry_request: dict[str, Any] | None = None,
) -> ReviewerDispatchResult:
    """Execute and validate one reviewer without changing workflow state."""

    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
    logs = run_evidence_path(run_directory, 'logs')
    request_path = reviewer_dispatch_path(run_directory, dispatch.paths.request)
    response_path = reviewer_dispatch_path(
        run_directory, dispatch.paths.temporary_result
    )
    result_path = reviewer_dispatch_path(run_directory, dispatch.paths.result)
    artifact_path = reviewer_dispatch_path(run_directory, dispatch.paths.artifact)
    stdout_path = reviewer_dispatch_path(run_directory, dispatch.paths.stdout)
    stderr_path = reviewer_dispatch_path(run_directory, dispatch.paths.stderr)
    metadata_path = reviewer_dispatch_path(
        run_directory, dispatch.paths.runtime_metadata
    )
    request: dict[str, Any] = retry_request or {
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
    validate_review_request(request, run_directory=run_directory)
    if retry_request is None:
        write_json_atomic(request_path, request, 'review_request')
    elif read_json_object(request_path) != request:
        message = 'retry review request differs from canonical evidence'
        raise WorkerError(message)
    started_at = timestamp()
    invocation_id = record_invocation(
        AttemptIdentity(
            run_id=str(run.id),
            role=RuntimeRole.REVIEWER,
            reviewer_id=dispatch.reviewer_id,
            agent=dispatch.identity,
            iteration=reviewing.iteration,
            sequence=request['sequence'],
            attempt=attempt,
            invocation_id=dispatch.invocation_id,
        ),
        ProcessOutcome(
            started_at=started_at, stdout='', stderr='', exit_code=None, finished=False
        ),
        run_directory=run_directory,
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
                runtime_metadata_path=runtime_metadata_path(
                    dispatch.identity, metadata_path, context.registry
                ),
                on_started=partial(
                    record_invocation,
                    AttemptIdentity(
                        run_id=str(run.id),
                        role=RuntimeRole.REVIEWER,
                        reviewer_id=dispatch.reviewer_id,
                        agent=dispatch.identity,
                        iteration=reviewing.iteration,
                        sequence=request['sequence'],
                        invocation_id=invocation_id,
                        attempt=attempt,
                    ),
                    ProcessOutcome(
                        started_at=started_at,
                        stdout=None,
                        stderr=None,
                        exit_code=None,
                        finished=False,
                    ),
                    run_directory=run_directory,
                    lifecycle=AttemptLifecycle(status=AttemptStatus.RUNNING),
                ),
            )
        )
    except subprocess.TimeoutExpired as error:
        models, model_status = exception_runtime_metadata(error)
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.REVIEWER,
                reviewer_id=dispatch.reviewer_id,
                agent=dispatch.identity,
                iteration=reviewing.iteration,
                sequence=request['sequence'],
                invocation_id=invocation_id,
                attempt=attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=error.stdout,
                stderr=error.stderr,
                exit_code=None,
                timed_out=True,
                effective_models=models,
                effective_model_status=model_status,
            ),
            run_directory=run_directory,
        )
        archive_unaccepted_response(
            response_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-result-attempt-{attempt:04d}.json',
            'rejected_review_result',
        )
        archive_unaccepted_response(
            artifact_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-artifact-attempt-{attempt:04d}.md',
            'rejected_review_artifact',
        )
        return ReviewerDispatchResult(
            ReviewerDecision(dispatch.reviewer_id, 'incomplete'), None
        )
    except OSError as error:
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.REVIEWER,
                reviewer_id=dispatch.reviewer_id,
                agent=dispatch.identity,
                iteration=reviewing.iteration,
                sequence=request['sequence'],
                invocation_id=invocation_id,
                attempt=attempt,
            ),
            ProcessOutcome(
                started_at=started_at, stdout='', stderr=str(error), exit_code=None
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(conclusion=AttemptConclusion.FAILED),
        )
        archive_unaccepted_response(
            response_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-result-attempt-{attempt:04d}.json',
            'rejected_review_result',
        )
        archive_unaccepted_response(
            artifact_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-artifact-attempt-{attempt:04d}.md',
            'rejected_review_artifact',
        )
        return ReviewerDispatchResult(
            ReviewerDecision(dispatch.reviewer_id, 'incomplete'), None
        )
    except BaseException as error:
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.REVIEWER,
                reviewer_id=dispatch.reviewer_id,
                agent=dispatch.identity,
                iteration=reviewing.iteration,
                sequence=request['sequence'],
                invocation_id=invocation_id,
                attempt=attempt,
            ),
            ProcessOutcome(
                started_at=started_at, stdout='', stderr=str(error), exit_code=None
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(conclusion=AttemptConclusion.FAILED),
        )
        archive_unaccepted_response(
            response_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-result-attempt-{attempt:04d}.json',
            'rejected_review_result',
        )
        archive_unaccepted_response(
            artifact_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-artifact-attempt-{attempt:04d}.md',
            'rejected_review_artifact',
        )
        raise
    finished_at = timestamp()
    if not completed.succeeded:
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.REVIEWER,
                reviewer_id=dispatch.reviewer_id,
                agent=dispatch.identity,
                iteration=reviewing.iteration,
                sequence=request['sequence'],
                invocation_id=invocation_id,
                attempt=attempt,
            ),
            ProcessOutcome(
                started_at=started_at,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.exit_code,
                finished_at=finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(conclusion=AttemptConclusion.FAILED),
        )
        archive_unaccepted_response(
            response_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-result-attempt-{attempt:04d}.json',
            'rejected_review_result',
        )
        archive_unaccepted_response(
            artifact_path,
            logs
            / f'{dispatch.reviewer_id}-rejected-review-artifact-attempt-{attempt:04d}.md',
            'rejected_review_artifact',
        )
        return ReviewerDispatchResult(
            ReviewerDecision(dispatch.reviewer_id, 'incomplete'), None
        )

    if artifact_path.is_file():
        record_finalized_path(artifact_path, 'review_artifact')
    received_at = timestamp() if response_path.is_file() else None
    validation_started_at = timestamp()
    try:
        response = read_json_object(response_path)
        require_unique_message_id(response, run_directory)
        verdict = validate_review_response(
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
        finalize_temporary_path(
            response_path,
            destination,
            'review_result' if valid else 'rejected_review_result',
        )
    record_invocation(
        AttemptIdentity(
            run_id=str(run.id),
            role=RuntimeRole.REVIEWER,
            reviewer_id=dispatch.reviewer_id,
            agent=dispatch.identity,
            iteration=reviewing.iteration,
            sequence=request['sequence'],
            invocation_id=invocation_id,
            attempt=attempt,
        ),
        ProcessOutcome(
            started_at=started_at,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.exit_code,
            finished_at=finished_at,
            effective_models=completed.effective_models,
            effective_model_status=completed.effective_model_status,
        ),
        run_directory=run_directory,
        lifecycle=AttemptLifecycle(
            conclusion=AttemptConclusion.SUCCEEDED
            if valid
            else AttemptConclusion.FAILED,
            response_received_at=received_at,
            validation_started_at=validation_started_at if received_at else None,
        ),
    )
    message_id = response.get('message_id')
    return ReviewerDispatchResult(
        ReviewerDecision(
            dispatch.reviewer_id, cast('Any', verdict if valid else 'incomplete')
        ),
        message_id if isinstance(message_id, str) else None,
    )


def _finish_reviewer_batch(
    *,
    context: WorkerContext,
    run: Run,
    reviewing: Run,
    reviewer_plan: ReviewerExecutionPlan,
    current_digest: str,
    dispatches: tuple[ReviewerDispatch, ...],
    results: tuple[ReviewerDispatchResult, ...],
) -> Run:
    """Persist one complete aggregate or interrupt an incomplete batch."""

    require_unchanged(
        worktree_digest(context.digest_worktree, run.worktree_path, run.base_sha),
        current_digest,
    )
    require_unique_batch_message_ids(results)
    decision = aggregate_review_batch(tuple(result.decision for result in results))
    if decision.incomplete_reviewers:
        interrupted = transition(reviewing, RunState.INTERRUPTED)
        context.store.update(interrupted, expected_state=RunState.REVIEWING)
        raise WorkerError(
            REVIEWER_BATCH_INCOMPLETE, code=REVIEWER_BATCH_INCOMPLETE_CODE
        )
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
                    'result_path': dispatch.paths.result,
                }
                for dispatch, result in zip(dispatches, results, strict=True)
            ],
            'changes_requested_by': list(decision.changes_requested_by),
            'blocked_by': list(decision.blocked_by),
            'incomplete_reviewers': [],
        }
    )
    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
    write_json_atomic(
        run_evidence_path(
            run_directory,
            *Path(
                evidence_path('review_batch_result', ordinal=reviewing.iteration)
            ).parts,
        ),
        batch_result.model_dump(mode='json'),
        'review_batch_result',
    )
    if decision.verdict == 'blocked':
        failed = transition(reviewing, RunState.FAILED)
        context.store.update(failed, expected_state=RunState.REVIEWING)
        raise WorkerError(
            REVIEWER_BATCH_INCOMPLETE, code=REVIEWER_BATCH_INCOMPLETE_CODE
        )
    decided = transition(
        reviewing,
        RunState.APPROVED
        if decision.verdict == 'approved'
        else RunState.CHANGES_REQUESTED,
    )
    context.store.update(decided, expected_state=RunState.REVIEWING)
    if decision.verdict == 'changes_requested':
        return decided
    awaiting = transition(decided, RunState.AWAITING_COMMIT_AUTHORIZATION)
    context.store.update(awaiting, expected_state=RunState.APPROVED)
    return awaiting


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
    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)
    current_digest = worktree_digest(digest_worktree, run.worktree_path, run.base_sha)
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
        write_json_atomic(
            run_evidence_path(run_directory, 'execution.json'),
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
            run_evidence_path(run_directory, directory).mkdir(
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
        return _finish_reviewer_batch(
            context=context,
            run=run,
            reviewing=reviewing,
            reviewer_plan=reviewer_plan,
            current_digest=current_digest,
            dispatches=dispatches,
            results=results,
        )
    except WorkerError as error:
        if error.code == REVIEWER_BATCH_INCOMPLETE_CODE:
            raise
        failed = transition(reviewing, RunState.FAILED)
        store.update(failed, expected_state=RunState.REVIEWING)
        raise
    except BaseException:
        failed = transition(reviewing, RunState.FAILED)
        store.update(failed, expected_state=RunState.REVIEWING)
        raise


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

    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
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
                write_json_atomic(
                    run_evidence_path(run_directory, 'failure.json'),
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

    run_directory = prepare_run_evidence_directory(runs_directory, str(run.id))
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
                write_json_atomic(
                    run_evidence_path(run_directory, 'failure.json'),
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
