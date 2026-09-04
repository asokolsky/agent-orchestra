"""Tests for normalized external agent invocation behavior."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_orchestra.agents import CommandAgentAdapter, DeveloperRequest
from agent_orchestra.runtime_metadata import RUNTIME_METADATA_ENV


@pytest.mark.parametrize('failure', ['timeout', 'interrupt'])
def test_command_adapter_preserves_metadata_across_exceptional_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Consume reported provenance before propagating a failed invocation."""

    metadata_path = tmp_path / 'runtime.json'

    def fail(_command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Write adapter metadata and simulate the selected interruption."""

        path = Path(kwargs['env'][RUNTIME_METADATA_ENV])
        path.write_text(
            json.dumps(
                {
                    'schema_version': 1,
                    'effective_models': ['reported-model'],
                    'status': 'reported',
                }
            )
        )
        if failure == 'timeout':
            command = 'command'
            raise subprocess.TimeoutExpired(command, 1)
        raise KeyboardInterrupt

    monkeypatch.setattr('agent_orchestra.agents.subprocess.run', fail)
    request = DeveloperRequest(
        objective='Test metadata recovery.',
        worktree_path=tmp_path,
        iteration=1,
        allowed_actions=(),
        timeout_seconds=1,
        request_path=tmp_path / 'request.json',
        response_path=tmp_path / 'response.json',
        runtime_metadata_path=metadata_path,
    )

    expected = subprocess.TimeoutExpired if failure == 'timeout' else KeyboardInterrupt
    with pytest.raises(expected) as raised:
        CommandAgentAdapter(('agent',)).execute(request)

    assert raised.value.__dict__['effective_models'] == ('reported-model',)
    assert raised.value.__dict__['effective_model_status'] == 'reported'
    assert not metadata_path.exists()


def test_command_adapter_ignores_non_utf8_runtime_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete the invocation when optional runtime metadata cannot be decoded."""

    metadata_path = tmp_path / 'runtime.json'

    def succeed(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Write malformed metadata and return a successful process result."""

        Path(kwargs['env'][RUNTIME_METADATA_ENV]).write_bytes(b'\xff')
        return subprocess.CompletedProcess(command, 0, stdout='done', stderr='')

    monkeypatch.setattr('agent_orchestra.agents.subprocess.run', succeed)
    request = DeveloperRequest(
        objective='Test malformed metadata recovery.',
        worktree_path=tmp_path,
        iteration=1,
        allowed_actions=(),
        timeout_seconds=1,
        request_path=tmp_path / 'request.json',
        response_path=tmp_path / 'response.json',
        runtime_metadata_path=metadata_path,
    )

    result = CommandAgentAdapter(('agent',)).execute(request)

    assert result.succeeded is True
    assert result.stdout == 'done'
    assert result.effective_models == ()
    assert result.effective_model_status == 'unavailable'
    assert not metadata_path.exists()
