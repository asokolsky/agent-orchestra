"""Execute one durable, bounded local review step."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Never
from uuid import uuid4

from pydantic import ValidationError

from agent_orchestra.adapter.registry import (
    RuntimeRole,
)
from agent_orchestra.developer_remediation import _resume_developer_request
from agent_orchestra.evidence import (
    RESUME_ACTIVATION_UNCERTAIN_CODE,
    RESUME_CANCELLED_CODE,
    RESUME_EXECUTION_FAILED_CODE,
    RESUME_INTERRUPTED_CODE,
    RESUME_METADATA_UNSUPPORTED_CODE,
    RESUME_SCOPE_CHANGED_CODE,
    RUN_NOT_RESUMABLE_CODE,
    WorkerError,
    finalize_temporary_path,
    manifest_evidence_path,
    read_json_object,
    require_unchanged,
    run_evidence_path,
    worktree_digest,
    write_json_atomic,
)
from agent_orchestra.execution_context import (
    EVIDENCE_INSIDE_WORKTREE,
    ITERATION_LIMIT,
    ResumedExecution,
    ReviewerRound,
    ReviewerSetReviewPlan,
    ReviewPlan,
    WorkerContext,
    _identity_from_record,
    _resolve_resume_identity,
)
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptStatus,
    InvocationRecord,
    RecoveryAction,
    attempt_record_path,
    latest_task_attempt,
    next_attempt,
    persist_attempt_record,
    prepare_run_evidence_directory,
    recovery_action,
    timestamp,
    transition_attempt,
)
from agent_orchestra.manifests import evidence_path
from agent_orchestra.messages import (
    NO_CHANGES,
    classify_remediation_progress,
    is_developer_disagreement,
    read_message_chain,
    require_unique_message_id,
    validate_developer_handoff,
    validate_remediation_request,
    validate_review_response,
)
from agent_orchestra.models import Run, RunState, same_diff_digest, utc_now
from agent_orchestra.queued_review import _run_queued_review
from agent_orchestra.reviewer_batch_run import (
    _resume_reviewer_set,
    _reviewer_set_plan_from_execution,
    _run_reviewer_set_iteration,
    _write_developer_disagreement,
)
from agent_orchestra.reviewer_batch_run import (
    run_queued_reviewer_set as _run_queued_reviewer_set,
)
from agent_orchestra.schemas import (
    EXECUTION_RECORD_ADAPTER,
    DeveloperHandoffMessageSchema,
    ExecutionRecord,
    ReviewerBatchResultSchemaV3,
    ReviewerSetExecutionRecordSchema,
)
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from agent_orchestra.store import JobStore


def _continue_reviewer_set_after_developer(
    *,
    context: WorkerContext,
    review_round: ReviewerRound,
    plan: ReviewerSetReviewPlan,
    review_result: dict[str, Any],
) -> Run:
    """Start the next reviewer batch after one accepted developer handoff."""

    run_directory = prepare_run_evidence_directory(
        context.runs_directory, str(review_round.run.id)
    )
    prior_review_paths = {
        member['reviewer_id']: run_evidence_path(
            run_directory, *Path(member['result_path']).parts
        )
        for member in review_result['reviewers']
        if member['result_path'] is not None
    }
    return _run_reviewer_set_iteration(
        context=context,
        review_round=ReviewerRound(
            run=review_round.run,
            reviewing=review_round.reviewing,
            current_digest=review_round.current_digest,
            sequence=review_round.sequence,
        ),
        plan=plan,
        prior_review_paths=prior_review_paths,
        resume_developer_request=_resume_reviewer_set_developer_request,
    )


def _resume_reviewer_set_developer_request(**kwargs: Any) -> Run:
    """Resume developer work with reviewer-set continuation hooks."""

    return _resume_developer_request(
        **kwargs,
        reviewer_set_continuation=_continue_reviewer_set_after_developer,
        disagreement_recorder=_write_developer_disagreement,
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
    resumed: ResumedExecution,
) -> Run:
    """Revalidate a durable reviewer response and continue without relaunching."""
    store = context.store
    digest_worktree = context.digest_worktree

    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
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
    # Same ordering as the run path: a recovered run with no developer command
    # cannot remediate, so its exhausted budget must not turn the durable
    # changes_requested outcome into a failure on resume.
    if not resumed.record.developer.command:
        return decided
    if run.iteration >= resumed.record.max_review_iterations:
        failed = transition(decided, RunState.FAILED)
        store.update(failed, expected_state=RunState.CHANGES_REQUESTED)
        raise WorkerError(ITERATION_LIMIT)
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
            'objective': resumed.record.objective,
            'allowed_actions': [],
            'timeout_seconds': resumed.record.developer.timeout_seconds,
            'review_result_path': str(canonical),
            'review_artifact_path': str(review_artifact_path),
        },
    }
    validate_remediation_request(remediation, run_directory=run_directory)
    write_json_atomic(remediation_path, remediation, 'remediation_request')
    developing = transition(decided, RunState.DEVELOPING)
    return _resume_developer_request(
        context=context,
        plan=resumed.review_plan(),
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
    resumed: ResumedExecution,
) -> Run:
    """Revalidate a durable developer response and continue without relaunching."""
    store = context.store
    digest_worktree = context.digest_worktree

    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
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
    findings = (
        review_result['findings']
        if isinstance(resumed.record, ReviewerSetExecutionRecordSchema)
        else review_result['payload']['findings']
    )
    finding_ids = tuple(finding['finding_id'] for finding in findings)
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
        if isinstance(resumed.record, ReviewerSetExecutionRecordSchema):
            _write_developer_disagreement(run_directory, disagreement, canonical)
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
    if isinstance(resumed.record, ReviewerSetExecutionRecordSchema):
        batch = ReviewerBatchResultSchemaV3.model_validate(review_result)
        return _continue_reviewer_set_after_developer(
            context=context,
            review_round=ReviewerRound(
                run=run,
                reviewing=reviewing,
                current_digest=new_digest,
                sequence=sequence + 2,
            ),
            plan=_reviewer_set_plan_from_execution(resumed.record, context.registry),
            review_result=batch.model_dump(mode='json'),
        )
    assert resumed.reviewer_identity is not None
    return _run_queued_review(
        context=context,
        plan=resumed.review_plan(),
        run=reviewing,
        continuation_sequence=sequence + 2,
        continuation_prior_review_path=review_result_path,
    )


def _resume_active_attempt(
    *,
    context: WorkerContext,
    run: Run,
    chain: tuple[tuple[Path, dict[str, Any]], ...],
    resumed: ResumedExecution,
) -> Run:
    """Recover an active workflow state from its latest durable task evidence."""

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
    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
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
                plan=resumed.review_plan(),
                run=run,
                continuation_sequence=sequence,
                continuation_prior_review_path=None,
                retry_review_request=request,
            )
        return _resume_developer_request(
            context=context,
            plan=resumed.review_plan(),
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
        resumed=resumed,
    )


def _resume_intermediate_state(
    *,
    context: WorkerContext,
    run: Run,
    run_directory: Path,
    chain: list[tuple[Path, dict[str, Any]]],
    resumed: ResumedExecution,
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
    # A review-only run has already reached its outcome, so it is not resumable
    # rather than budget-exhausted. Testing the developer command first keeps
    # resume from rewriting that durable changes_requested into a failure.
    if not resumed.record.developer.command:
        raise WorkerError(
            f'job is not resumable from {run.state}',
            code=RUN_NOT_RESUMABLE_CODE,
        )
    if (
        last_message['message_type'] == 'review_result'
        and last_message['payload']['verdict'] == 'changes_requested'
        and run.iteration >= resumed.record.max_review_iterations
    ):
        failed = transition(run, RunState.FAILED)
        store.update(failed, expected_state=RunState.CHANGES_REQUESTED)
        raise WorkerError(ITERATION_LIMIT)
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
                'objective': resumed.record.objective,
                'allowed_actions': [],
                'timeout_seconds': resumed.record.developer.timeout_seconds,
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
        plan=resumed.review_plan(),
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


def _resume_review(  # noqa: PLR0911
    *,
    context: WorkerContext,
    run: Run,
) -> Run:
    """Resume one recoverable run from its canonical execution evidence."""
    store = context.store
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
    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)
    execution = _read_execution_record(run_directory, str(run.id))
    if isinstance(execution, ReviewerSetExecutionRecordSchema):
        return _resume_reviewer_set(
            context=context,
            run=run,
            run_directory=run_directory,
            execution=execution,
            resume_developer_request=_resume_reviewer_set_developer_request,
            resume_developer_validation=_resume_developer_validation,
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
    resumed_execution = ResumedExecution(
        record=execution,
        developer_identity=developer_identity,
        reviewer_identity=reviewer_identity,
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
            resumed=resumed_execution,
            measured_digest=measured_digest,
        )

    if run.state in {RunState.REVIEWING, RunState.DEVELOPING}:
        if run.state is RunState.REVIEWING and not same_diff_digest(
            measured_digest, run.diff_digest
        ):
            message = 'resume scope changed during active task recovery'
            raise WorkerError(message, code=RESUME_SCOPE_CHANGED_CODE)
        return _resume_active_attempt(
            context=context, run=run, chain=tuple(chain), resumed=resumed_execution
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


def run_queued_reviewer_set(
    *,
    context: WorkerContext,
    run: Run,
    plan: ReviewerSetReviewPlan,
) -> Run:
    """Run a reviewer set with worker-owned developer continuations."""

    return _run_queued_reviewer_set(
        context=context,
        run=run,
        plan=plan,
        resume_developer_request=_resume_reviewer_set_developer_request,
    )


def resume_review(
    *,
    context: WorkerContext,
    run: Run,
) -> Run:
    """Resume one run and persist any recoverable-command failure."""

    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
    try:
        return _resume_review(
            context=context,
            run=run,
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
