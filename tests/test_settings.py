"""Tests for XDG-aware global settings."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from agent_orchestra.adapter.registry import DEFAULT_RUNTIME_REGISTRY, RuntimeRegistry
from agent_orchestra.cli import build_parser, main

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
        '\n[reviewer_sets.security]\n'
        'members = [\n'
        '  { id = "primary", runtime = "codex", model = "gpt-5.6" },\n'
        '  { id = "second", runtime = "claude-code", required = true },\n'
        ']\n'
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
    assert document['schema_version'] == 17
    assert document['settings']['storage.database']['source'] == 'command_line'
    assert document['settings']['storage.runs_directory']['source'] == 'command_line'
    assert document['settings']['retention.job_evidence_days'] == {
        'value': 45,
        'source': 'file',
    }
    assert document['settings']['reviewer_sets'] == {
        'value': [
            {
                'id': 'security',
                'members': [
                    {
                        'id': 'primary',
                        'runtime': 'codex',
                        'vendor': 'openai',
                        'model': 'gpt-5.6',
                        'required': True,
                    },
                    {
                        'id': 'second',
                        'runtime': 'claude-code',
                        'vendor': 'anthropic',
                        'model': None,
                        'required': True,
                    },
                ],
            }
        ],
        'source': 'file',
        'status': 'review_only',
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


def test_reviewer_sets_reject_duplicate_member_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject ambiguous reviewer identities before command execution."""

    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[reviewer_sets.default]\nmembers = [\n'
        '  { id = "same", runtime = "codex" },\n'
        '  { id = "same", runtime = "claude-code" },\n]\n'
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))

    assert main(['config', 'show']) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_settings'
    assert 'duplicate reviewer ID in default: same' in document['error']['message']


def test_reviewer_sets_reject_unknown_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resolve reviewer runtimes through the canonical registry."""

    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[reviewer_sets.default]\nmembers = [\n'
        '  { id = "first", runtime = "codex" },\n'
        '  { id = "second", runtime = "missing" },\n]\n'
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))

    assert main(['config', 'show']) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_settings'
    assert document['error']['message'].endswith(
        'members[1].runtime: runtime_unknown: missing'
    )


def test_reviewer_sets_use_the_parser_runtime_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validate configured members against the parser's selected registry."""

    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[reviewer_sets.custom]\nmembers = [\n'
        '  { id = "first", runtime = "custom" },\n'
        '  { id = "second", runtime = "custom" },\n]\n'
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    custom = replace(DEFAULT_RUNTIME_REGISTRY.require('codex'), identifier='custom')

    parser = build_parser(runtime_registry=RuntimeRegistry((custom,)))

    assert parser.parse_args(['config', 'show']).runtime_registry.identifiers() == (
        'custom',
    )


def test_reviewer_sets_reject_an_empty_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reject an empty configured table instead of misreporting its provenance."""

    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text('[reviewer_sets]\n')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))

    assert main(['config', 'show']) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_settings'
    assert 'must contain at least one set' in document['error']['message']


def test_reviewer_sets_reject_each_documented_invalid_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep documented reviewer-set validation rules covered by the CLI contract."""

    cases = (
        (
            '[reviewer_sets.default]\nmembers = [{ id = "only", runtime = "codex" }]\n',
            'members must contain at least two reviewers',
        ),
        (
            (
                '[reviewer_sets.Default]\n'
                'members = [{ id = "one", runtime = "codex" }, '
                '{ id = "two", runtime = "codex" }]\n'
            ),
            "invalid reviewer set ID: 'Default'",
        ),
        ('[reviewer_sets.default]\n', 'reviewer_sets.default must contain members'),
        (
            (
                '[reviewer_sets.default]\nunknown = true\n'
                'members = [{ id = "one", runtime = "codex" }, '
                '{ id = "two", runtime = "codex" }]\n'
            ),
            'reviewer_sets.default contains unknown fields: unknown',
        ),
        (
            (
                '[reviewer_sets.default]\nmembers = [\n'
                '  { id = "one", runtime = "codex", unknown = true },\n'
                '  { id = "two", runtime = "codex" },\n]\n'
            ),
            'reviewer_sets.default.members[0] contains missing or unknown fields',
        ),
        (
            (
                '[reviewer_sets.default]\nmembers = [\n'
                '  { id = "one", runtime = "codex", required = false },\n'
                '  { id = "two", runtime = "codex" },\n]\n'
            ),
            'reviewer_sets.default.members[0].required must be true',
        ),
        (
            (
                '[reviewer_sets.default]\nmembers = [\n'
                '  { id = "one", runtime = "codex", model = "" },\n'
                '  { id = "two", runtime = "codex" },\n]\n'
            ),
            'reviewer_sets.default.members[0].model must be a non-empty string',
        ),
    )
    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))

    for configured, expected_message in cases:
        config.write_text(configured)

        assert main(['config', 'show']) == 2
        document = json.loads(capsys.readouterr().out)
        assert document['error']['code'] == 'invalid_settings'
        assert expected_message in document['error']['message']
