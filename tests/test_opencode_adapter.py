"""Verify OpenCode's bounded command and canonical event boundary."""

from __future__ import annotations

import stat
import sys
from typing import TYPE_CHECKING

import pytest

from agent_orchestra.adapter import opencode
from agent_orchestra.adapter.issue_reviewer import IssueReviewerError
from agent_orchestra.adapter.opencode import OpenCodeAdapterError
from agent_orchestra.adapter.opencode_isolation import OpenCodeIsolationError
from agent_orchestra.adapter.registry import RuntimeRole

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def fake_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a deterministic executable in place of the OpenCode provider."""

    executable = tmp_path / 'fake-opencode'
    executable.write_text(
        f"""#!{sys.executable}
import json
import os
import sys
prompt = sys.stdin.read()
if '--format' not in sys.argv or 'json' not in sys.argv or not prompt:
    sys.exit(9)
if os.environ.get('FAKE_OPENCODE_MODE') == 'nonzero':
    sys.exit(4)
def emit(kind, part):
    print(json.dumps({{'type': kind, 'timestamp': 1, 'sessionID': 's1', 'part': {{'sessionID': 's1', **part}}}}), flush=True)
emit('step_start', {{'type': 'step-start'}})
emit('text', {{'type': 'text', 'text': '{{"ok":true}}'}})
if os.environ.get('FAKE_OPENCODE_MODE') != 'partial':
    emit('step_finish', {{'type': 'step-finish', 'cost': 0.01, 'tokens': {{'input': 10, 'output': 5, 'reasoning': 1, 'cache': {{'read': 2, 'write': 3}}}}}})
""",
        encoding='utf-8',
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(opencode, 'require_opencode', lambda: executable)
    monkeypatch.setattr(opencode, 'require_supported_version', lambda *_: None)
    monkeypatch.setattr(
        opencode, 'sandbox_command', lambda path, *_args, **_kw: [str(path)]
    )
    return executable


def test_opencode_run_consumes_complete_jsonl(
    tmp_path: Path, fake_opencode: Path
) -> None:
    """A process with complete events yields JSON and reported usage."""

    del fake_opencode
    scratch = tmp_path / 'scratch'
    directory = tmp_path / 'cwd'
    scratch.mkdir()
    directory.mkdir()

    value, stdout, stderr, models, usage = opencode._run(
        RuntimeRole.ISSUE_REVIEWER,
        'return JSON',
        directory,
        5,
        model='provider/model',
        variant='high',
        scratch=scratch,
    )

    assert value == {'ok': True}
    assert 'step_finish' in stdout
    assert stderr == ''
    assert models == ()
    assert usage.totals is not None
    assert usage.totals.input_tokens == 10


def test_developer_receives_review_evidence_and_result_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A developer prompt contains the finding it must address and its schema."""

    job = tmp_path / 'run'
    (job / 'messages').mkdir(parents=True)
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    review_result = job / 'review.json'
    review_artifact = job / 'review.md'
    review_result.write_text('{"findings":[{"finding_id":"F1"}]}')
    review_artifact.write_text('# Review\n')
    request = {
        'scope': {'worktree_path': str(worktree)},
        'payload': {
            'timeout_seconds': 30,
            'review_result_path': str(review_result),
            'review_artifact_path': str(review_artifact),
        },
    }
    monkeypatch.setattr(opencode, 'read_request', lambda _: request)
    monkeypatch.setattr(opencode, '_stage_skill', lambda *_: None)
    monkeypatch.setattr(opencode, 'write_handoff', lambda *_: None)
    captured: dict[str, object] = {}

    def fake_run(
        *args: object, **kwargs: object
    ) -> tuple[dict[str, object], str, str, tuple[str, ...], object]:
        """Record the prompt and exact external paths passed to the role."""

        captured['prompt'] = args[1]
        captured['external_read_paths'] = kwargs['external_read_paths']
        return {}, '', '', (), None

    monkeypatch.setattr(opencode, '_run', fake_run)

    opencode.OpenCodeDeveloperAdapter().execute(
        job / 'messages/request.json', job / 'messages/response.json'
    )

    assert '"finding_id":"F1"' in str(captured['prompt'])
    assert 'Result JSON Schema:' in str(captured['prompt'])
    assert captured['external_read_paths'] == (review_result, review_artifact)


def test_issue_reviewer_wraps_preflight_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing CLI remains a finished issue-review attempt failure."""

    def unavailable() -> Path:
        """Simulate an isolation preflight failure before process launch."""

        message = 'opencode executable not found'
        raise OpenCodeIsolationError(message)

    monkeypatch.setattr(opencode, 'require_opencode', unavailable)

    with pytest.raises(
        IssueReviewerError, match='opencode executable not found'
    ) as exc:
        opencode.OpenCodeIssueReviewerAdapter().execute({}, timeout=5)

    assert exc.value.exit_code is None
    assert exc.value.timed_out is False


@pytest.mark.parametrize(
    'mode',
    ['partial', 'nonzero'],
)
def test_opencode_run_rejects_failed_or_partial_output(
    tmp_path: Path,
    fake_opencode: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """A failed process or incomplete stream cannot produce workflow state."""

    del fake_opencode
    monkeypatch.setenv('FAKE_OPENCODE_MODE', mode)
    scratch = tmp_path / 'scratch'
    directory = tmp_path / 'cwd'
    scratch.mkdir()
    directory.mkdir()

    with pytest.raises(OpenCodeAdapterError) as raised:
        opencode._run(
            RuntimeRole.ISSUE_REVIEWER,
            'return JSON',
            directory,
            5,
            model=None,
            variant=None,
            scratch=scratch,
        )
    if mode == 'partial':
        assert 'step_start' in raised.value.stdout
        assert raised.value.exit_code == 0
    else:
        assert raised.value.exit_code == 4
