"""Tests for XDG-aware global settings."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_orchestra.cli import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_config_show_reports_file_values_and_cli_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolve XDG settings and show the source of every effective value."""

    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[storage]\n'
        'database = "configured.db"\n'
        'runs_directory = "configured-runs"\n\n'
        '[retention]\njob_evidence_days = 45\n'
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))

    assert (
        main(
            [
                '--database',
                str(tmp_path / 'override.db'),
                'config',
                'show',
                '--runs-directory',
                str(tmp_path / 'override-runs'),
            ]
        )
        == 0
    )

    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == 13
    assert document['settings']['storage.database']['source'] == 'command_line'
    assert document['settings']['storage.runs_directory']['source'] == 'command_line'
    assert document['settings']['retention.job_evidence_days'] == {
        'value': 45,
        'source': 'file',
    }
    assert not (tmp_path / 'override.db').exists()


def test_invalid_settings_fail_closed_without_creating_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject invalid TOML before parsing or initializing a command."""

    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('[retention]\njob_evidence_days = 0\n')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))

    assert main(['config', 'show']) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_settings'
    assert config.read_text() == '[retention]\njob_evidence_days = 0\n'
