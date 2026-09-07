"""Tests for durable task-attempt lifecycle evidence."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptStatus,
    InvocationEvidenceError,
    InvocationRecord,
    RecoveryAction,
    TaskStatus,
    derive_task_status,
    read_records,
    recovery_action,
    transition_attempt,
    validate_attempt_record,
    write_record,
)
from agent_orchestra.models import RunState


def pending_attempt() -> InvocationRecord:
    """Return a valid pending schema-4 attempt record."""

    return InvocationRecord(
        schema_version=4,
        run_id='run',
        task_id='run:000001-reviewer',
        invocation_id='run:000001-reviewer:attempt-0001',
        role='reviewer',
        agent_vendor='openai',
        requested_model=None,
        effective_models=(),
        effective_model_status='unavailable',
        runtime='codex',
        iteration=1,
        started_at='2026-09-07T10:00:00Z',
        finished_at=None,
        exit_code=None,
        timed_out=False,
        interrupted=False,
        stdout_path='/run/stdout.log',
        stderr_path='/run/stderr.log',
        attempt=1,
        status='pending',
        conclusion=None,
    )


def test_attempt_transitions_through_validation_to_success() -> None:
    """Accept the complete pending, running, and successful path."""

    running = replace(
        transition_attempt(pending_attempt(), AttemptStatus.RUNNING), exit_code=0
    )
    completed = transition_attempt(
        running,
        AttemptStatus.COMPLETED,
        conclusion=AttemptConclusion.SUCCEEDED,
        finished_at='2026-09-07T10:01:00Z',
        response_received_at='2026-09-07T10:01:01Z',
        validation_started_at='2026-09-07T10:01:02Z',
    )

    assert completed.status == 'completed'
    assert completed.conclusion == 'succeeded'


def test_attempt_self_transitions_support_progress_and_idempotency() -> None:
    """Use the shared table for running progress and terminal idempotency."""

    running = transition_attempt(pending_attempt(), AttemptStatus.RUNNING)
    running = transition_attempt(running, AttemptStatus.RUNNING)
    completed = transition_attempt(
        running,
        AttemptStatus.COMPLETED,
        conclusion=AttemptConclusion.FAILED,
        finished_at='2026-09-07T10:01:00Z',
    )

    assert (
        transition_attempt(
            completed,
            AttemptStatus.COMPLETED,
            conclusion=AttemptConclusion.FAILED,
            finished_at='2026-09-07T10:01:00Z',
        )
        == completed
    )


def test_spawn_failure_can_complete_directly_from_pending() -> None:
    """Represent a definitive process activation failure without running."""

    completed = transition_attempt(
        pending_attempt(),
        AttemptStatus.COMPLETED,
        conclusion=AttemptConclusion.FAILED,
        finished_at='2026-09-07T10:00:01Z',
    )

    assert completed.conclusion == 'failed'


@pytest.mark.parametrize(
    'conclusion', [AttemptConclusion.SUCCEEDED, AttemptConclusion.TIMED_OUT]
)
def test_pending_rejects_terminal_outcomes_that_require_activation(
    conclusion: AttemptConclusion,
) -> None:
    """Reject success and timeout when process activation never became durable."""

    with pytest.raises(InvocationEvidenceError, match='invalid attempt transition'):
        transition_attempt(
            pending_attempt(),
            AttemptStatus.COMPLETED,
            conclusion=conclusion,
            finished_at='2026-09-07T10:00:01Z',
        )


@pytest.mark.parametrize(
    ('field', 'value', 'message'),
    [
        ('conclusion', 'failed', 'invalid attempt status and conclusion'),
        ('response_received_at', '2026-09-07T09:59:59Z', 'pending attempt'),
        ('task_id', None, 'requires task_id'),
        ('task_id', 'other:000001-reviewer', 'does not match run and role'),
        ('invocation_id', 'random', 'does not match task and attempt'),
        ('status', 'unknown', 'invalid attempt status'),
        ('effective_model_status', 'unknown', 'invalid effective_model_status'),
        ('attempt', 0, 'must be positive'),
        ('exit_code', 1, 'pending attempt'),
    ],
)
def test_invalid_attempt_fields_fail_with_stable_diagnostics(
    field: str, value: object, message: str
) -> None:
    """Reject contradictory identity, lifecycle, and milestone fields."""

    with pytest.raises(InvocationEvidenceError, match=message):
        validate_attempt_record(
            replace(pending_attempt(), **cast('Any', {field: value}))
        )


def test_unknown_attempt_conclusion_fails_with_stable_diagnostic() -> None:
    """Reject a terminal outcome outside the schema-4 vocabulary."""

    running = transition_attempt(pending_attempt(), AttemptStatus.RUNNING)
    invalid = replace(
        running,
        status='completed',
        conclusion=cast('Any', 'unknown'),
        finished_at='2026-09-07T10:00:01Z',
    )

    with pytest.raises(InvocationEvidenceError, match='invalid attempt conclusion'):
        validate_attempt_record(invalid)


def test_succeeded_attempt_requires_zero_exit_code() -> None:
    """Reject success evidence that contradicts the process exit status."""

    running = replace(
        transition_attempt(pending_attempt(), AttemptStatus.RUNNING), exit_code=17
    )

    with pytest.raises(InvocationEvidenceError, match='cannot have nonzero exit_code'):
        transition_attempt(
            running,
            AttemptStatus.COMPLETED,
            conclusion=AttemptConclusion.SUCCEEDED,
            finished_at='2026-09-07T10:01:00Z',
            response_received_at='2026-09-07T10:01:01Z',
            validation_started_at='2026-09-07T10:01:02Z',
        )


@pytest.mark.parametrize(
    ('changes', 'message'),
    [
        (
            {'response_received_at': '2026-09-07T10:01:01Z'},
            'response_received_at requires finished_at',
        ),
        (
            {
                'finished_at': '2026-09-07T10:01:00Z',
                'validation_started_at': '2026-09-07T10:01:01Z',
            },
            'validation_started_at requires response_received_at',
        ),
    ],
)
def test_milestones_require_preceding_durable_evidence(
    changes: dict[str, object], message: str
) -> None:
    """Reject response and validation milestones with missing predecessors."""

    running = transition_attempt(pending_attempt(), AttemptStatus.RUNNING)

    with pytest.raises(InvocationEvidenceError, match=message):
        validate_attempt_record(replace(running, **cast('Any', changes)))


def test_completed_attempt_is_immutable() -> None:
    """Reject transitions out of a terminal attempt."""

    completed = transition_attempt(
        pending_attempt(),
        AttemptStatus.COMPLETED,
        conclusion=AttemptConclusion.FAILED,
        finished_at='2026-09-07T10:00:01Z',
    )

    with pytest.raises(InvocationEvidenceError, match='invalid attempt transition'):
        transition_attempt(completed, AttemptStatus.RUNNING)


def test_persisted_completed_attempt_is_immutable(tmp_path: Path) -> None:
    """Reject replacement of terminal evidence already stored on disk."""

    path = tmp_path / 'attempt.json'
    completed = transition_attempt(
        pending_attempt(),
        AttemptStatus.COMPLETED,
        conclusion=AttemptConclusion.FAILED,
        finished_at='2026-09-07T10:00:01Z',
    )
    write_record(path, pending_attempt())
    write_record(path, completed)

    with pytest.raises(InvocationEvidenceError, match='completed attempt is immutable'):
        write_record(path, replace(completed, exit_code=1))


def test_persisted_completed_attempt_accepts_identical_rewrite(tmp_path: Path) -> None:
    """Make an uncertain but successful terminal write safe to repeat."""

    path = tmp_path / 'invocations/attempt.json'
    completed = transition_attempt(
        pending_attempt(),
        AttemptStatus.COMPLETED,
        conclusion=AttemptConclusion.FAILED,
        finished_at='2026-09-07T10:00:01Z',
    )
    write_record(path, pending_attempt())
    write_record(path, completed)

    write_record(path, completed)

    assert json.loads(path.read_text())['conclusion'] == 'failed'


def test_new_persisted_attempt_must_start_pending(tmp_path: Path) -> None:
    """Enforce the initial creation edge before any later transition."""

    running = transition_attempt(pending_attempt(), AttemptStatus.RUNNING)

    with pytest.raises(InvocationEvidenceError, match='must start pending'):
        write_record(tmp_path / 'attempt.json', running)


def test_concurrent_initial_record_creation_does_not_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a stale first-writer view after another creator wins the path."""

    path = tmp_path / 'attempt.json'
    write_record(path, pending_attempt())
    path_type = type(path)
    real_exists = path_type.exists

    def stale_exists(candidate: Path) -> bool:
        """Simulate checking the destination immediately before another writer wins."""

        return False if candidate == path else real_exists(candidate)

    monkeypatch.setattr(path_type, 'exists', stale_exists)

    with pytest.raises(InvocationEvidenceError, match='attempt record already exists'):
        write_record(path, pending_attempt())


