"""Contract tests for built-in developer runtime adapters."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from agent_orchestra.adapter.claude_code import run_claude_code_developer
from agent_orchestra.adapter.codex import run_codex_developer

if TYPE_CHECKING:
    from collections.abc import Callable


def _request(path: Path, worktree: Path) -> None:
    """Write one canonical remediation request."""

    document = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': 'review-message-id',
        'run_id': '20260903T120000Z-a7f3c921',
        'sequence': 3,
        'iteration': 1,
        'message_type': 'remediation_request',
        'sender': 'orchestrator',
        'recipient': 'developer',
        'scope': {
            'worktree_path': str(worktree),
            'base_sha': 'base',
            'head_sha': 'head',
            'diff_digest': f'sha256:{"a" * 64}',
        },
        'payload': {
            'objective': 'Address every accepted finding.',
            'allowed_actions': [],
            'timeout_seconds': 1800,
            'review_result_path': '/run/messages/000002-review-result.json',
            'review_artifact_path': '/run/artifacts/review-0001.md',
        },
    }
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding='utf-8')


def _result() -> dict[str, object]:
    """Return one valid canonical developer handoff result."""

    return {
        'status': 'ready_for_review',
        'summary': 'Addressed the finding.',
        'files_changed': ['src/example.py'],
        'validation': [{'command': 'mise tests', 'outcome': 'passed'}],
        'dispositions': [
            {
                'finding_id': 'F-001',
                'disposition': 'addressed',
                'rationale': 'Implemented the required validation.',
            }
        ],
        'remaining_risks': [],
    }


@pytest.mark.parametrize('runtime', ['codex', 'claude-code'])
def test_developer_adapter_writes_equivalent_canonical_handoff(
    runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep runtime choice out of the persisted developer response."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    response = tmp_path / 'run/response.json'
    _request(request, worktree)
    skill = tmp_path / 'skills/agent-orchestra-developer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('developer instructions\n')

    if runtime == 'codex':
        monkeypatch.setattr(
            'agent_orchestra.adapter.codex.skill_destination',
            lambda *_args, **_kwargs: skill,
        )
        monkeypatch.setattr(
            'agent_orchestra.adapter.codex.shutil.which', lambda _: '/bin/codex'
        )

        def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            """Write Codex structured output to its declared result path."""

            result_path = Path(command[command.index('--output-last-message') + 1])
            result_path.write_text(json.dumps(_result()), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

        monkeypatch.setattr('agent_orchestra.adapter.codex.subprocess.run', run)
        invoke: Callable[..., None] = run_codex_developer
    else:
        monkeypatch.setattr(
            'agent_orchestra.adapter.claude_code.skill_destination',
            lambda *_args, **_kwargs: skill,
        )
        monkeypatch.setattr(
            'agent_orchestra.adapter.claude_code.shutil.which',
            lambda _: '/bin/claude',
        )
        monkeypatch.setattr(
            'agent_orchestra.adapter.claude_code.subprocess.run',
            lambda command, **_kwargs: subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({'structured_output': _result()}),
                stderr='',
            ),
        )
        invoke = run_claude_code_developer

    invoke(request, response, model='runtime-model')

    document = json.loads(response.read_text())
    assert document['sequence'] == 4
    assert document['message_type'] == 'developer_handoff'
    assert document['payload'] == _result()
    assert 'model' not in document


def test_claude_developer_confines_writes_to_primary_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Use the OS sandbox's primary-working-directory write boundary."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    response = tmp_path / 'run/response.json'
    _request(request, worktree)
    skill = tmp_path / 'skills/agent-orchestra-developer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('developer instructions\n')
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.skill_destination',
        lambda *_args, **_kwargs: skill,
    )
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.shutil.which', lambda _: '/bin/claude'
    )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Capture the isolated invocation and return a valid result."""

        observed['command'] = command
        observed['kwargs'] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps({'structured_output': _result()}),
            stderr='',
        )

    monkeypatch.setattr('agent_orchestra.adapter.claude_code.subprocess.run', run)

    run_claude_code_developer(request, response)

    command = observed['command']
    assert isinstance(command, list)
    assert command[command.index('--setting-sources') + 1] == ''
    settings = json.loads(command[command.index('--settings') + 1])
    assert settings == {
        'sandbox': {
            'enabled': True,
            'failIfUnavailable': True,
            'allowUnsandboxedCommands': False,
        }
    }
    assert '--strict-mcp-config' in command
    assert command[command.index('--mcp-config') + 1] == '{"mcpServers":{}}'
    assert '--add-dir' not in command
    assert 'additionalDirectories' not in settings
    assert 'allowWrite' not in json.dumps(settings)
    assert command[command.index('--tools') + 1] != 'default'
    assert 'Edit(/)' not in command
    kwargs = observed['kwargs']
    assert isinstance(kwargs, dict)
    environment = kwargs['env']
    assert isinstance(environment, dict)
    assert environment['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'] == '1'


