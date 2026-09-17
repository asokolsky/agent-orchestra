"""Tests for the non-interactive Claude Code reviewer adapter."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from agent_orchestra.adapter.claude_code import (
    ClaudeCodeReviewerAdapter,
    ClaudeCodeReviewerError,
)
from agent_orchestra.invocations import EffectiveModelStatus
from agent_orchestra.runtime_metadata import (
    RUNTIME_METADATA_ENV,
    RuntimeMetadata,
    read_runtime_metadata,
)


def run_claude_code_reviewer(
    request: Path, response: Path, *, model: str | None = None
) -> None:
    """Invoke the concrete Claude Code reviewer adapter."""

    ClaudeCodeReviewerAdapter(model).execute(request, response)


def _write_request(path: Path, worktree: Path, artifact: Path) -> None:
    """Write a complete canonical review request."""

    document = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': None,
        'run_id': '20260903T120000Z-a7f3c921',
        'sequence': 1,
        'iteration': 1,
        'message_type': 'review_request',
        'sender': 'orchestrator',
        'recipient': 'reviewer',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': {
            'worktree_path': str(worktree),
            'base_sha': 'base',
            'head_sha': 'head',
            'diff_digest': f'sha256:{"a" * 64}',
        },
        'payload': {
            'objective': 'Review the change.',
            'allowed_actions': [],
            'timeout_seconds': 1800,
            'artifact_path': str(artifact),
            'prior_review_path': None,
        },
    }
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding='utf-8')


@pytest.fixture(autouse=True)
def installed_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide an isolated Claude Code reviewer skill."""

    skill = tmp_path / 'claude/skills/agent-orchestra-reviewer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('review instructions\n')
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.skill_destination',
        lambda *_args, **_kwargs: skill,
    )


def _approved_result() -> dict[str, object]:
    """Return a valid canonical approval result."""

    return {
        'verdict': 'approved',
        'summary': 'Ready.',
        'findings': [],
        'validation': ['tests passed'],
        'verification_gaps': [],
    }


def test_claude_code_reviewer_is_read_only_and_writes_protocol_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin read-only CLI flags and canonical output translation."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    artifact = tmp_path / 'run/artifacts/review-0001.md'
    request = tmp_path / 'run/messages/request.json'
    response = tmp_path / 'run/result.json'
    _write_request(request, worktree, artifact)
    observed: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Capture the command and return Claude Code's JSON envelope."""

        observed['command'] = command
        observed['kwargs'] = kwargs
        skill_root = Path(command[command.index('--plugin-dir') + 1])
        observed['skill_root'] = skill_root
        assert (skill_root / 'skills/agent-orchestra-reviewer/SKILL.md').is_file()
        assert (skill_root / '.claude-plugin/plugin.json').is_file()
        stdout = json.dumps({'structured_output': _approved_result()})
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr='')

    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.shutil.which', lambda _: '/bin/claude'
    )
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.run_streaming_process', run
    )

    run_claude_code_reviewer(request, response, model='sonnet')

    command = observed['command']
    assert isinstance(command, list)
    assert command[0] == '/bin/claude'
    assert '--print' in command
    assert command[command.index('--permission-mode') + 1] == 'dontAsk'
    assert 'Bash(git diff --no-ext-diff --binary HEAD)' in command
    assert 'Bash(mise run tests)' not in command
    assert '--output' not in command
    assert command[command.index('--setting-sources') + 1] == ''
    assert command[command.index('--mcp-config') + 1] == '{"mcpServers":{}}'
    settings = json.loads(command[command.index('--settings') + 1])
    assert settings['sandbox']['filesystem']['denyWrite'] == [str(worktree.resolve())]
    assert settings['sandbox']['failIfUnavailable'] is True
    assert settings['sandbox']['allowUnsandboxedCommands'] is False
    skill_root = observed['skill_root']
    assert isinstance(skill_root, Path)
    assert not skill_root.exists()
    assert command[command.index('--model') + 1] == 'sonnet'
    kwargs = observed['kwargs']
    assert isinstance(kwargs, dict)
    assert kwargs['cwd'] == worktree
    environment = kwargs['env']
    assert isinstance(environment, dict)
    assert environment['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'] == '1'
    prompt = str(kwargs['input'])
    assert (
        'Skill tool with name '
        '`agent-orchestra-runtime:agent-orchestra-reviewer`' in prompt
    )
    assert 'Network access and project validation commands' in prompt
    assert 'network commands\nsuch as `curl`' in prompt
    assert 'do not run `mise trust`' in prompt
    assert json.loads(response.read_text())['payload']['verdict'] == 'approved'
    assert artifact.is_file()


