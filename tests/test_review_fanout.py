"""Tests for disjoint per-reviewer fan-out dispatch."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from agent_orchestra.adapter.registry import DEFAULT_RUNTIME_REGISTRY
from agent_orchestra.invocations import InvocationIdentity
from agent_orchestra.review_fanout import (
    ReviewFanoutError,
    build_review_fanout,
)
from agent_orchestra.reviewer_paths import ReviewerEvidencePaths
from agent_orchestra.reviewer_plan import (
    ReviewerExecution,
    ReviewerExecutionPlan,
    build_reviewer_execution_plan,
    select_reviewer_set,
)
from agent_orchestra.settings import load_settings


def _settings(tmp_path: Path) -> Path:
    """Write one mixed-runtime reviewer set and return its settings path."""

    path = tmp_path / 'config.toml'
    path.write_text(
        '[reviewer_sets.default]\n'
        'members = [\n'
        '  { id = "security", runtime = "codex", model = "gpt-5.6" },\n'
        '  { id = "portability", runtime = "claude-code" },\n'
        ']\n',
        encoding='utf-8',
    )
    return path


def _plan(tmp_path: Path) -> ReviewerExecutionPlan:
    """Resolve the configured mixed-runtime plan used by these tests."""

    return build_reviewer_execution_plan(
        select_reviewer_set(load_settings(_settings(tmp_path)), 'default'),
        registry=DEFAULT_RUNTIME_REGISTRY,
        executable=Path('/python'),
        timeout_seconds=90,
    )


def _reviewer(reviewer_id: str) -> ReviewerExecution:
    """Build one minimal reviewer execution with the supplied identifier."""

    return ReviewerExecution(
        reviewer_id=reviewer_id,
        command=('/python', '-m', 'reviewer'),
        identity=InvocationIdentity(vendor='vendor', model=None, runtime='runtime'),
        timeout_seconds=90,
    )


def test_fanout_preserves_plan_order_identity_and_commands(tmp_path: Path) -> None:
    """Carry every immutable plan field onto its reviewer dispatch."""

    dispatches = build_review_fanout(
        _plan(tmp_path), run_id='job-1', sequence=1, iteration=1, attempt=1
    )

    assert [dispatch.reviewer_id for dispatch in dispatches] == [
        'security',
        'portability',
    ]
    assert dispatches[0].identity.model == 'gpt-5.6'
    assert dispatches[1].identity.model is None
    assert dispatches[0].command[-1] == 'gpt-5.6'
    assert all(dispatch.timeout_seconds == 90 for dispatch in dispatches)


def test_fanout_builds_reviewer_qualified_durable_identities(tmp_path: Path) -> None:
    """Give every concurrent reviewer a distinct task and invocation ID."""

    dispatches = build_review_fanout(
        _plan(tmp_path), run_id='job-1', sequence=3, iteration=2, attempt=4
    )

    assert dispatches[0].task_id == 'job-1:000003-reviewer-security'
    assert dispatches[0].invocation_id == (
        'job-1:000003-reviewer-security:attempt-0004'
    )
    assert dispatches[1].task_id == 'job-1:000003-reviewer-portability'
    assert len({dispatch.task_id for dispatch in dispatches}) == 2
    assert len({dispatch.invocation_id for dispatch in dispatches}) == 2


def test_fanout_owns_disjoint_paths_in_every_evidence_family(
    tmp_path: Path,
) -> None:
    """Share no mutable path between two reviewers of one iteration."""

    dispatches = build_review_fanout(
        _plan(tmp_path), run_id='job-1', sequence=1, iteration=1, attempt=1
    )

    families = [field.name for field in fields(ReviewerEvidencePaths)]
    for family in families:
        first = getattr(dispatches[0].paths, family)
        second = getattr(dispatches[1].paths, family)
        assert first != second, family
        assert 'security' in first
        assert 'portability' in second
    assert dispatches[0].paths.temporary_result != '.review-result.json'


def test_fanout_is_deterministic_for_resume(tmp_path: Path) -> None:
    """Rebuild identical dispatch identities from the same durable inputs."""

    plan = _plan(tmp_path)
    first = build_review_fanout(
        plan, run_id='job-1', sequence=3, iteration=2, attempt=4
    )
    resumed = build_review_fanout(
        plan, run_id='job-1', sequence=3, iteration=2, attempt=4
    )

    assert resumed == first


def test_fanout_rejects_duplicate_reviewer_ids() -> None:
    """Fail closed before two reviewers can collide on one namespace."""

    plan = ReviewerExecutionPlan('default', (_reviewer('same'), _reviewer('same')))

    with pytest.raises(ReviewFanoutError, match='duplicate reviewer IDs'):
        build_review_fanout(plan, run_id='job-1', sequence=1, iteration=1, attempt=1)


def test_fanout_requires_at_least_two_reviewers() -> None:
    """Reject a batch that cannot aggregate more than one verdict."""

    plan = ReviewerExecutionPlan('default', (_reviewer('security'),))

    with pytest.raises(ReviewFanoutError, match='at least two reviewers'):
        build_review_fanout(plan, run_id='job-1', sequence=1, iteration=1, attempt=1)


@pytest.mark.parametrize(
    ('sequence', 'iteration', 'attempt', 'message'),
    [
        (0, 1, 1, 'must be positive'),
        (1, 0, 1, 'must be positive'),
        (1, 1, 0, 'must be positive'),
    ],
)
def test_fanout_normalizes_identity_failures(
    tmp_path: Path, sequence: int, iteration: int, attempt: int, message: str
) -> None:
    """Keep path-layer rejections inside the fan-out error boundary."""

    with pytest.raises(ReviewFanoutError, match=message):
        build_review_fanout(
            _plan(tmp_path),
            run_id='job-1',
            sequence=sequence,
            iteration=iteration,
            attempt=attempt,
        )


def test_fanout_rejects_an_unsafe_reviewer_identifier() -> None:
    """Refuse an identifier that could escape the run directory."""

    plan = ReviewerExecutionPlan(
        'default', (_reviewer('security'), _reviewer('../escape'))
    )

    with pytest.raises(ReviewFanoutError, match='invalid reviewer ID'):
        build_review_fanout(plan, run_id='job-1', sequence=1, iteration=1, attempt=1)
