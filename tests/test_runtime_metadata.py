"""Tests for machine-readable effective-model provenance."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_orchestra.adapter.claude_code import _effective_models
from agent_orchestra.runtime_metadata import (
    RUNTIME_METADATA_ENV,
    RuntimeMetadataError,
    read_runtime_metadata,
    write_runtime_metadata,
)

if TYPE_CHECKING:
    from pathlib import Path


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


@pytest.mark.parametrize(
    ('models', 'expected_status'),
    [(('model-a', 'model-b'), 'reported'), ((), 'unavailable')],
)
def test_runtime_metadata_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    models: tuple[str, ...],
    expected_status: str,
) -> None:
    """Exchange effective identities without retaining a transient sidecar."""

    path = tmp_path / 'runtime.json'
    monkeypatch.setenv(RUNTIME_METADATA_ENV, str(path))

    write_runtime_metadata(models)

    assert read_runtime_metadata(path) == (models, expected_status)
    assert not path.exists()


def test_runtime_metadata_rejects_inconsistent_status(
    tmp_path: Path,
) -> None:
    """Reject metadata that claims reporting without a model identity."""

    path = tmp_path / 'runtime.json'
    path.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'effective_models': [],
                'status': 'reported',
            }
        )
    )

    with pytest.raises(RuntimeMetadataError, match='values'):
        read_runtime_metadata(path)

    assert not path.exists()