def test_claude_code_reviewer_requires_structured_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject successful processes without a canonical structured result."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    _write_request(request, worktree, tmp_path / 'run/artifacts/review.md')
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.shutil.which', lambda _: '/bin/claude'
    )
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.run_streaming_process',
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout='{"result":"text"}', stderr=''
        ),
    )

    with pytest.raises(ClaudeCodeReviewerError, match='structured_output'):
        run_claude_code_reviewer(request, tmp_path / 'run/result.json')


def test_claude_code_reviewer_reports_models_before_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain reported provenance when Claude Code exits unsuccessfully."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    metadata = tmp_path / 'run/runtime.json'
    _write_request(request, worktree, tmp_path / 'run/artifacts/review.md')
    monkeypatch.setenv(RUNTIME_METADATA_ENV, str(metadata))
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.shutil.which', lambda _: '/bin/claude'
    )
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.run_streaming_process',
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            9,
            stdout=json.dumps(
                {
                    'modelUsage': {'claude-sonnet-4-6': {'inputTokens': 10}},
                    'structured_output': _approved_result(),
                }
            ),
            stderr='failed',
        ),
    )

    with pytest.raises(ClaudeCodeReviewerError, match='failed with code 9'):
        run_claude_code_reviewer(request, tmp_path / 'run/result.json')

    assert read_runtime_metadata(metadata) == RuntimeMetadata(
        effective_models=('claude-sonnet-4-6',),
        effective_model_status=EffectiveModelStatus.REPORTED,
    )


def test_claude_code_reviewer_reports_structured_output_exhaustion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve Claude's actionable structured-output failure classification."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    metadata = tmp_path / 'run/runtime.json'
    _write_request(request, worktree, tmp_path / 'run/artifacts/review.md')
    monkeypatch.setenv(RUNTIME_METADATA_ENV, str(metadata))
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.shutil.which', lambda _: '/bin/claude'
    )
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.run_streaming_process',
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps(
                {
                    'subtype': 'error_max_structured_output_retries',
                    'errors': [
                        'Failed to provide valid structured output after 5 attempts'
                    ],
                    'num_turns': 22,
                    'total_cost_usd': 0.48,
                    'permission_denials': [{'tool_name': 'Bash'}],
                    'modelUsage': {'claude-sonnet': {'inputTokens': 10}},
                }
            ),
            stderr='permission mode forced to default',
        ),
    )

    with pytest.raises(
        ClaudeCodeReviewerError, match='exhausted structured-output retries'
    ):
        run_claude_code_reviewer(request, tmp_path / 'run/result.json')

    assert read_runtime_metadata(metadata) == RuntimeMetadata(
        effective_models=('claude-sonnet',),
        effective_model_status=EffectiveModelStatus.REPORTED,
        failure_code='structured_output_exhausted',
        failure_message=(
            'claude-code exhausted structured-output retries: '
            'Failed to provide valid structured output after 5 attempts; '
            'num_turns=22; total_cost_usd=0.48; permission_denials=Bash'
        ),
    )


