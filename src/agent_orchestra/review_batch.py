"""Deterministic source-review batch aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from agent_orchestra.reviewer_paths import ReviewerIdentityError, validate_reviewer_id

ReviewerOutcome = Literal['approved', 'changes_requested', 'blocked', 'incomplete']
AggregateVerdict = Literal['approved', 'changes_requested', 'blocked']
EMPTY_BATCH = 'review batch must contain at least one reviewer'
DUPLICATE_REVIEWER_IDS = 'review batch contains duplicate reviewer IDs'
INVALID_OUTCOME = 'review batch contains an invalid reviewer outcome'
VALID_OUTCOMES = frozenset(get_args(ReviewerOutcome))


class ReviewBatchError(ValueError):
    """Raised when reviewer outcomes cannot form one valid batch decision."""


@dataclass(frozen=True, slots=True)
class ReviewerDecision:
    """One required reviewer's terminal or incomplete batch outcome."""

    reviewer_id: str
    outcome: ReviewerOutcome


@dataclass(frozen=True, slots=True)
class ReviewBatchDecision:
    """One deterministic aggregate verdict with machine-readable rationale."""

    verdict: AggregateVerdict
    changes_requested_by: tuple[str, ...]
    blocked_by: tuple[str, ...]
    incomplete_reviewers: tuple[str, ...]


def aggregate_review_batch(
    decisions: tuple[ReviewerDecision, ...],
) -> ReviewBatchDecision:
    """Aggregate every required reviewer using fail-closed precedence."""

    if not decisions:
        raise ReviewBatchError(EMPTY_BATCH)
    reviewer_ids = [decision.reviewer_id for decision in decisions]
    try:
        for reviewer_id in reviewer_ids:
            validate_reviewer_id(reviewer_id)
    except ReviewerIdentityError as error:
        raise ReviewBatchError(str(error)) from error
    if len(reviewer_ids) != len(set(reviewer_ids)):
        raise ReviewBatchError(DUPLICATE_REVIEWER_IDS)
    if any(decision.outcome not in VALID_OUTCOMES for decision in decisions):
        raise ReviewBatchError(INVALID_OUTCOME)

    changes_requested = tuple(
        decision.reviewer_id
        for decision in decisions
        if decision.outcome == 'changes_requested'
    )
    blocked = tuple(
        decision.reviewer_id for decision in decisions if decision.outcome == 'blocked'
    )
    incomplete = tuple(
        decision.reviewer_id
        for decision in decisions
        if decision.outcome == 'incomplete'
    )
    if blocked or incomplete:
        verdict: AggregateVerdict = 'blocked'
    elif changes_requested:
        verdict = 'changes_requested'
    else:
        verdict = 'approved'
    return ReviewBatchDecision(verdict, changes_requested, blocked, incomplete)
