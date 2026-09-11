"""
Dispatch and recover one concurrent required-reviewer batch.

Every required reviewer evaluates the same frozen diff at once, each writing
reviewer-qualified evidence, and the batch reaches one deterministic aggregate
decision before workflow state changes. Recovery preserves validated peer
responses and redispatches only the members that did not finish.

The aggregation policy and its value types live in `review_batch`; this module
executes and recovers a batch rather than deciding its verdict. The
single-reviewer path is separate again, and the two differ deliberately.
"""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from pydantic import ValidationError

from agent_orchestra.adapter.registry import RuntimeRegistry, RuntimeRole
from agent_orchestra.agents import CommandAgentAdapter, ReviewerRequest
from agent_orchestra.evidence import (
    RESUME_ACTIVATION_UNCERTAIN_CODE,
    RESUME_SCOPE_CHANGED_CODE,
    REVIEWER_BATCH_INCOMPLETE_CODE,
    RUN_NOT_RESUMABLE_CODE,
    WorkerError,
    archive_unaccepted_response,
    contained_job_reference,
    finalize_temporary_path,
    manifest_evidence_path,
    read_json_object,
    record_finalized_path,
    require_unchanged,
    reviewer_dispatch_path,
    run_evidence_path,
    worktree_digest,
    write_json_atomic,
    write_text_atomic,
)
from agent_orchestra.execution_context import (
    EMPTY_OBJECTIVE,
    EVIDENCE_INSIDE_WORKTREE,
    INVALID_DEVELOPER_TIMEOUT,
    INVALID_ITERATION_LIMIT,
    ITERATION_LIMIT,
    ReviewerSetReviewPlan,
    WorkerContext,
    _execution_record,
    _identity_from_record,
    _resolve_resume_identity,
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
    failure_conclusion,
    latest_task_attempt,
    next_attempt,
    prepare_run_evidence_directory,
    record_invocation,
    recovery_action,
    timestamp,
)
from agent_orchestra.manifests import canonical_message_evidence, evidence_path
from agent_orchestra.messages import (
    NO_CHANGES,
    is_developer_disagreement,
    require_unique_batch_message_ids,
    require_unique_message_id,
    reviewer_id_from_message_path,
    validate_developer_handoff,
    validate_remediation_request,
    validate_review_request,
    validate_review_response,
)
from agent_orchestra.models import (
    Run,
    RunState,
    same_diff_digest,
    utc_now,
)
from agent_orchestra.queued_review import DEVELOPER_DISAGREEMENT
from agent_orchestra.reports import render_reviewer_batch
from agent_orchestra.review_batch import (
    ReviewerDecision,
    ReviewerDispatchResult,
    aggregate_review_batch,
)
from agent_orchestra.review_fanout import ReviewerDispatch, build_review_fanout
from agent_orchestra.reviewer_paths import (
    ReviewerIdentityError,
    reviewer_evidence_paths,
    reviewer_task_id,
)
from agent_orchestra.reviewer_plan import ReviewerExecution, ReviewerExecutionPlan
from agent_orchestra.runtime_metadata import (
    exception_runtime_metadata,
    runtime_metadata_path,
)
from agent_orchestra.schemas import (
    DeveloperHandoffMessageSchema,
    ReviewerBatchResultSchemaV3,
    ReviewerSetExecutionRecordSchema,
    ReviewResultMessageSchema,
)
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    DeveloperContinuation = Callable[..., Run]

REVIEWER_BATCH_INCOMPLETE = 'reviewer batch did not complete'


def _write_developer_disagreement(
    run_directory: Path, run: Run, handoff_path: Path
) -> None:
    """Persist the stable human-decision marker for a developer disagreement."""

    write_json_atomic(
        run_evidence_path(run_directory, 'decision-required.json'),
        {
            'schema_version': 1,
            'run_id': str(run.id),
            'state': str(RunState.CHANGES_REQUESTED),
            'reason': {
                'code': 'developer_disagreement',
                'message': DEVELOPER_DISAGREEMENT,
            },
            'developer_handoff_path': str(handoff_path),
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        },
        'decision_required',
    )