@pytest.mark.parametrize(
    ('subtype', 'failure_code', 'failure_message'),
    [
        (
            'error_max_turns',
            'turn_limit_exhausted',
            'claude-code exhausted its turn limit',
        ),
        (
            'error_max_budget_usd',
            'provider_budget_exhausted',
            'claude-code exhausted its budget limit',
        ),
        (
            'error_during_execution',
            'provider_execution_failed',
            'claude-code failed during execution',
        ),
    ],
)
def test_claude_code_reviewer_classifies_documented_failures(
    subtype: str,
    failure_code: str,
    failure_message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Map each documented nonzero result subtype to stable metadata."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    metadata = tmp_path / 'run/runtime.json'
    _write_request(request, worktree, tmp_path / 'run/artifacts/review.md')
    monkeypatch.setenv(RUNTIME_METADATA_ENV, str(metadata))
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.shutil.which', lambda _: '/bin/claude'
    )
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.run_streaming_process',
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            stdout=json.dumps({'subtype': subtype, 'num_turns': 22}),
            stderr='',
        ),
    )

    with pytest.raises(ClaudeCodeReviewerError, match=failure_message):
        run_claude_code_reviewer(request, tmp_path / 'run/result.json')

    assert read_runtime_metadata(metadata) == RuntimeMetadata(
        failure_code=failure_code,
        failure_message=f'{failure_message}; num_turns=22',
    )


def test_claude_code_reviewer_reports_models_before_schema_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain reported provenance when the structured result is invalid."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    metadata = tmp_path / 'run/runtime.json'
    _write_request(request, worktree, tmp_path / 'run/artifacts/review.md')
    monkeypatch.setenv(RUNTIME_METADATA_ENV, str(metadata))
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.shutil.which', lambda _: '/bin/claude'
    )
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.run_streaming_process',
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    'modelUsage': {'claude-sonnet-4-6': {'inputTokens': 10}},
                    'structured_output': {'verdict': 'approved'},
                }
            ),
            stderr='',
        ),
    )

    with pytest.raises(ClaudeCodeReviewerError):
        run_claude_code_reviewer(request, tmp_path / 'run/result.json')

    assert read_runtime_metadata(metadata) == RuntimeMetadata(
        effective_models=('claude-sonnet-4-6',),
        effective_model_status=EffectiveModelStatus.REPORTED,
    )


def test_claude_code_reviewer_parses_structured_output_with_invalid_utf8(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfdbinary: pytest.CaptureFixture[bytes],
) -> None:
    """Preserve invalid bytes while parsing the decoded structured response."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    artifact = tmp_path / 'run/artifacts/review.md'
    request = tmp_path / 'run/messages/request.json'
    response = tmp_path / 'run/result.json'
    _write_request(request, worktree, artifact)
    raw_output = (
        b'{"noise":"\xe9","structured_output":'
        + json.dumps(_approved_result()).encode()
        + b'}'
    )
    executable = tmp_path / 'bin/claude'
    executable.parent.mkdir()
    executable.write_text(
        f'#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write({raw_output!r})\n'
    )
    executable.chmod(0o755)
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.shutil.which', lambda _: str(executable)
    )

    run_claude_code_reviewer(request, response)

    captured = capfdbinary.readouterr()
    assert captured.out == raw_output
    assert json.loads(response.read_text())['payload']['verdict'] == 'approved'
    assert artifact.is_file()


def test_claude_code_reviewer_requires_installed_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return the actionable Claude Code installation command."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    _write_request(request, worktree, tmp_path / 'run/artifacts/review.md')
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.shutil.which', lambda _: '/bin/claude'
    )
    monkeypatch.setattr(
        'agent_orchestra.adapter.claude_code.skill_destination',
        lambda *_args, **_kwargs: tmp_path / 'missing',
    )

    with pytest.raises(ClaudeCodeReviewerError, match='--agent claude-code'):
        run_claude_code_reviewer(request, tmp_path / 'run/result.json')
