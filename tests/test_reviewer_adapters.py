"""Contract tests shared by built-in reviewer runtime adapters."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from agent_orchestra.adapter.claude_code import run_claude_code_reviewer
from agent_orchestra.adapter.codex import run_codex_reviewer

if TYPE_CHECKING:
    from collections.abc import Callable


def _write_request(path: Path, worktree: Path, artifact: Path) -> None:
    """Write one canonical review request."""

    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
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
                    'objective': 'Review the exact diff.',
                    'allowed_actions': [],
                    'timeout_seconds': 30,
                    'artifact_path': str(artifact),
                    'prior_review_path': None,
                },
            }
        ),
        encoding='utf-8',
    )


def _result() -> dict[str, object]:
    """Return one runtime-neutral canonical review result."""

    return {
        'verdict': 'approved',
        'summary': 'Ready.',
        'findings': [],
        'validation': ['tests passed'],
        'verification_gaps': [],
    }


@pytest.mark.parametrize('runtime', ['codex', 'claude-code'])
def test_reviewer_adapters_produce_equivalent_read_only_results(
    runtime: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep runtime mechanics out of the canonical reviewer response."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    artifact = tmp_path / 'run/artifacts/review.md'
    response = tmp_path / 'run/response.json'
    _write_request(request, worktree, artifact)
    skill = tmp_path / 'skills/agent-orchestra-reviewer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('review instructions\n')
    observed: dict[str, object] = {}

    if runtime == 'codex':
        monkeypatch.setattr(
            'agent_orchestra.adapter.codex.skill_destination',
            lambda *_args, **_kwargs: skill,
        )
        monkeypatch.setattr(
            'agent_orchestra.adapter.codex.shutil.which', lambda _: '/bin/codex'
        )

        def run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            """Capture Codex invocation and write its schema result."""

            observed['command'] = command
            observed['kwargs'] = kwargs
            result_path = Path(command[command.index('--output-last-message') + 1])
            result_path.write_text(json.dumps(_result()), encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

        monkeypatch.setattr('agent_orchestra.adapter.codex.run_streaming_process', run)
        invoke: Callable[..., None] = run_codex_reviewer
    else:
        monkeypatch.setattr(
            'agent_orchestra.adapter.claude_code.skill_destination',
            lambda *_args, **_kwargs: skill,
        )
        monkeypatch.setattr(
            'agent_orchestra.adapter.claude_code.shutil.which',
            lambda _: '/bin/claude',
        )

        def run(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            """Capture Claude Code invocation and return its schema result."""

            observed['command'] = command
            observed['kwargs'] = kwargs
            output = json.dumps({'structured_output': _result()})
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')

        monkeypatch.setattr(
            'agent_orchestra.adapter.claude_code.run_streaming_process', run
        )
        invoke = run_claude_code_reviewer

    invoke(request, response, model='runtime-model')

    command = observed['command']
    assert isinstance(command, list)
    if runtime == 'codex':
        assert command[command.index('--sandbox') + 1] == 'read-only'
        assert command[command.index('--cd') + 1] == str(worktree)
    else:
        assert command[command.index('--permission-mode') + 1] == 'dontAsk'
        kwargs = observed['kwargs']
        assert isinstance(kwargs, dict)
        assert kwargs['cwd'] == worktree
    assert command[command.index('--model') + 1] == 'runtime-model'
    document = json.loads(response.read_text())
    assert document['payload'] == {**_result(), 'artifact_path': str(artifact)}
    assert artifact.is_file()


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
def test_reviewer_adapters_report_stable_execution_failures(
    runtime: str,
    failure: str,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalize common reviewer failures for every supported runtime."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'run/messages/request.json'
    _write_request(request, worktree, tmp_path / 'run/artifacts/review.md')
    skill = tmp_path / 'skills/agent-orchestra-reviewer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('review instructions\n')
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

    monkeypatch.setattr(f'{module}.run_streaming_process', fail)
    invoke = run_codex_reviewer if runtime == 'codex' else run_claude_code_reviewer

    with pytest.raises(RuntimeError, match=expected):
        invoke(request, tmp_path / 'run/response.json')