def _developer_disagreement_is_pending(run_directory: Path, run: Run) -> bool:
    """Return whether durable evidence requires a human disagreement decision."""

    invalid = 'developer disagreement evidence is miscorrelated'
    marker_path = run_evidence_path(run_directory, 'decision-required.json')
    if not marker_path.is_file():
        return False
    marker = read_json_object(marker_path)
    handoff_value = marker.get('developer_handoff_path')
    handoff_path = Path(handoff_value) if isinstance(handoff_value, str) else Path()
    reason = marker.get('reason')
    try:
        contained_job_reference(run_directory, handoff_path, invalid)
    except WorkerError as error:
        raise WorkerError(invalid) from error
    if (
        marker.get('run_id') != str(run.id)
        or marker.get('state') != str(RunState.CHANGES_REQUESTED)
        or not isinstance(reason, dict)
        or reason.get('code') != 'developer_disagreement'
        or not handoff_path.is_file()
    ):
        raise WorkerError(invalid)
    handoff_document = read_json_object(handoff_path)
    try:
        handoff = DeveloperHandoffMessageSchema.model_validate(handoff_document)
    except ValidationError as error:
        raise WorkerError(invalid) from error
    expected_handoff_path = manifest_evidence_path(
        run_directory, 'developer_handoff', handoff.sequence
    )
    request_path = manifest_evidence_path(
        run_directory, 'remediation_request', handoff.sequence - 1
    )
    request = read_json_object(request_path)
    validate_remediation_request(request, run_directory=run_directory)
    try:
        batch = ReviewerBatchResultSchemaV3.model_validate(
            read_json_object(Path(request['payload']['review_result_path']))
        )
        handoff = validate_developer_handoff(
            handoff_document,
            request=request,
            finding_ids=tuple(item.finding_id for item in batch.findings),
        )
    except ValidationError as error:
        raise WorkerError(invalid) from error
    expected_scope = {
        'worktree_path': str(run.worktree_path),
        'base_sha': run.base_sha,
        'head_sha': run.head_sha,
        'diff_digest': run.diff_digest,
    }
    if (
        handoff_path.resolve() != expected_handoff_path.resolve()
        or handoff.run_id != str(run.id)
        or handoff.iteration != run.iteration
        or handoff.scope.model_dump(mode='json') != expected_scope
        or handoff.in_reply_to != request.get('message_id')
        or request.get('run_id') != str(run.id)
        or request.get('iteration') != run.iteration
        or request.get('in_reply_to') != batch.message_id
        or batch.run_id != str(run.id)
        or batch.iteration != run.iteration
        or batch.diff_digest != run.diff_digest
        or not is_developer_disagreement(handoff)
    ):
        raise WorkerError(invalid)
    return True


