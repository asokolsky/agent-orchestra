"""
Run the bounded single-reviewer loop for one queued immutable diff.

One reviewer evaluates the frozen diff, and on `changes_requested` the developer
remediates and the cycle repeats until approval or a stopping condition. Every
state change is persisted before the next agent starts, so an interrupted run
can be resumed from its durable evidence rather than replayed.

The concurrent reviewer-set path is a separate entry point; this module is the
single-reviewer compatibility path the CLI still exposes.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError

from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.agents import (
    CommandAgentAdapter,
    DeveloperRequest,
    ReviewerRequest,
)
from agent_orchestra.evidence import (
    RESUME_EXECUTION_FAILED_CODE,
    RESUME_INTERRUPTED_CODE,
    WorkerError,
    archive_unaccepted_response,
    finalize_temporary_path,
    invocation_stem,
    manifest_evidence_path,
    read_json_object,
    record_finalized_path,
    require_unchanged,
    run_evidence_path,
    worktree_digest,
    write_json_atomic,
)
from agent_orchestra.execution_context import (
    EMPTY_OBJECTIVE,
    EVIDENCE_INSIDE_WORKTREE,
    INVALID_DEVELOPER_TIMEOUT,
    INVALID_ITERATION_LIMIT,
    ITERATION_LIMIT,
    ReviewPlan,
    WorkerContext,
    _execution_record,
    _resolve_resume_identity,
)
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptIdentity,
    AttemptLifecycle,
    AttemptStatus,
    InvocationIdentity,
    ProcessOutcome,
    attempt_activation_was_persisted,
    prepare_run_evidence_directory,
    record_invocation,
    timestamp,
)
from agent_orchestra.messages import (
    NO_CHANGES,
    classify_remediation_progress,
    is_developer_disagreement,
    require_unique_message_id,
    validate_developer_handoff,
    validate_remediation_request,
    validate_review_request,
    validate_review_response,
)
from agent_orchestra.models import Run, RunState, same_diff_digest, utc_now
from agent_orchestra.runtime_metadata import (
    exception_runtime_metadata,
    runtime_metadata_path,
)
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from collections.abc import Sequence

DEVELOPER_DISAGREEMENT = 'developer disputed every finding without changing the diff'
EMPTY_COMMAND = 'reviewer command must not be empty'


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

    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
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


def run_queued_review(
    *,
    context: WorkerContext,
    run: Run,
    objective: str,
    reviewer_command: Sequence[str],
    developer_command: Sequence[str],
    timeout_seconds: int,
    developer_timeout_seconds: int | None = None,
    max_iterations: int = 3,
    reviewer_identity: InvocationIdentity | None = None,
    developer_identity: InvocationIdentity | None = None,
) -> Run:
    """Run the bounded loop and persist every worker failure as durable evidence."""

    run_directory = prepare_run_evidence_directory(context.runs_directory, str(run.id))
    reviewer_identity = reviewer_identity or InvocationIdentity(
        vendor='unknown', model=None, runtime='custom-command'
    )
    developer_identity = developer_identity or InvocationIdentity(
        vendor='unknown', model=None, runtime='custom-command'
    )
    try:
        return _run_queued_review(
            context=context,
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
