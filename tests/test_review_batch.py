"""Tests for deterministic source-review batch aggregation."""

from __future__ import annotations

from typing import cast

import pytest

from agent_orchestra.review_batch import (
    ReviewBatchError,
    ReviewerDecision,
    ReviewerOutcome,
    aggregate_review_batch,
)


def test_all_required_approvals_aggregate_to_approval() -> None:
    """Approve only after every required reviewer approves."""

    decision = aggregate_review_batch(
        (
            ReviewerDecision('codex', 'approved'),
            ReviewerDecision('claude', 'approved'),
        )
    )

    assert decision.verdict == 'approved'
    assert decision.changes_requested_by == ()
    assert decision.blocked_by == ()
    assert decision.incomplete_reviewers == ()


def test_changes_requested_precedes_blocked_and_incomplete() -> None:
    """Preserve actionable findings as the highest-priority batch result."""

    decision = aggregate_review_batch(
        (
            ReviewerDecision('first', 'blocked'),
            ReviewerDecision('second', 'changes_requested'),
            ReviewerDecision('third', 'incomplete'),
        )
    )

    assert decision.verdict == 'changes_requested'
    assert decision.changes_requested_by == ('second',)
    assert decision.blocked_by == ('first',)
    assert decision.incomplete_reviewers == ('third',)


@pytest.mark.parametrize('outcome', ['blocked', 'incomplete'])
def test_non_actionable_failure_prevents_approval(outcome: ReviewerOutcome) -> None:
    """Fail closed when any required reviewer cannot approve the diff."""

    decision = aggregate_review_batch(
        (
            ReviewerDecision('first', 'approved'),
            ReviewerDecision('second', outcome),
        )
    )

    assert decision.verdict == 'blocked'


@pytest.mark.parametrize(
    ('decisions', 'message'),
    [
        ((), 'must contain at least one reviewer'),
        (
            (
                ReviewerDecision('same', 'approved'),
                ReviewerDecision('same', 'approved'),
            ),
            'duplicate reviewer IDs',
        ),
        ((ReviewerDecision('../escape', 'approved'),), 'invalid reviewer ID'),
    ],
)
def test_invalid_batches_fail_closed(
    decisions: tuple[ReviewerDecision, ...], message: str
) -> None:
    """Reject aggregate inputs whose membership is ambiguous."""

    with pytest.raises(ReviewBatchError, match=message):
        aggregate_review_batch(decisions)


def test_unknown_outcome_fails_closed() -> None:
    """Reject values introduced outside the batch outcome vocabulary."""

    decision = ReviewerDecision('first', cast('ReviewerOutcome', 'unknown'))

    with pytest.raises(ReviewBatchError, match='invalid reviewer outcome'):
        aggregate_review_batch((decision,))
