"""Tests for the built-in non-interactive Codex reviewer adapter."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from agent_orchestra.codex_reviewer import (
    REVIEW_SCHEMA,
    CodexReviewerError,
    run_codex_reviewer,
)
from agent_orchestra.models import REVIEW_FINDING_FIELDS


def write_request(path: Path, worktree: Path, artifact: Path) -> None:
    """Write one complete review request for adapter tests."""

    request = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': None,
        'run_id': '20260902T130000Z-a7f3c921',
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request), encoding='utf-8')


@pytest.fixture(autouse=True)
def installed_reviewer_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide an isolated installed reviewer skill for adapter tests."""

    skill = tmp_path / 'codex/skills/agent-orchestra-reviewer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('review instructions\n')
    monkeypatch.setattr(
        'agent_orchestra.codex_reviewer.skill_destination',
        lambda *_args, **_kwargs: skill,
    )


def approved_result() -> dict[str, object]:
    """Return a schema-compatible approved Codex result."""

    return {
        'verdict': 'approved',
        'summary': 'The change is ready.',
        'findings': [],
        'validation': ['pytest passed'],
        'verification_gaps': [],
    }


def test_review_schema_has_deterministic_required_field_order() -> None:
    """Keep the serialized schema stable across interpreter processes."""

    finding_schema = REVIEW_SCHEMA['properties']['findings']['items']

    assert finding_schema['required'] == sorted(REVIEW_FINDING_FIELDS)


def test_codex_adapter_runs_read_only_and_writes_protocol_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Constrain Codex and convert its result into durable protocol files."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    artifact = tmp_path / 'runs/artifacts/review-0001.md'
    request = tmp_path / 'runs/messages/request.json'
    response = tmp_path / 'runs/response.json'
    request.parent.mkdir(parents=True)
    write_request(request, worktree, artifact)
    observed: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Capture the Codex command and synthesize its structured result."""

        observed['command'] = command
        observed['input'] = kwargs['input']
        result_path = Path(command[command.index('--output-last-message') + 1])
        result_path.write_text(json.dumps(approved_result()), encoding='utf-8')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(
        'agent_orchestra.codex_reviewer.shutil.which', lambda _: '/bin/codex'
    )
    monkeypatch.setattr('agent_orchestra.codex_reviewer.subprocess.run', run)

    run_codex_reviewer(request, response, model='review-model')

    command = observed['command']
    assert isinstance(command, list)
    assert command[:2] == ['/bin/codex', 'exec']
    assert '--ignore-user-config' in command
    assert command[command.index('--sandbox') + 1] == 'read-only'
    assert command[command.index('--cd') + 1] == str(worktree)
    assert command[command.index('--model') + 1] == 'review-model'
    assert '$agent-orchestra-reviewer' in str(observed['input'])
    document = json.loads(response.read_text(encoding='utf-8'))
    assert document['in_reply_to'] == json.loads(request.read_text())['message_id']
    assert document['payload']['artifact_path'] == str(artifact)
    assert document['payload']['verdict'] == 'approved'
    assert 'pytest passed' in artifact.read_text(encoding='utf-8')


def test_codex_adapter_requires_installed_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report a stable error when the Codex executable is unavailable."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'messages/request.json'
    write_request(request, worktree, tmp_path / 'artifacts/review.md')
    monkeypatch.setattr('agent_orchestra.codex_reviewer.shutil.which', lambda _: None)

    with pytest.raises(CodexReviewerError, match='codex executable not found'):
        run_codex_reviewer(request, tmp_path / 'response.json')


def test_codex_adapter_rejects_approved_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject an internally inconsistent schema-constrained result."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'messages/request.json'
    write_request(request, worktree, tmp_path / 'artifacts/review.md')
    response = tmp_path / 'response.json'
    result = approved_result()
    result['findings'] = [
        {
            'finding_id': 'F-001',
            'severity': 'medium',
            'title': 'Finding',
            'path': 'file.py',
            'line': 1,
            'explanation': 'Explanation.',
            'acceptance_criterion': 'Fix it.',
        }
    ]

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        """Write the inconsistent result returned by a mocked Codex CLI."""

        result_path = Path(command[command.index('--output-last-message') + 1])
        result_path.write_text(json.dumps(result), encoding='utf-8')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    monkeypatch.setattr(
        'agent_orchestra.codex_reviewer.shutil.which', lambda _: '/bin/codex'
    )
    monkeypatch.setattr('agent_orchestra.codex_reviewer.subprocess.run', run)

    with pytest.raises(CodexReviewerError, match='cannot contain findings'):
        run_codex_reviewer(request, response)


def test_codex_adapter_requires_reviewer_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a silent fallback when the reviewer role skill is absent."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'messages/request.json'
    write_request(request, worktree, tmp_path / 'artifacts/review.md')
    monkeypatch.setattr(
        'agent_orchestra.codex_reviewer.shutil.which', lambda _: '/bin/codex'
    )
    monkeypatch.setattr(
        'agent_orchestra.codex_reviewer.skill_destination',
        lambda *_args, **_kwargs: tmp_path / 'missing-skill',
    )

    with pytest.raises(CodexReviewerError, match='skills install'):
        run_codex_reviewer(request, tmp_path / 'response.json')


def test_codex_adapter_rejects_artifact_outside_run_directory(tmp_path: Path) -> None:
    """Prevent a standalone request from writing an arbitrary artifact path."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    run_directory = tmp_path / 'run'
    request = run_directory / 'messages/request.json'
    write_request(request, worktree, tmp_path / 'outside/review.md')

    with pytest.raises(CodexReviewerError, match='inside the request run directory'):
        run_codex_reviewer(request, run_directory / 'response.json')


def test_codex_adapter_reports_request_outside_messages_directory(
    tmp_path: Path,
) -> None:
    """Diagnose an unsupported standalone request layout accurately."""

    worktree = tmp_path / 'repo'
    worktree.mkdir()
    request = tmp_path / 'request.json'
    write_request(request, worktree, tmp_path / 'artifacts/review.md')

    with pytest.raises(CodexReviewerError, match='run messages directory'):
        run_codex_reviewer(request, tmp_path / 'response.json')