def _reviewer_set_plan_from_execution(
    execution: ReviewerSetExecutionRecordSchema, registry: RuntimeRegistry
) -> ReviewerSetReviewPlan:
    """Rebuild one complete reviewer-set workflow plan from durable evidence."""

    return ReviewerSetReviewPlan(
        execution.objective,
        _reviewer_plan_from_execution(execution, registry),
        tuple(execution.developer.command),
        execution.developer.timeout_seconds,
        execution.max_review_iterations,
        _resolve_resume_identity(
            _identity_from_record(
                execution.developer.identity.vendor,
                execution.developer.identity.model,
                execution.developer.identity.runtime,
            ),
            RuntimeRole.DEVELOPER,
            registry,
        ),
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
    prior_sequences: list[int] = []
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
            if identity is not None:
                prior_sequences.append(identity[1])
            continue
        sequence = identity[1]
        reviewer_id = reviewer_id_from_message_path(
            path, sequence=sequence, message_type='review_request'
        )
        if reviewer_id is None:
            continue
        request = read_json_object(path)
        if request.get('iteration') != run.iteration:
            prior_sequences.append(sequence)
            continue
        if reviewer_id not in expected_ids or reviewer_id in sequences:
            message = 'reviewer-set request evidence does not match its execution plan'
            raise WorkerError(message)
        sequences[reviewer_id] = sequence
    if not set(sequences).issubset(expected_ids) or len(set(sequences.values())) > 1:
        message = 'reviewer-set request evidence has no single durable sequence'
        raise WorkerError(message)
    if run.iteration == 1 and set(sequences) != expected_ids:
        message = 'reviewer-set request evidence has no single durable sequence'
        raise WorkerError(message)
    if sequences:
        return next(iter(sequences.values()))
    return max(prior_sequences, default=0) + 1


def _prior_reviewer_result_path(
    run_directory: Path, *, run: Run, reviewer_id: str
) -> Path | None:
    """Return the preceding iteration's canonical result for one reviewer."""

    if run.iteration == 1:
        return None
    candidates: list[Path] = []
    for path in run_evidence_path(run_directory, 'messages').iterdir():
        identity = canonical_message_evidence(
            path.relative_to(run_directory).as_posix()
        )
        if identity is None or identity[0] != 'review_result':
            continue
        path_reviewer = reviewer_id_from_message_path(
            path, sequence=identity[1], message_type='review_result'
        )
        if path_reviewer != reviewer_id:
            continue
        document = read_json_object(path)
        if document.get('iteration') == run.iteration - 1:
            candidates.append(path)
    if len(candidates) != 1:
        message = 'reviewer-set prior result evidence is incomplete'
        raise WorkerError(message)
    return candidates[0]


def _completed_reviewer_result(
    run_directory: Path,
    dispatch: ReviewerDispatch,
    *,
    run: Run,
    objective: str,
    current_digest: str,
    expected_prior_review_path: Path | None,
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
        expected_prior_review_path=expected_prior_review_path,
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
    expected_prior_review_path: Path | None,
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
        or payload['prior_review_path']
        != (
            str(expected_prior_review_path)
            if expected_prior_review_path is not None
            else None
        )
    ):
        message = 'reviewer-set request does not match its execution plan'
        raise WorkerError(message)


def _validate_reviewer_set_remediation(
    *,
    run: Run,
    run_directory: Path,
    execution: ReviewerSetExecutionRecordSchema,
    sequence: int,
    request_path: Path,
    request: dict[str, Any],
) -> ReviewerBatchResultSchemaV3:
    """Bind reviewer-set remediation authority to canonical durable evidence."""

    validate_remediation_request(request, run_directory=run_directory)
    batch_path = Path(request['payload']['review_result_path']).resolve()
    expected_batch_path = run_evidence_path(
        run_directory,
        *Path(evidence_path('review_batch_result', ordinal=run.iteration)).parts,
    )
    batch = ReviewerBatchResultSchemaV3.model_validate(read_json_object(batch_path))
    result_sequences: set[int] = set()
    expected_findings: list[dict[str, object]] = []
    for reviewer in batch.reviewers:
        if reviewer.result_path is None:
            continue
        identity = canonical_message_evidence(reviewer.result_path)
        if identity is None or identity[0] != 'review_result':
            message = 'reviewer-set remediation batch member is miscorrelated'
            raise WorkerError(message)
        result_sequences.add(identity[1])
        result_path = run_evidence_path(
            run_directory, *Path(reviewer.result_path).parts
        )
        result_document = read_json_object(result_path)
        result = ReviewResultMessageSchema.model_validate(result_document)
        expected_paths = reviewer_evidence_paths(
            sequence=identity[1] - 1,
            iteration=batch.iteration,
            reviewer_id=reviewer.reviewer_id,
            attempt=1,
        )
        reviewer_request = read_json_object(
            run_evidence_path(run_directory, *Path(expected_paths.request).parts)
        )
        reviewer_artifact_path = run_evidence_path(
            run_directory, *Path(expected_paths.artifact).parts
        )
        validate_review_request(reviewer_request, run_directory=run_directory)
        validate_review_response(
            result_document,
            request=reviewer_request,
            artifact_path=reviewer_artifact_path,
        )
        if (
            reviewer.result_path != expected_paths.result
            or result.run_id != batch.run_id
            or result.iteration != batch.iteration
            or result.scope.diff_digest != batch.diff_digest
            or result.payload.verdict != reviewer.outcome
        ):
            message = 'reviewer-set remediation batch member is miscorrelated'
            raise WorkerError(message)
        if reviewer.outcome == 'changes_requested':
            for finding in result.payload.findings:
                source = finding.model_dump(mode='json')
                source_id = finding.finding_id
                expected_findings.append(
                    {
                        **source,
                        'finding_id': f'{reviewer.reviewer_id}:{source_id}',
                        'reviewer_id': reviewer.reviewer_id,
                        'source_finding_id': source_id,
                    }
                )
    expected_scope = {
        'worktree_path': str(run.worktree_path),
        'base_sha': run.base_sha,
        'head_sha': run.head_sha,
        'diff_digest': batch.diff_digest,
    }
    first_sequence = (
        next(iter(result_sequences)) + 1 if len(result_sequences) == 1 else 0
    )
    canonical_request = manifest_evidence_path(
        run_directory, 'remediation_request', sequence
    )
    artifact_path = run_evidence_path(run_directory, *Path(batch.artifact_path).parts)
    if (
        request_path.resolve() != canonical_request.resolve()
        or request.get('run_id') != str(run.id)
        or request.get('iteration') != run.iteration
        or request.get('sequence') != sequence
        or request.get('scope') != expected_scope
        or request['payload']['objective'] != execution.objective
        or request['payload']['timeout_seconds'] != execution.developer.timeout_seconds
        or batch_path != expected_batch_path.resolve()
        or request['payload']['review_artifact_path'] != str(artifact_path)
        or batch.message_id != request['in_reply_to']
        or batch.run_id != str(run.id)
        or batch.iteration != run.iteration
        or (
            run.state is not RunState.VALIDATION_REQUIRED
            and batch.diff_digest != run.diff_digest
        )
        or batch.reviewer_set_id != execution.reviewer_plan.reviewer_set_id
        or [item.reviewer_id for item in batch.reviewers]
        != [item.reviewer_id for item in execution.reviewer_plan.reviewers]
        or batch.model_dump(mode='json')['findings'] != expected_findings
        or batch.verdict != 'changes_requested'
        or sequence < first_sequence
        or (sequence - first_sequence) % 2 != 0
    ):
        message = 'reviewer-set remediation request is miscorrelated'
        raise WorkerError(message)
    return batch


def _resume_reviewer_set(  # noqa: PLR0911
    *,
    context: WorkerContext,
    run: Run,
    run_directory: Path,
    execution: ReviewerSetExecutionRecordSchema,
    resume_developer_request: DeveloperContinuation,
    resume_developer_validation: DeveloperContinuation,
) -> Run:
    """Resume only incomplete members of one immutable reviewer batch."""

    plan = _reviewer_set_plan_from_execution(execution, context.registry)
    if run.state is RunState.CHANGES_REQUESTED:
        if _developer_disagreement_is_pending(run_directory, run):
            return run
        if run.iteration >= plan.max_iterations:
            failed = transition(run, RunState.FAILED)
            context.store.update(failed, expected_state=RunState.CHANGES_REQUESTED)
            raise WorkerError(ITERATION_LIMIT)
        if not plan.developer_command:
            return run
        current_digest = worktree_digest(
            context.digest_worktree, run.worktree_path, run.base_sha
        )
        if current_digest is None or not same_diff_digest(
            current_digest, run.diff_digest
        ):
            message = 'resume scope changed before reviewer-set remediation'
            raise WorkerError(message, code=RESUME_SCOPE_CHANGED_CODE)
        batch_path = run_evidence_path(
            run_directory,
            *Path(evidence_path('review_batch_result', ordinal=run.iteration)).parts,
        )
        batch = ReviewerBatchResultSchemaV3.model_validate(read_json_object(batch_path))
        result_sequences = {
            identity[1]
            for reviewer in batch.reviewers
            if reviewer.result_path is not None
            and (identity := canonical_message_evidence(reviewer.result_path))
            is not None
            and identity[0] == 'review_result'
        }
        if len(result_sequences) != 1:
            message = 'reviewer-set remediation request is miscorrelated'
            raise WorkerError(message)
        sequence = next(iter(result_sequences)) + 1
        request_path = manifest_evidence_path(
            run_directory, 'remediation_request', sequence
        )
        if request_path.is_file():
            request = read_json_object(request_path)
        else:
            artifact_path = run_evidence_path(
                run_directory, *Path(batch.artifact_path).parts
            )
            request = {
                'schema_version': 1,
                'message_id': str(uuid4()),
                'in_reply_to': batch.message_id,
                'run_id': str(run.id),
                'sequence': sequence,
                'iteration': run.iteration,
                'message_type': 'remediation_request',
                'sender': 'orchestrator',
                'recipient': 'developer',
                'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
                'scope': {
                    'worktree_path': str(run.worktree_path),
                    'base_sha': run.base_sha,
                    'head_sha': run.head_sha,
                    'diff_digest': current_digest,
                },
                'payload': {
                    'objective': plan.objective,
                    'allowed_actions': [],
                    'timeout_seconds': plan.developer_timeout_seconds,
                    'review_result_path': str(batch_path),
                    'review_artifact_path': str(artifact_path),
                },
            }
            validate_remediation_request(request, run_directory=run_directory)
            write_json_atomic(request_path, request, 'remediation_request')
        _validate_reviewer_set_remediation(
            run=run,
            run_directory=run_directory,
            execution=execution,
            sequence=sequence,
            request_path=request_path,
            request=request,
        )
        return resume_developer_request(
            context=context,
            plan=plan,
            run=transition(run, RunState.DEVELOPING),
            request=request,
            current_digest=current_digest,
            allow_unchanged_ready=False,
            attempt=1,
            resume_expected_state=RunState.CHANGES_REQUESTED,
        )

    developer_requests = [
        (identity[1], path, read_json_object(path))
        for path in run_evidence_path(run_directory, 'messages').glob(
            '*-remediation-request.json'
        )
        if (
            identity := canonical_message_evidence(
                path.relative_to(run_directory).as_posix()
            )
        )
        is not None
        and identity[0] == 'remediation_request'
    ]
    interrupted_developer = run.state is RunState.INTERRUPTED and (
        context.store.interrupted_origin(str(run.id)) is RunState.DEVELOPING
    )
    if interrupted_developer:
        if not developer_requests:
            message = 'interrupted reviewer-set developer request is missing'
            raise WorkerError(message)
        sequence, request_path, request = max(
            developer_requests, key=lambda item: item[0]
        )
        _validate_reviewer_set_remediation(
            run=run,
            run_directory=run_directory,
            execution=execution,
            sequence=sequence,
            request_path=request_path,
            request=request,
        )
        current_digest = worktree_digest(
            context.digest_worktree, run.worktree_path, run.base_sha
        )
        if current_digest is None:
            raise WorkerError(NO_CHANGES)
        return resume_developer_request(
            context=context,
            plan=plan,
            run=replace(run, state=RunState.DEVELOPING, updated_at=utc_now()),
            request=request,
            current_digest=current_digest,
            allow_unchanged_ready=False,
            attempt=next_attempt(
                run_directory, sequence, str(request['recipient']), run.state
            ),
            resume_expected_state=RunState.INTERRUPTED,
        )

    if run.state is RunState.DEVELOPING:
        if not developer_requests:
            message = 'active reviewer-set developer request is missing'
            raise WorkerError(message)
        sequence, request_path, request = max(
            developer_requests, key=lambda item: item[0]
        )
        _validate_reviewer_set_remediation(
            run=run,
            run_directory=run_directory,
            execution=execution,
            sequence=sequence,
            request_path=request_path,
            request=request,
        )
        latest = latest_task_attempt(run_directory, sequence, RuntimeRole.DEVELOPER)
        temporary = run_evidence_path(run_directory, '.developer-handoff.json')
        canonical = manifest_evidence_path(
            run_directory, 'developer_handoff', sequence + 1
        )
        action = recovery_action(
            latest,
            response_artifact_present=temporary.is_file() or canonical.is_file(),
            workflow_state=run.state,
        )
        if action is RecoveryAction.LAUNCH:
            return resume_developer_request(
                context=context,
                plan=plan,
                run=run,
                request=request,
                current_digest=run.diff_digest or '',
                allow_unchanged_ready=False,
                attempt=1,
            )
        if action is RecoveryAction.FAIL_ACTIVATION_UNCERTAIN or latest is None:
            message = 'cannot resume task with uncertain active attempt'
            raise WorkerError(message, code=RESUME_ACTIVATION_UNCERTAIN_CODE)
        if action is RecoveryAction.NONE:
            return run
        return resume_developer_validation(
            context=context,
            run=run,
            request=request,
            record=latest,
            action=action,
            execution=execution,
            reviewer_identity=None,
            developer_identity=plan.developer_identity,
        )

    if run.state is RunState.VALIDATION_REQUIRED:
        if not developer_requests:
            message = 'reviewer-set validation recovery request is missing'
            raise WorkerError(message)
        sequence, request_path, request = max(
            developer_requests, key=lambda item: item[0]
        )
        batch = _validate_reviewer_set_remediation(
            run=run,
            run_directory=run_directory,
            execution=execution,
            sequence=sequence,
            request_path=request_path,
            request=request,
        )
        handoff_path = manifest_evidence_path(
            run_directory, 'developer_handoff', sequence + 1
        )
        handoff = read_json_object(handoff_path)
        parsed = validate_developer_handoff(
            handoff,
            request=request,
            finding_ids=tuple(item.finding_id for item in batch.findings),
        )
        if parsed.payload.status not in {'blocked', 'failed'}:
            message = 'reviewer-set validation handoff is not recoverable'
            raise WorkerError(message)
        measured_digest = worktree_digest(
            context.digest_worktree, run.worktree_path, run.base_sha
        )
        if measured_digest is None or not same_diff_digest(
            measured_digest, run.diff_digest
        ):
            message = 'resume scope changed since validation became required'
            raise WorkerError(message, code=RESUME_SCOPE_CHANGED_CODE)
        next_sequence = sequence + 2
        recovery_path = manifest_evidence_path(
            run_directory, 'remediation_request', next_sequence
        )
        recovery_request = {
            **request,
            'message_id': str(uuid4()),
            'sequence': next_sequence,
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        }
        validate_remediation_request(recovery_request, run_directory=run_directory)
        write_json_atomic(recovery_path, recovery_request, 'remediation_request')
        return resume_developer_request(
            context=context,
            plan=plan,
            run=transition(run, RunState.DEVELOPING),
            request=recovery_request,
            current_digest=measured_digest,
            allow_unchanged_ready=True,
            attempt=1,
            resume_expected_state=RunState.VALIDATION_REQUIRED,
        )

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
    reviewer_plan = plan.reviewer_plan
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
    retry_requests: dict[str, dict[str, Any] | None] = {}
    prior_review_paths: dict[str, Path | None] = {}
    retry_attempts: dict[str, int] = {}
    retry_dispatches: list[ReviewerDispatch] = []
    activated = False
    try:
        for base_dispatch in base_dispatches:
            prior_review_path = _prior_reviewer_result_path(
                run_directory, run=run, reviewer_id=base_dispatch.reviewer_id
            )
            completed = _completed_reviewer_result(
                run_directory,
                base_dispatch,
                run=run,
                objective=execution.objective,
                current_digest=current_digest,
                expected_prior_review_path=prior_review_path,
            )
            if completed is not None:
                result, _ = completed
                results_by_id[base_dispatch.reviewer_id] = result
                continue
            request_path = reviewer_dispatch_path(
                run_directory, base_dispatch.paths.request
            )
            current_request = (
                read_json_object(request_path) if request_path.is_file() else None
            )
            artifact_path = reviewer_dispatch_path(
                run_directory, base_dispatch.paths.artifact
            )
            if current_request is not None:
                validate_review_request(current_request, run_directory=run_directory)
                _validate_reviewer_set_request_scope(
                    current_request,
                    run=run,
                    objective=execution.objective,
                    current_digest=current_digest,
                    sequence=sequence,
                    dispatch=base_dispatch,
                    artifact_path=artifact_path,
                    expected_prior_review_path=prior_review_path,
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
            retry_requests[retry_dispatch.reviewer_id] = current_request
            prior_review_paths[retry_dispatch.reviewer_id] = prior_review_path
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
                            prior_review_path=prior_review_paths[dispatch.reviewer_id],
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
            plan=_reviewer_set_plan_from_execution(execution, context.registry),
            current_digest=current_digest,
            batch_sequence=sequence,
            dispatches=base_dispatches,
            results=results,
            resume_developer_request=resume_developer_request,
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
        terminal = transition(
            reviewing,
            RunState.INTERRUPTED if reviewing.iteration > 1 else RunState.FAILED,
        )
        context.store.update(terminal, expected_state=RunState.REVIEWING)
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
    prior_review_path: Path | None = None,
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
            'prior_review_path': (
                str(prior_review_path) if prior_review_path is not None else None
            ),
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
        metadata = exception_runtime_metadata(error)
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
                effective_models=metadata.effective_models,
                effective_model_status=metadata.effective_model_status,
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
                timed_out=completed.timed_out,
                finished_at=finished_at,
                effective_models=completed.effective_models,
                effective_model_status=completed.effective_model_status,
            ),
            run_directory=run_directory,
            lifecycle=AttemptLifecycle(
                conclusion=failure_conclusion(completed.timed_out)
            ),
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
            timed_out=completed.timed_out,
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
    plan: ReviewerSetReviewPlan,
    current_digest: str,
    batch_sequence: int,
    dispatches: tuple[ReviewerDispatch, ...],
    results: tuple[ReviewerDispatchResult, ...],
    resume_developer_request: DeveloperContinuation,
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
    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
    aggregate_findings: list[dict[str, object]] = []
    for dispatch, result in zip(dispatches, results, strict=True):
        if result.decision.outcome != 'changes_requested':
            continue
        response = read_json_object(
            reviewer_dispatch_path(run_directory, dispatch.paths.result)
        )
        for finding in response['payload']['findings']:
            source_finding_id = str(finding['finding_id'])
            aggregate_findings.append(
                {
                    **finding,
                    'finding_id': f'{dispatch.reviewer_id}:{source_finding_id}',
                    'reviewer_id': dispatch.reviewer_id,
                    'source_finding_id': source_finding_id,
                }
            )
    artifact_path = run_evidence_path(
        run_directory, 'artifacts', f'review-batch-{reviewing.iteration:04d}.md'
    )
    batch_result = ReviewerBatchResultSchemaV3.model_validate(
        {
            'schema_version': 3,
            'message_id': str(uuid4()),
            'run_id': str(run.id),
            'iteration': reviewing.iteration,
            'reviewer_set_id': plan.reviewer_plan.reviewer_set_id,
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
            'findings': aggregate_findings,
            'artifact_path': artifact_path.relative_to(run_directory).as_posix(),
        }
    )
    write_text_atomic(
        artifact_path,
        render_reviewer_batch(batch_result),
        evidence_type='review_artifact',
    )
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
    if decision.verdict == 'changes_requested':
        if reviewing.iteration >= plan.max_iterations:
            context.store.update(decided, expected_state=RunState.REVIEWING)
            failed = transition(decided, RunState.FAILED)
            context.store.update(failed, expected_state=RunState.CHANGES_REQUESTED)
            raise WorkerError(ITERATION_LIMIT)
        context.store.update(decided, expected_state=RunState.REVIEWING)
        if not plan.developer_command:
            return decided
        sequence = batch_sequence + 2
        batch_path = run_evidence_path(
            run_directory,
            *Path(
                evidence_path('review_batch_result', ordinal=reviewing.iteration)
            ).parts,
        )
        remediation_path = manifest_evidence_path(
            run_directory, 'remediation_request', sequence
        )
        remediation: dict[str, Any] = {
            'schema_version': 1,
            'message_id': str(uuid4()),
            'in_reply_to': batch_result.message_id,
            'run_id': str(run.id),
            'sequence': sequence,
            'iteration': reviewing.iteration,
            'message_type': 'remediation_request',
            'sender': 'orchestrator',
            'recipient': 'developer',
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
            'scope': {
                'worktree_path': str(run.worktree_path),
                'base_sha': run.base_sha,
                'head_sha': run.head_sha,
                'diff_digest': current_digest,
            },
            'payload': {
                'objective': plan.objective,
                'allowed_actions': [],
                'timeout_seconds': plan.developer_timeout_seconds,
                'review_result_path': str(batch_path),
                'review_artifact_path': str(artifact_path),
            },
        }
        validate_remediation_request(remediation, run_directory=run_directory)
        write_json_atomic(remediation_path, remediation, 'remediation_request')
        developing = transition(decided, RunState.DEVELOPING)
        context.store.update(developing, expected_state=RunState.CHANGES_REQUESTED)
        return resume_developer_request(
            context=context,
            plan=plan,
            run=developing,
            request=remediation,
            current_digest=current_digest,
            allow_unchanged_ready=False,
            attempt=1,
        )
    context.store.update(decided, expected_state=RunState.REVIEWING)
    awaiting = transition(decided, RunState.AWAITING_COMMIT_AUTHORIZATION)
    context.store.update(awaiting, expected_state=RunState.APPROVED)
    return awaiting


def _run_reviewer_set_iteration(
    *,
    context: WorkerContext,
    run: Run,
    reviewing: Run,
    plan: ReviewerSetReviewPlan,
    current_digest: str,
    sequence: int,
    prior_review_paths: dict[str, Path] | None = None,
    resume_developer_request: DeveloperContinuation,
) -> Run:
    """Execute one complete concurrent reviewer-set iteration."""

    dispatches = build_review_fanout(
        plan.reviewer_plan,
        run_id=str(run.id),
        sequence=sequence,
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
                    objective=plan.objective,
                    current_digest=current_digest,
                    dispatch=dispatch,
                    sequence=sequence,
                    attempt=1,
                    prior_review_path=(
                        prior_review_paths.get(dispatch.reviewer_id)
                        if prior_review_paths is not None
                        else None
                    ),
                ),
                dispatches,
            )
        )
    return _finish_reviewer_batch(
        context=context,
        run=run,
        reviewing=reviewing,
        plan=plan,
        current_digest=current_digest,
        batch_sequence=sequence,
        dispatches=dispatches,
        results=results,
        resume_developer_request=resume_developer_request,
    )


