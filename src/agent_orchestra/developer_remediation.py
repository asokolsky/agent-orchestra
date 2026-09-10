"""
Resume the developer remediation half of an interrupted review.

After a reviewer requests changes, the developer is dispatched with the complete
findings and must produce a new diff digest. This module recovers that step from
durable evidence: it reuses an already-persisted remediation request rather than
inventing a new one, and refuses to relaunch an attempt whose activation cannot
be established.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.agents import CommandAgentAdapter, DeveloperRequest
from agent_orchestra.evidence import (
    RESUME_EXECUTION_FAILED_CODE,
    RESUME_INTERRUPTED_CODE,
    WorkerError,
    archive_unaccepted_response,
    finalize_temporary_path,
    invocation_stem,
    manifest_evidence_path,
    read_json_object,
    run_evidence_path,
    worktree_digest,
)
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptIdentity,
    AttemptLifecycle,
    AttemptStatus,
    ProcessOutcome,
    attempt_activation_was_persisted,
    prepare_run_evidence_directory,
    record_invocation,
    timestamp,
)
from agent_orchestra.messages import (
    is_developer_disagreement,
    require_unique_message_id,
    validate_developer_handoff,
    validate_resumed_progress,
)
from agent_orchestra.models import Run, RunState, same_diff_digest, utc_now
from agent_orchestra.queued_review import _run_queued_review
from agent_orchestra.runtime_metadata import (
    exception_runtime_metadata,
    runtime_metadata_path,
)
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from agent_orchestra.execution_context import ReviewPlan, WorkerContext


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

    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
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
