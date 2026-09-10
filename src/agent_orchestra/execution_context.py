"""
The collaborators and plans one worker invocation is configured with.

`WorkerContext` holds what the caller supplies identically on every path and
never changes as a workflow advances. The plan types hold what the run path
takes from the caller and the resume path rebuilds from durable evidence. They
are kept apart deliberately: merging them would silently use caller values when
resuming.

None of these holds a `Run`. `workflow.transition` returns a replacement with a
new state and iteration, so an object holding one would go stale on every
transition; only the run's identity is durable, and it is passed alongside.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_orchestra.adapter.registry import (
    DEFAULT_RUNTIME_REGISTRY,
    RuntimeRegistryError,
    RuntimeRole,
)
from agent_orchestra.evidence import WorkerError
from agent_orchestra.invocations import InvocationIdentity
from agent_orchestra.reviewer_plan import (
    ReviewerExecutionPlan,
    reviewer_execution_plan_record,
)
from agent_orchestra.schemas import (
    ExecutionRecord,
    ExecutionRecordSchema,
    ReviewerSetExecutionRecordSchema,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from agent_orchestra.adapter.registry import RuntimeRegistry
    from agent_orchestra.store import JobStore


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """
    Caller-supplied collaborators invariant across one worker invocation.

    These four values are identical on the run and the resume path and never
    change as a workflow advances, so they travel as one collaborator rather
    than as four parameters through every function in the chain. Adding a
    cross-cutting collaborator becomes a field here instead of a signature edit
    in nine places, which is what #38 had to do for the runtime registry.
    """

    store: JobStore
    runs_directory: Path
    digest_worktree: Callable[[Path, str], str | None]
    registry: RuntimeRegistry = DEFAULT_RUNTIME_REGISTRY


@dataclass(frozen=True, slots=True)
class ReviewPlan:
    """
    One workflow's objective, commands, limits, and agent identities.

    The run path takes these from the caller while the resume path rebuilds
    them from the durable execution record, so they are workflow state rather
    than caller configuration and are kept apart from WorkerContext.
    """

    objective: str
    reviewer_command: Sequence[str]
    developer_command: Sequence[str]
    timeout_seconds: int
    developer_timeout_seconds: int | None
    max_iterations: int
    reviewer_identity: InvocationIdentity
    developer_identity: InvocationIdentity

    @classmethod
    def from_execution_record(
        cls,
        execution: ExecutionRecordSchema,
        *,
        reviewer_identity: InvocationIdentity,
        developer_identity: InvocationIdentity,
    ) -> ReviewPlan:
        """
        Rebuild the plan a stopped run was executing from its own evidence.

        The identities are supplied rather than read here because the resume
        path resolves them against the registry only for runs that are not
        approved, and that condition belongs with the caller that knows the
        run state.
        """

        return cls(
            objective=execution.objective,
            reviewer_command=execution.reviewer.command,
            developer_command=execution.developer.command,
            timeout_seconds=execution.reviewer.timeout_seconds,
            developer_timeout_seconds=execution.developer.timeout_seconds,
            max_iterations=execution.max_review_iterations,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
        )


@dataclass(frozen=True, slots=True)
class ReviewerSetReviewPlan:
    """One workflow's immutable reviewer batch and developer configuration."""

    objective: str
    reviewer_plan: ReviewerExecutionPlan
    developer_command: Sequence[str]
    developer_timeout_seconds: int
    max_iterations: int
    developer_identity: InvocationIdentity


def _execution_record(
    plan: ReviewPlan | ReviewerSetReviewPlan,
    *,
    run_id: str,
    created_at: str,
) -> ExecutionRecord:
    """Build one strict versioned execution record from a worker plan."""

    common: dict[str, Any] = {
        'run_id': run_id,
        'objective': plan.objective,
        'developer': {
            'command': list(plan.developer_command),
            'identity': {
                'vendor': plan.developer_identity.vendor,
                'model': plan.developer_identity.model,
                'runtime': plan.developer_identity.runtime,
            },
            'timeout_seconds': plan.developer_timeout_seconds,
        },
        'max_review_iterations': plan.max_iterations,
        'created_at': created_at,
    }
    if isinstance(plan, ReviewerSetReviewPlan):
        return ReviewerSetExecutionRecordSchema.model_validate(
            {
                **common,
                'schema_version': 3,
                'reviewer_plan': reviewer_execution_plan_record(plan.reviewer_plan),
            }
        )
    return ExecutionRecordSchema.model_validate(
        {
            **common,
            'schema_version': 2,
            'reviewer': {
                'command': list(plan.reviewer_command),
                'identity': {
                    'vendor': plan.reviewer_identity.vendor,
                    'model': plan.reviewer_identity.model,
                    'runtime': plan.reviewer_identity.runtime,
                },
                'timeout_seconds': plan.timeout_seconds,
            },
        }
    )


def _identity_from_record(
    vendor: str, model: str | None, runtime: str
) -> InvocationIdentity:
    """Convert persisted execution identity fields into the runtime value."""

    return InvocationIdentity(vendor=vendor, model=model, runtime=runtime)


def _resolve_resume_identity(
    identity: InvocationIdentity,
    role: RuntimeRole,
    registry: RuntimeRegistry,
) -> InvocationIdentity:
    """Validate a persisted runtime role and derive its current vendor."""

    if identity.runtime == 'custom-command':
        return identity
    try:
        runtime = registry.require(identity.runtime, role)
    except RuntimeRegistryError as error:
        raise WorkerError(str(error), code=error.code) from error
    return InvocationIdentity(
        vendor=runtime.vendor,
        model=identity.model,
        runtime=runtime.identifier,
    )