def _run_queued_reviewer_set(
    *,
    context: WorkerContext,
    run: Run,
    objective: str,
    reviewer_plan: ReviewerExecutionPlan,
    developer_command: Sequence[str],
    developer_timeout_seconds: int,
    max_iterations: int,
    developer_identity: InvocationIdentity,
    resume_developer_request: DeveloperContinuation,
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
    resolved_reviewers = tuple(
        replace(
            reviewer,
            identity=_resolve_resume_identity(
                reviewer.identity, RuntimeRole.REVIEWER, context.registry
            ),
        )
        for reviewer in reviewer_plan.reviewers
    )
    reviewer_plan = replace(reviewer_plan, reviewers=resolved_reviewers)
    developer_identity = _resolve_resume_identity(
        developer_identity, RuntimeRole.DEVELOPER, context.registry
    )
    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)
    current_digest = worktree_digest(
        context.digest_worktree, run.worktree_path, run.base_sha
    )
    if current_digest is None:
        raise WorkerError(NO_CHANGES)
    prepared = replace(
        transition(run, RunState.PREPARING),
        diff_digest=current_digest,
        updated_at=utc_now(),
    )
    context.store.update(prepared, expected_state=RunState.QUEUED)
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
        context.store.update(reviewing, expected_state=RunState.PREPARING)
    except BaseException:
        failed = transition(prepared, RunState.FAILED)
        context.store.update(failed, expected_state=RunState.PREPARING)
        raise
    try:
        for directory in ('artifacts', 'logs', 'invocations'):
            run_evidence_path(run_directory, directory).mkdir(
                parents=True, exist_ok=True
            )
        return _run_reviewer_set_iteration(
            context=context,
            run=run,
            reviewing=reviewing,
            plan=plan,
            current_digest=current_digest,
            sequence=1,
            resume_developer_request=resume_developer_request,
        )
    except WorkerError as error:
        if error.code == REVIEWER_BATCH_INCOMPLETE_CODE:
            raise
        durable = context.store.get(str(run.id))
        if durable.state is RunState.REVIEWING:
            failed = transition(durable, RunState.FAILED)
            context.store.update(failed, expected_state=RunState.REVIEWING)
        raise
    except BaseException:
        durable = context.store.get(str(run.id))
        if durable.state is RunState.REVIEWING:
            terminal = transition(
                durable,
                RunState.INTERRUPTED if durable.iteration > 1 else RunState.FAILED,
            )
            context.store.update(terminal, expected_state=RunState.REVIEWING)
        raise


def run_queued_reviewer_set(
    *,
    context: WorkerContext,
    run: Run,
    objective: str,
    reviewer_plan: ReviewerExecutionPlan,
    developer_command: Sequence[str],
    developer_timeout_seconds: int,
    max_iterations: int,
    developer_identity: InvocationIdentity,
    resume_developer_request: DeveloperContinuation,
) -> Run:
    """Run a reviewer batch and persist every worker failure as durable evidence."""

    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
    try:
        return _run_queued_reviewer_set(
            context=context,
            run=run,
            objective=objective,
            reviewer_plan=reviewer_plan,
            developer_command=developer_command,
            developer_timeout_seconds=developer_timeout_seconds,
            max_iterations=max_iterations,
            developer_identity=developer_identity,
            resume_developer_request=resume_developer_request,
        )
    except WorkerError as error:
        if not run_directory.is_relative_to(run.worktree_path.resolve()):
            try:
                durable_run = context.store.get(str(run.id))
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
