"""Resolve configured reviewer sets into immutable execution plans."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_orchestra.adapter.registry import RuntimeRegistry, RuntimeRole
from agent_orchestra.invocations import InvocationIdentity

if TYPE_CHECKING:
    from pathlib import Path

    from agent_orchestra.settings import ReviewerSet, Settings


class ReviewerPlanError(ValueError):
    """Raised when a reviewer execution plan cannot be resolved."""


@dataclass(frozen=True, slots=True)
class ReviewerExecution:
    """Immutable executable configuration for one required reviewer."""

    reviewer_id: str
    command: tuple[str, ...]
    identity: InvocationIdentity
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ReviewerExecutionPlan:
    """Ordered required reviewers selected for one review batch."""

    reviewer_set_id: str
    reviewers: tuple[ReviewerExecution, ...]


def select_reviewer_set(settings: Settings, reviewer_set_id: str) -> ReviewerSet:
    """Return one configured reviewer set by its stable identifier."""

    for reviewer_set in settings.reviewer_sets:
        if reviewer_set.identifier == reviewer_set_id:
            return reviewer_set
    message = f'unknown reviewer set: {reviewer_set_id}'
    raise ReviewerPlanError(message)


def build_reviewer_execution_plan(
    reviewer_set: ReviewerSet,
    *,
    registry: RuntimeRegistry,
    executable: Path,
    timeout_seconds: int,
) -> ReviewerExecutionPlan:
    """Resolve one ordered reviewer set through the runtime registry."""

    if timeout_seconds <= 0:
        message = 'reviewer timeout must be positive'
        raise ReviewerPlanError(message)
    reviewers: list[ReviewerExecution] = []
    for member in reviewer_set.members:
        runtime = registry.require(member.runtime, RuntimeRole.REVIEWER)
        command = [str(executable), '-m', runtime.module]
        if member.model is not None:
            command.extend(['--model', member.model])
        reviewers.append(
            ReviewerExecution(
                reviewer_id=member.identifier,
                command=tuple(command),
                identity=InvocationIdentity(
                    vendor=runtime.vendor,
                    model=member.model,
                    runtime=runtime.identifier,
                ),
                timeout_seconds=timeout_seconds,
            )
        )
    if len(reviewers) < 2:
        message = 'reviewer execution plan requires at least two reviewers'
        raise ReviewerPlanError(message)
    return ReviewerExecutionPlan(reviewer_set.identifier, tuple(reviewers))
