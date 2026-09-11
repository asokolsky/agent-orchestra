"""Tests for machine-readable effective-model provenance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_orchestra.adapter.claude_code import _effective_models
from agent_orchestra.invocations import EffectiveModelStatus
from agent_orchestra.runtime_metadata import (
    RUNTIME_METADATA_ENV,
    RuntimeMetadata,
    RuntimeMetadataError,
    read_runtime_metadata,
    reviewer_process_environment,
    write_runtime_metadata,
)


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