def test_existing_pending_attempt_cannot_be_claimed_again(tmp_path: Path) -> None:
    """Reject a delayed creator after another resumer has claimed the attempt."""

    path = tmp_path / 'attempt.json'
    write_record(path, pending_attempt())

    with pytest.raises(InvocationEvidenceError, match='attempt record already exists'):
        write_record(path, pending_attempt())


def test_task_status_follows_latest_attempt() -> None:
    """Derive current task status without a separate persisted state."""

    first = transition_attempt(
        pending_attempt(),
        AttemptStatus.COMPLETED,
        conclusion=AttemptConclusion.INTERRUPTED,
        finished_at='2026-09-07T10:00:01Z',
    )
    retry = replace(
        pending_attempt(),
        invocation_id='run:000001-reviewer:attempt-0002',
        attempt=2,
        started_at='2026-09-07T10:01:00Z',
    )

    assert derive_task_status((first,)) is TaskStatus.COMPLETED
    assert derive_task_status((first, retry)) is TaskStatus.PENDING


def test_validation_milestone_cannot_precede_response() -> None:
    """Reject reverse ordering for observed response and validation milestones."""

    running = transition_attempt(pending_attempt(), AttemptStatus.RUNNING)
    with pytest.raises(InvocationEvidenceError, match='precedes response_received_at'):
        transition_attempt(
            running,
            AttemptStatus.COMPLETED,
            conclusion=AttemptConclusion.FAILED,
            finished_at='2026-09-07T10:01:00Z',
            response_received_at='2026-09-07T10:01:02Z',
            validation_started_at='2026-09-07T10:01:01Z',
        )


