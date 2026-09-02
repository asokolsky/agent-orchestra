"""Tests for orchestration state transitions."""

from pathlib import Path

import pytest

from agent_orchestra.models import Run, RunState
from agent_orchestra.workflow import (
    ApprovalInvalidationError,
    InvalidTransitionError,
    invalidate_approval,
    transition,
)


def make_run() -> Run:
    """Create a run suitable for workflow tests."""

    return Run.create_local(
        Path('/repo'), Path('/worktree'), 'base', 'head', 'digest-1'
    )


def test_review_transition_increments_iteration() -> None:
    """Count each transition into review as a new review iteration."""

    run = transition(make_run(), RunState.PREPARING)
    run = transition(run, RunState.DEVELOPING)
    run = transition(run, RunState.AWAITING_REVIEW)

    assert run.state is RunState.AWAITING_REVIEW
    assert run.iteration == 1


def test_developer_can_address_requested_changes() -> None:
    """Allow the developer to receive and address review findings."""

    run = transition(make_run(), RunState.PREPARING)
    run = transition(run, RunState.AWAITING_REVIEW)
    run = transition(run, RunState.CHANGES_REQUESTED)
    run = transition(run, RunState.DEVELOPING)
    run = transition(run, RunState.AWAITING_REVIEW)

    assert run.iteration == 2


def test_approval_does_not_skip_authorization_gate() -> None:
    """Reject a direct transition from approval to committed state."""

    run = transition(make_run(), RunState.PREPARING)
    run = transition(run, RunState.AWAITING_REVIEW)
    run = transition(run, RunState.APPROVED)

    with pytest.raises(InvalidTransitionError):
        transition(run, RunState.COMMITTED)


@pytest.mark.parametrize(
    'state', [RunState.APPROVED, RunState.AWAITING_COMMIT_AUTHORIZATION]
)
def test_changed_diff_invalidates_approval(state: RunState) -> None:
    """Return an approved changed diff to a new review iteration."""

    run = transition(make_run(), RunState.PREPARING)
    run = transition(run, RunState.AWAITING_REVIEW)
    run = transition(run, RunState.APPROVED)
    if state is RunState.AWAITING_COMMIT_AUTHORIZATION:
        run = transition(run, state)

    invalidated = invalidate_approval(run, 'digest-2')

    assert invalidated.state is RunState.AWAITING_REVIEW
    assert invalidated.diff_digest == 'digest-2'
    assert invalidated.iteration == 2


def test_approval_requires_a_changed_digest_for_invalidation() -> None:
    """Reject invalidation when the reviewed diff has not changed."""

    run = transition(make_run(), RunState.PREPARING)
    run = transition(run, RunState.AWAITING_REVIEW)
    run = transition(run, RunState.APPROVED)

    with pytest.raises(ApprovalInvalidationError):
        invalidate_approval(run, 'digest-1')


def test_terminal_state_cannot_transition() -> None:
    """Reject transitions out of a terminal state."""

    run = transition(make_run(), RunState.CANCELLED)

    with pytest.raises(InvalidTransitionError):
        transition(run, RunState.PREPARING)
