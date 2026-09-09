"""Resolve one reviewer execution plan into disjoint per-reviewer dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_orchestra.reviewer_paths import (
    ReviewerEvidencePaths,
    ReviewerIdentityError,
    reviewer_evidence_paths,
    reviewer_invocation_id,
    reviewer_task_id,
)

if TYPE_CHECKING:
    from agent_orchestra.invocations import InvocationIdentity
    from agent_orchestra.reviewer_plan import ReviewerExecutionPlan

DUPLICATE_REVIEWER_IDS = 'review fan-out contains duplicate reviewer IDs'
EMPTY_FANOUT = 'review fan-out requires at least two reviewers'


class ReviewFanoutError(ValueError):
    """Raised when a reviewer batch cannot own disjoint durable evidence."""


@dataclass(frozen=True, slots=True)
class ReviewerDispatch:
    """One reviewer's durable identity, command, and owned evidence paths."""

    reviewer_id: str
    command: tuple[str, ...]
    identity: InvocationIdentity
    timeout_seconds: int
    task_id: str
    invocation_id: str
    paths: ReviewerEvidencePaths


def build_review_fanout(
    plan: ReviewerExecutionPlan,
    *,
    run_id: str,
    sequence: int,
    iteration: int,
    attempt: int,
) -> tuple[ReviewerDispatch, ...]:
    """Resolve every required reviewer into its own dispatch and evidence."""

    if len(plan.reviewers) < 2:
        raise ReviewFanoutError(EMPTY_FANOUT)
    reviewer_ids = [reviewer.reviewer_id for reviewer in plan.reviewers]
    if len(reviewer_ids) != len(set(reviewer_ids)):
        raise ReviewFanoutError(DUPLICATE_REVIEWER_IDS)
    try:
        dispatches = tuple(
            ReviewerDispatch(
                reviewer_id=reviewer.reviewer_id,
                command=reviewer.command,
                identity=reviewer.identity,
                timeout_seconds=reviewer.timeout_seconds,
                task_id=reviewer_task_id(run_id, sequence, reviewer.reviewer_id),
                invocation_id=reviewer_invocation_id(
                    run_id, sequence, reviewer.reviewer_id, attempt
                ),
                paths=reviewer_evidence_paths(
                    sequence=sequence,
                    iteration=iteration,
                    reviewer_id=reviewer.reviewer_id,
                    attempt=attempt,
                ),
            )
            for reviewer in plan.reviewers
        )
    except ReviewerIdentityError as error:
        raise ReviewFanoutError(str(error)) from error
    return dispatches