@pytest.mark.parametrize('role', ['developer', 'reviewer', 'issue_reviewer'])
@pytest.mark.parametrize(
    ('origin', 'conclusion', 'response', 'validation'),
    [
        (AttemptStatus.PENDING, AttemptConclusion.FAILED, None, None),
        (AttemptStatus.PENDING, AttemptConclusion.CANCELLED, None, None),
        (AttemptStatus.PENDING, AttemptConclusion.INTERRUPTED, None, None),
        (
            AttemptStatus.RUNNING,
            AttemptConclusion.SUCCEEDED,
            '2026-09-07T10:01:01Z',
            '2026-09-07T10:01:02Z',
        ),
        (AttemptStatus.RUNNING, AttemptConclusion.FAILED, None, None),
        (AttemptStatus.RUNNING, AttemptConclusion.TIMED_OUT, None, None),
        (AttemptStatus.RUNNING, AttemptConclusion.CANCELLED, None, None),
        (AttemptStatus.RUNNING, AttemptConclusion.INTERRUPTED, None, None),
    ],
)
def test_all_terminal_transitions_for_both_roles(
    role: str,
    origin: AttemptStatus,
    conclusion: AttemptConclusion,
    response: str | None,
    validation: str | None,
) -> None:
    """Accept every documented terminal edge for all agent-role attempts."""

    task_id = f'run:000001-{role}'
    record = replace(
        pending_attempt(),
        task_id=task_id,
        invocation_id=f'{task_id}:attempt-0001',
        role=cast('Any', role),
    )
    if origin is AttemptStatus.RUNNING:
        record = transition_attempt(record, AttemptStatus.RUNNING)
    if conclusion is AttemptConclusion.SUCCEEDED:
        record = replace(record, exit_code=0)

    completed = transition_attempt(
        record,
        AttemptStatus.COMPLETED,
        conclusion=conclusion,
        finished_at='2026-09-07T10:01:00Z',
        response_received_at=response,
        validation_started_at=validation,
    )

    assert completed.conclusion == conclusion


