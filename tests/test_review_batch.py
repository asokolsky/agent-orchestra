"""Tests for deterministic source-review batch aggregation."""

from __future__ import annotations

from itertools import product
from typing import cast, get_args

import pytest

from agent_orchestra.review_batch import (
    VALID_OUTCOMES,
    ReviewBatchError,
    ReviewerDecision,
    ReviewerOutcome,
    aggregate_review_batch,
)
from agent_orchestra.schemas import ReviewerBatchResultSchema


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


def test_blocked_and_incomplete_precede_changes_requested() -> None:
    """Fail closed when an actionable batch also lacks a required review."""

    decision = aggregate_review_batch(
        (
            ReviewerDecision('first', 'blocked'),
            ReviewerDecision('second', 'changes_requested'),
            ReviewerDecision('third', 'incomplete'),
        )
    )

    assert decision.verdict == 'blocked'
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


def test_runtime_outcomes_follow_the_typed_vocabulary() -> None:
    """Keep runtime validation synchronized with the reviewer outcome type."""

    assert frozenset(get_args(ReviewerOutcome)) == VALID_OUTCOMES


def test_every_aggregate_decision_validates_as_canonical_evidence() -> None:
    """Pin schema verdict derivation to every combination of runtime outcomes."""

    reviewer_ids = ('first', 'second', 'third')
    for outcomes in product(sorted(VALID_OUTCOMES), repeat=len(reviewer_ids)):
        decisions = tuple(
            ReviewerDecision(reviewer_id, cast('ReviewerOutcome', outcome))
            for reviewer_id, outcome in zip(reviewer_ids, outcomes, strict=True)
        )
        aggregate = aggregate_review_batch(decisions)
        ReviewerBatchResultSchema.model_validate(
            {
                'schema_version': 1,
                'run_id': 'run-1',
                'iteration': 1,
                'reviewer_set_id': 'default',
                'aggregation_policy': 'all_required',
                'diff_digest': 'sha256:' + 'a' * 64,
                'verdict': aggregate.verdict,
                'reviewers': [
                    {
                        'reviewer_id': decision.reviewer_id,
                        'outcome': decision.outcome,
                        'result_path': (
                            None
                            if decision.outcome == 'incomplete'
                            else (
                                'messages/000002-'
                                f'{decision.reviewer_id}-review-result.json'
                            )
                        ),
                    }
                    for decision in decisions
                ],
                'changes_requested_by': list(aggregate.changes_requested_by),
                'blocked_by': list(aggregate.blocked_by),
                'incomplete_reviewers': list(aggregate.incomplete_reviewers),
            }
        )
