"""Tests for machine-readable effective-model provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_orchestra.adapter.claude_code import _effective_models, _runtime_usage
from agent_orchestra.invocations import EffectiveModelStatus
from agent_orchestra.runtime_metadata import (
    RUNTIME_METADATA_ENV,
    RuntimeMetadata,
    RuntimeMetadataError,
    read_runtime_metadata,
    reviewer_process_environment,
    write_runtime_metadata,
)
from agent_orchestra.usage import ModelUsage, RuntimeUsage, UsageValues


def test_claude_model_usage_preserves_multiple_effective_models() -> None:
    """Preserve every model identity in Claude Code's reported order."""

    output = {
        'modelUsage': {
            'claude-sonnet-4-6': {'inputTokens': 10},
            'claude-haiku-4-5-20251001': {'inputTokens': 2},
        }
    }

    assert _effective_models(output) == (
        'claude-sonnet-4-6',
        'claude-haiku-4-5-20251001',
    )
    assert _effective_models({}) == ()
    assert _effective_models({'modelUsage': []}) == ()


def test_claude_usage_keeps_aggregate_and_model_scopes_separate() -> None:
    """Preserve partial multi-model provenance without inventing one grand total."""

    output = {
        'num_turns': 3,
        'total_cost_usd': 0.25,
        'usage': {
            'input_tokens': 100,
            'output_tokens': 20,
            'cache_read_input_tokens': 40,
        },
        'modelUsage': {
            'claude-sonnet': {'inputTokens': 80, 'costUSD': 0.2},
            'claude-haiku': {'outputTokens': 5},
        },
    }

    assert _runtime_usage(output) == RuntimeUsage(
        turn_count=3,
        totals=UsageValues(
            input_tokens=100,
            output_tokens=20,
            cache_read_input_tokens=40,
            total_cost_usd=0.25,
        ),
        models=(
            ModelUsage(
                model='claude-sonnet',
                values=UsageValues(input_tokens=80, total_cost_usd=0.2),
            ),
            ModelUsage(
                model='claude-haiku',
                values=UsageValues(output_tokens=5),
            ),
        ),
    )


@pytest.mark.parametrize(
    'output',
    [
        {'num_turns': True},
        {'total_cost_usd': -0.1},
        {'usage': {'input_tokens': False, 'output_tokens': -1}},
        {'modelUsage': {'claude-sonnet': {'inputTokens': '10'}}},
    ],
)
def test_claude_usage_rejects_unusable_vendor_values(
    output: dict[str, object],
) -> None:
    """Treat malformed, boolean, and negative values as unavailable."""

    assert _runtime_usage(output) is None


def test_claude_usage_retains_valid_fields_from_a_partial_envelope() -> None:
    """Keep valid fields without allowing malformed peers to poison or pad them."""

    assert _runtime_usage(
        {
            'usage': {
                'input_tokens': 12,
                'output_tokens': True,
                'cache_creation_input_tokens': -3,
            }
        }
    ) == RuntimeUsage(totals=UsageValues(input_tokens=12))


def test_reviewer_environment_confines_transient_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route common validation caches to the isolated writable directory."""

    monkeypatch.setenv(RUNTIME_METADATA_ENV, 'outer-runtime.json')

    environment = reviewer_process_environment(tmp_path, TMPDIR='override')

    assert environment['TMPDIR'] == 'override'
    assert environment['PYTHONDONTWRITEBYTECODE'] == '1'
    assert environment['PYTEST_ADDOPTS'] == '-p no:cacheprovider'
    for name in (
        'MISE_CACHE_DIR',
        'MISE_STATE_DIR',
        'MYPY_CACHE_DIR',
        'RUFF_CACHE_DIR',
        'UV_CACHE_DIR',
        'XDG_CACHE_HOME',
    ):
        assert Path(environment[name]).is_relative_to(tmp_path)
    assert RUNTIME_METADATA_ENV not in environment


@pytest.mark.parametrize(
    ('models', 'expected_status'),
    [
        (('model-a', 'model-b'), EffectiveModelStatus.REPORTED),
        ((), EffectiveModelStatus.UNAVAILABLE),
    ],
)
@pytest.mark.parametrize('timed_out', [False, True])
def test_runtime_metadata_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    models: tuple[str, ...],
    expected_status: EffectiveModelStatus,
    timed_out: bool,
) -> None:
    """Exchange identities and bounding outcome without retaining a sidecar."""

    path = tmp_path / 'runtime.json'
    monkeypatch.setenv(RUNTIME_METADATA_ENV, str(path))

    write_runtime_metadata(models, timed_out=timed_out)

    assert read_runtime_metadata(path) == RuntimeMetadata(
        effective_models=models,
        effective_model_status=expected_status,
        timed_out=timed_out,
    )
    assert not path.exists()


def test_runtime_metadata_round_trips_adapter_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carry one stable adapter failure through the private sidecar."""

    path = tmp_path / 'runtime.json'
    monkeypatch.setenv(RUNTIME_METADATA_ENV, str(path))

    write_runtime_metadata(
        ('claude-sonnet',),
        failure_code='structured_output_exhausted',
        failure_message='structured output failed after five attempts',
    )

    assert json.loads(path.read_text())['schema_version'] == 4
    assert read_runtime_metadata(path) == RuntimeMetadata(
        effective_models=('claude-sonnet',),
        effective_model_status=EffectiveModelStatus.REPORTED,
        failure_code='structured_output_exhausted',
        failure_message='structured output failed after five attempts',
    )


def test_runtime_metadata_reads_schema_two_without_failure_details(
    tmp_path: Path,
) -> None:
    """Keep the prior ephemeral metadata schema readable during upgrades."""

    path = tmp_path / 'runtime.json'
    path.write_text(
        json.dumps(
            {
                'schema_version': 2,
                'effective_models': [],
                'status': 'unavailable',
                'timed_out': False,
            }
        )
    )

    assert read_runtime_metadata(path) == RuntimeMetadata()


def test_runtime_metadata_rejects_inconsistent_status(
    tmp_path: Path,
) -> None:
    """Reject metadata that claims reporting without a model identity."""

    path = tmp_path / 'runtime.json'
    path.write_text(
        json.dumps(
            {
                'schema_version': 2,
                'effective_models': [],
                'status': 'reported',
                'timed_out': False,
            }
        )
    )

    with pytest.raises(RuntimeMetadataError, match='values'):
        read_runtime_metadata(path)

    assert not path.exists()