@pytest.mark.parametrize(
    ('boundary', 'record', 'response_present', 'workflow_state', 'expected'),
    [
        (1, None, False, RunState.REVIEWING, RecoveryAction.LAUNCH),
        (
            2,
            pending_attempt(),
            False,
            RunState.REVIEWING,
            RecoveryAction.FAIL_ACTIVATION_UNCERTAIN,
        ),
        (
            3,
            pending_attempt(),
            False,
            RunState.REVIEWING,
            RecoveryAction.FAIL_ACTIVATION_UNCERTAIN,
        ),
        (
            4,
            transition_attempt(pending_attempt(), AttemptStatus.RUNNING),
            False,
            RunState.REVIEWING,
            RecoveryAction.FAIL_ACTIVATION_UNCERTAIN,
        ),
        (
            5,
            transition_attempt(pending_attempt(), AttemptStatus.RUNNING),
            True,
            RunState.REVIEWING,
            RecoveryAction.PERSIST_RESPONSE_AND_VALIDATE,
        ),
        (
            6,
            replace(
                transition_attempt(pending_attempt(), AttemptStatus.RUNNING),
                finished_at='2026-09-07T10:01:00Z',
                response_received_at='2026-09-07T10:01:01Z',
            ),
            True,
            RunState.REVIEWING,
            RecoveryAction.VALIDATE_RESPONSE,
        ),
        (
            7,
            replace(
                transition_attempt(pending_attempt(), AttemptStatus.RUNNING),
                finished_at='2026-09-07T10:01:00Z',
                response_received_at='2026-09-07T10:01:01Z',
                validation_started_at='2026-09-07T10:01:02Z',
            ),
            True,
            RunState.REVIEWING,
            RecoveryAction.VALIDATE_RESPONSE,
        ),
        (
            8,
            transition_attempt(
                transition_attempt(pending_attempt(), AttemptStatus.RUNNING),
                AttemptStatus.COMPLETED,
                conclusion=AttemptConclusion.FAILED,
                finished_at='2026-09-07T10:01:00Z',
            ),
            False,
            RunState.REVIEWING,
            RecoveryAction.APPLY_CONCLUSION,
        ),
        (
            9,
            transition_attempt(
                transition_attempt(pending_attempt(), AttemptStatus.RUNNING),
                AttemptStatus.COMPLETED,
                conclusion=AttemptConclusion.FAILED,
                finished_at='2026-09-07T10:01:00Z',
            ),
            False,
            RunState.FAILED,
            RecoveryAction.NONE,
        ),
    ],
    ids=lambda value: f'boundary-{value}' if isinstance(value, int) else None,
)
def test_recovery_boundaries_select_deterministic_actions(
    boundary: int,
    record: InvocationRecord | None,
    response_present: bool,
    workflow_state: RunState,
    expected: RecoveryAction,
) -> None:
    """Cover each crash boundary without implicitly relaunching uncertain work."""

    del boundary
    assert (
        recovery_action(
            record,
            response_artifact_present=response_present,
            workflow_state=workflow_state,
        )
        is expected
    )


def test_legacy_invocation_schema_is_unreadable(tmp_path: Path) -> None:
    """Reject schemas one through three instead of guessing lifecycle fields."""

    manifests = tmp_path / 'invocations'
    manifests.mkdir()
    (manifests / 'legacy.json').write_text('{"schema_version": 3}\n')

    with pytest.raises(InvocationEvidenceError, match='unsupported invocation'):
        read_records(tmp_path, 'run')


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('started_at', 1),
        ('role', []),
        ('attempt', '1'),
    ],
)
def test_read_records_rejects_wrong_primitive_types(
    tmp_path: Path, field: str, value: object
) -> None:
    """Normalize malformed JSON primitives as evidence errors."""

    manifests = tmp_path / 'invocations'
    manifests.mkdir()
    document = asdict(pending_attempt())
    document[field] = value
    (manifests / 'attempt.json').write_text(json.dumps(document))

    with pytest.raises(InvocationEvidenceError, match='invalid invocation record'):
        read_records(tmp_path, 'run')


def test_read_records_rejects_duplicate_task_attempts(tmp_path: Path) -> None:
    """Reject two files that claim the same task and attempt identity."""

    manifests = tmp_path / 'invocations'
    manifests.mkdir()
    record = replace(
        pending_attempt(),
        stdout_path=str(tmp_path / 'logs/stdout.log'),
        stderr_path=str(tmp_path / 'logs/stderr.log'),
    )
    document = json.dumps(asdict(record))
    (manifests / 'first.json').write_text(document)
    (manifests / 'second.json').write_text(document)

    with pytest.raises(InvocationEvidenceError, match='duplicate task attempt'):
        read_records(tmp_path, 'run')


@pytest.mark.parametrize(
    ('field', 'changed_value'),
    [
        ('response_received_at', '2026-09-07T10:01:01.500000Z'),
        ('validation_started_at', '2026-09-07T10:01:03Z'),
    ],
)
def test_persisted_milestones_are_immutable(
    tmp_path: Path, field: str, changed_value: str
) -> None:
    """Reject changes to response and validation timestamps once stored."""

    path = tmp_path / 'attempt.json'
    running = replace(
        transition_attempt(pending_attempt(), AttemptStatus.RUNNING),
        finished_at='2026-09-07T10:01:00Z',
        response_received_at='2026-09-07T10:01:01Z',
        validation_started_at='2026-09-07T10:01:02Z',
    )
    write_record(path, pending_attempt())
    write_record(path, transition_attempt(pending_attempt(), AttemptStatus.RUNNING))
    write_record(path, running)

    changed = replace(running, **cast('Any', {field: changed_value}))
    with pytest.raises(InvocationEvidenceError, match='immutable once set'):
        write_record(path, changed)