@pytest.mark.skipif(shutil.which('claude') is None, reason='claude is not installed')
def test_claude_cli_accepts_isolated_empty_mcp_configuration() -> None:
    """Validate critical isolation flags against the installed Claude CLI."""

    executable = shutil.which('claude')
    assert executable is not None
    completed = subprocess.run(
        [
            executable,
            '--mcp-config',
            '{"mcpServers":{}}',
            '--strict-mcp-config',
            '--setting-sources',
            '',
            '--settings',
            json.dumps(
                {
                    'sandbox': {
                        'enabled': True,
                        'failIfUnavailable': True,
                        'allowUnsandboxedCommands': False,
                    }
                }
            ),
            'mcp',
            'list',
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert 'No MCP servers configured' in completed.stdout


@pytest.mark.parametrize('runtime', ['codex', 'claude-code'])
@pytest.mark.parametrize(
    ('failure', 'expected'),
    [
        ('missing', 'executable not found'),
        ('timeout', 'timed out'),
        ('nonzero', 'failed with code 9'),
        ('malformed', 'JSON|structured_output'),
    ],
)
def test_developer_adapters_report_stable_execution_failures(
    runtime: str,
    failure: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalize common developer failures for every supported runtime."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    _request(request, worktree)
    skill = tmp_path / 'skills/agent-orchestra-developer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('developer instructions\n')
    module = (
        'agent_orchestra.adapter.codex'
        if runtime == 'codex'
        else 'agent_orchestra.adapter.claude_code'
    )
    executable = 'codex' if runtime == 'codex' else 'claude'
    monkeypatch.setattr(f'{module}.skill_destination', lambda *_args: skill)
    monkeypatch.setattr(
        f'{module}.shutil.which',
        (lambda _: None) if failure == 'missing' else (lambda _: f'/bin/{executable}'),
    )

    def fail(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        """Return or raise the selected deterministic process failure."""

        if failure == 'timeout':
            raise subprocess.TimeoutExpired(command, 1)
        if failure == 'nonzero':
            return subprocess.CompletedProcess(command, 9, stdout='', stderr='failed')
        if runtime == 'claude-code':
            return subprocess.CompletedProcess(command, 0, stdout='not-json', stderr='')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(f'{module}.subprocess.run', fail)
    invoke = run_codex_developer if runtime == 'codex' else run_claude_code_developer

    with pytest.raises(RuntimeError, match=expected):
        invoke(request, tmp_path / 'run/response.json')


def test_codex_developer_rejects_nonpositive_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject an invalid timeout before attempting to locate or invoke Codex."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    _request(request, worktree)
    document = json.loads(request.read_text())
    document['payload']['timeout_seconds'] = 0
    request.write_text(json.dumps(document))
    monkeypatch.setattr(
        'agent_orchestra.adapter.codex.shutil.which',
        lambda _: pytest.fail('Codex lookup must not occur'),
    )

    with pytest.raises(RuntimeError, match='codex development timed out'):
        run_codex_developer(request, tmp_path / 'run/response.json')
