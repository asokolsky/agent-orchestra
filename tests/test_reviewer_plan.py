"""Tests for immutable reviewer-set execution plans."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_orchestra.adapter.registry import DEFAULT_RUNTIME_REGISTRY
from agent_orchestra.reviewer_plan import (
    ReviewerPlanError,
    build_reviewer_execution_plan,
    reviewer_execution_plan_record,
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


def test_select_and_build_ordered_mixed_runtime_plan(tmp_path: Path) -> None:
    """Freeze configured order, commands, identities, and timeouts."""

    settings = load_settings(_settings(tmp_path))
    reviewer_set = select_reviewer_set(settings, 'default')

    plan = build_reviewer_execution_plan(
        reviewer_set,
        registry=DEFAULT_RUNTIME_REGISTRY,
        executable=Path('/python'),
        timeout_seconds=90,
    )

    assert plan.reviewer_set_id == 'default'
    assert [reviewer.reviewer_id for reviewer in plan.reviewers] == [
        'security',
        'portability',
    ]
    assert plan.reviewers[0].command == (
        '/python',
        '-m',
        'agent_orchestra.adapter.codex',
        '--model',
        'gpt-5.6',
    )
    assert plan.reviewers[0].identity.vendor == 'openai'
    assert plan.reviewers[1].command == (
        '/python',
        '-m',
        'agent_orchestra.adapter.claude_code',
    )
    assert plan.reviewers[1].identity.vendor == 'anthropic'
    assert {reviewer.timeout_seconds for reviewer in plan.reviewers} == {90}


def test_select_reviewer_set_rejects_unknown_name(tmp_path: Path) -> None:
    """Fail selection before any execution plan can be persisted."""

    settings = load_settings(_settings(tmp_path))

    with pytest.raises(ReviewerPlanError, match='unknown reviewer set: missing'):
        select_reviewer_set(settings, 'missing')


def test_build_reviewer_execution_plan_rejects_invalid_timeout(
    tmp_path: Path,
) -> None:
    """Reject an invalid shared reviewer timeout."""

    settings = load_settings(_settings(tmp_path))
    reviewer_set = select_reviewer_set(settings, 'default')

    with pytest.raises(ReviewerPlanError, match='timeout must be positive'):
        build_reviewer_execution_plan(
            reviewer_set,
            registry=DEFAULT_RUNTIME_REGISTRY,
            executable=Path('/python'),
            timeout_seconds=0,
        )


def test_reviewer_execution_plan_record_preserves_order_and_provenance(
    tmp_path: Path,
) -> None:
    """Serialize every immutable plan field into strict canonical metadata."""

    settings = load_settings(_settings(tmp_path))
    plan = build_reviewer_execution_plan(
        select_reviewer_set(settings, 'default'),
        registry=DEFAULT_RUNTIME_REGISTRY,
        executable=Path('/python'),
        timeout_seconds=90,
    )

    record = reviewer_execution_plan_record(plan)

    assert record.schema_version == 1
    assert record.reviewer_set_id == 'default'
    assert record.aggregation_policy == 'all_required'
    assert [reviewer.reviewer_id for reviewer in record.reviewers] == [
        'security',
        'portability',
    ]
    assert record.reviewers[0].identity.model == 'gpt-5.6'
    assert record.reviewers[1].identity.model is None
