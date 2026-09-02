"""Explicit state transitions for orchestration runs."""

from __future__ import annotations

from dataclasses import replace

from agent_orchestra.models import Run, RunState, same_diff_digest, utc_now


class InvalidTransitionError(ValueError):
    """Raised when a workflow transition violates the lifecycle contract."""


class ApprovalInvalidationError(ValueError):
    """Raised when approval cannot be invalidated for a changed diff."""


# Ordinary forward lifecycle transitions. Approval invalidation is deliberately
# guarded by invalidate_approval because it also requires a changed diff digest.
TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.QUEUED: frozenset({RunState.PREPARING, RunState.CANCELLED}),
    RunState.PREPARING: frozenset(
        {
            RunState.DEVELOPING,
            RunState.AWAITING_REVIEW,
            RunState.FAILED,
            RunState.INTERRUPTED,
        }
    ),
    RunState.DEVELOPING: frozenset(
        {RunState.AWAITING_REVIEW, RunState.FAILED, RunState.INTERRUPTED}
    ),
    RunState.AWAITING_REVIEW: frozenset(
        {
            RunState.CHANGES_REQUESTED,
            RunState.APPROVED,
            RunState.FAILED,
            RunState.INTERRUPTED,
            RunState.SUPERSEDED,
        }
    ),
    RunState.CHANGES_REQUESTED: frozenset(
        {RunState.DEVELOPING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.APPROVED: frozenset({RunState.AWAITING_COMMIT_AUTHORIZATION}),
    RunState.AWAITING_COMMIT_AUTHORIZATION: frozenset(
        {RunState.COMMITTED, RunState.CANCELLED}
    ),
    RunState.COMMITTED: frozenset({RunState.AWAITING_PUBLISH_AUTHORIZATION}),
    RunState.AWAITING_PUBLISH_AUTHORIZATION: frozenset(
        {RunState.PUBLISHED, RunState.CANCELLED}
    ),
    RunState.INTERRUPTED: frozenset(
        {
            RunState.PREPARING,
            RunState.DEVELOPING,
            RunState.AWAITING_REVIEW,
            RunState.CANCELLED,
        }
    ),
}


def transition(run: Run, target: RunState) -> Run:
    """Return a run advanced to an allowed target state."""

    allowed = TRANSITIONS.get(run.state, frozenset())
    if target not in allowed:
        raise InvalidTransitionError(f'cannot transition from {run.state} to {target}')

    iteration = (
        run.iteration + 1 if target is RunState.AWAITING_REVIEW else run.iteration
    )
    return replace(run, state=target, iteration=iteration, updated_at=utc_now())


def invalidate_approval(run: Run, diff_digest: str) -> Run:
    """Return an approved run to review after its diff changes."""

    invalidatable = {RunState.APPROVED, RunState.AWAITING_COMMIT_AUTHORIZATION}
    if run.state not in invalidatable:
        raise ApprovalInvalidationError(f'cannot invalidate approval from {run.state}')
    if not diff_digest or same_diff_digest(diff_digest, run.diff_digest):
        message = 'approval invalidation requires a new diff digest'
        raise ApprovalInvalidationError(message)
    return replace(
        run,
        state=RunState.AWAITING_REVIEW,
        diff_digest=diff_digest,
        iteration=run.iteration + 1,
        updated_at=utc_now(),
    )
