"""Tests for the issue-readiness review workflow."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_orchestra import issue_review
from agent_orchestra.adapter.base import IssueReviewExecution
from agent_orchestra.adapter.issue_reviewer import IssueReviewerError
from agent_orchestra.cli import main
from agent_orchestra.invocations import read_records
from agent_orchestra.issue_review import (
    IssueReviewError,
    publish_issue_feedback,
    run_issue_review,
)
from agent_orchestra.issue_sources import (
    IssueLocator,
    IssueSnapshot,
    ProviderFeedback,
    write_snapshot,
)
from agent_orchestra.models import IssueJob, RunState
from agent_orchestra.store import ConcurrentUpdateError, RunStore


def snapshot(
    *, body: str = 'Complete description', updated_at: str = '2026-01-02T00:00:00Z'
) -> IssueSnapshot:
    """Create one deterministic GitHub-shaped canonical snapshot."""

    digest = 'sha256:' + ('1' if body == 'Complete description' else '2') * 64
    return IssueSnapshot(
        locator=IssueLocator(
            'github',
            'github.com',
            'acme',
            'widgets',
            12,
            'https://github.com/acme/widgets/issues/12',
        ),
        title='Describe feature',
        body=body,
        author='author',
        labels=('feature',),
        state='open',
        created_at='2026-01-01T00:00:00Z',
        updated_at=updated_at,
        digest=digest,
    )


def setup_job(tmp_path: Path) -> tuple[RunStore, IssueJob, Path]:
    """Persist one captured issue and return its workflow context."""

    source = snapshot()
    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    job = IssueJob.create(
        provider=source.locator.provider,
        host=source.locator.host,
        remote_url=source.locator.url,
        namespace=source.locator.namespace,
        project=source.locator.project,
        issue_number=source.locator.number,
        title=source.title,
        author=source.author,
        source_updated_at=source.updated_at,
        source_digest=source.digest,
    )
    store.add_issue(job)
    runs = tmp_path / 'runs'
    write_snapshot(runs / job.id / 'issue.json', source)
    return store, job, runs


def write_reviewer(path: Path, verdict: str = 'ready') -> None:
    """Write a deterministic canonical issue reviewer."""

    path.write_text(
        f'''"""Test issue reviewer."""
import json
import sys
from pathlib import Path

request = json.loads(Path(sys.argv[1]).read_text())
result = {{
    "schema_version": 1,
    "source_digest": request["source"]["source_digest"],
    "verdict": "{verdict}",
    "summary": "The issue is clear.",
    "findings": [],
    "validation": ["Reviewed all required dimensions."],
    "verification_gaps": [],
}}
Path(sys.argv[2]).write_text(json.dumps(result))
''',
        encoding='utf-8',
    )


def test_run_issue_review_persists_result_and_feedback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Accept a current result and expose durable JSON and Markdown evidence."""

    store, job, runs = setup_job(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())

    finished = run_issue_review(
        job,
        store,
        runs,
        objective='Review readiness.',
        agent='codex',
        model=None,
        timeout=30,
        command=(sys.executable, str(reviewer)),
    )

    iteration = runs / job.id / 'iterations' / '000001'
    assert finished.state is RunState.APPROVED
    assert json.loads((iteration / 'result.json').read_text())['verdict'] == 'ready'
    assert '**Verdict:** ready' in (iteration / 'feedback.md').read_text()
    records = read_records(runs / job.id, job.id)
    assert len(records) == 1
    assert records[0].conclusion == 'succeeded'
    assert Path(records[0].stdout_path).read_text() == ''
    assert records[0].exit_code == 0


def test_feedback_renders_validation_and_verification_gaps() -> None:
    """Keep every explanatory result section in provider-facing feedback."""

    rendered = issue_review._render_feedback(
        {
            'verdict': 'blocked',
            'summary': 'More evidence is needed.',
            'findings': [],
            'validation': ['Checked the stated acceptance criteria.'],
            'verification_gaps': ['The external dependency is unspecified.'],
        }
    )

    assert '## Validation\n\n- Checked the stated acceptance criteria.' in rendered
    assert (
        '## Verification gaps\n\n- The external dependency is unspecified.' in rendered
    )


def test_resume_issue_review_retries_timed_out_builtin_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Resume a failed built-in issue review from its durable request and identity."""

    store, job, runs = setup_job(tmp_path)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())

    def timeout(*_args: object, **_kwargs: object) -> object:
        """Simulate a timed-out built-in adapter invocation."""

        message = 'timed out'
        raise IssueReviewerError(message, timed_out=True)

    monkeypatch.setattr(
        'agent_orchestra.issue_review.CodexIssueReviewerAdapter.execute', timeout
    )
    with pytest.raises(IssueReviewError, match='timed out'):
        run_issue_review(
            job,
            store,
            runs,
            objective='Review readiness.',
            agent='codex',
            model='codex-test',
            timeout=30,
        )

    result = {
        'schema_version': 1,
        'source_digest': snapshot().digest,
        'verdict': 'ready',
        'summary': 'The issue is ready.',
        'findings': [],
        'validation': ['Reviewed all dimensions.'],
        'verification_gaps': [],
    }
    monkeypatch.setattr(
        'agent_orchestra.issue_review.CodexIssueReviewerAdapter.execute',
        lambda *_args, **_kwargs: IssueReviewExecution(result, '', '', 0),
    )

    assert (
        main(
            [
                '--database',
                str(store.database_path),
                'resume',
                job.id,
                '--runs-directory',
                str(runs),
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)['state'] == 'approved'
    assert store.get_issue(job.id).state is RunState.APPROVED
    records = read_records(runs / job.id, job.id)
    assert [record.conclusion for record in records] == ['timed_out', 'succeeded']
    assert records[-1].requested_model == 'codex-test'


def test_run_issue_review_dispatches_claude_code_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the Claude Code implementation through issue orchestration."""

    store, job, runs = setup_job(tmp_path)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())
    calls: list[dict[str, object]] = []

    def execute(
        _adapter: object, request: dict[str, Any], *, timeout: int
    ) -> IssueReviewExecution:
        """Return one valid canonical result from the selected implementation."""

        calls.append(request)
        return IssueReviewExecution(
            {
                'schema_version': 1,
                'source_digest': request['source']['source_digest'],
                'verdict': 'ready',
                'summary': 'Ready.',
                'findings': [],
                'validation': ['Reviewed.'],
                'verification_gaps': [],
            },
            '',
            '',
            0,
            ('claude-test',),
        )

    monkeypatch.setattr(
        'agent_orchestra.issue_review.ClaudeCodeIssueReviewerAdapter.execute', execute
    )

    finished = run_issue_review(
        job,
        store,
        runs,
        objective='Review readiness.',
        agent='claude-code',
        model='sonnet',
        timeout=30,
    )

    assert finished.state is RunState.APPROVED
    assert len(calls) == 1
    assert read_records(runs / job.id, job.id)[0].effective_models == ('claude-test',)


def test_run_issue_review_rejects_change_during_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed when the provider revision moves during an agent invocation."""

    store, job, runs = setup_job(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer)
    snapshots = iter(
        (
            snapshot(),
            snapshot(body='Changed', updated_at='2026-01-03T00:00:00Z'),
        )
    )
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: next(snapshots))

    with pytest.raises(IssueReviewError, match='changed during review'):
        run_issue_review(
            job,
            store,
            runs,
            objective='Review readiness.',
            agent='codex',
            model=None,
            timeout=30,
            command=(sys.executable, str(reviewer)),
        )

    assert store.get_issue(job.id).state is RunState.FAILED
    records = read_records(runs / job.id, job.id)
    assert records[0].conclusion == 'failed'
    assert 'changed during review' in Path(records[0].stderr_path).read_text()


def test_run_issue_review_retries_failed_attempt_for_same_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reuse the durable request and increment the attempt after runtime failure."""

    store, job, runs = setup_job(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())

    with pytest.raises(IssueReviewError):
        run_issue_review(
            job,
            store,
            runs,
            objective='Review readiness.',
            agent='codex',
            model=None,
            timeout=30,
            command=(str(tmp_path / 'missing-reviewer'),),
        )

    failed = store.get_issue(job.id)
    finished = run_issue_review(
        failed,
        store,
        runs,
        objective='This replacement objective must not rewrite the request.',
        agent='codex',
        model=None,
        timeout=30,
        command=(sys.executable, str(reviewer)),
    )

    request = json.loads(
        (runs / job.id / 'iterations' / '000001' / 'request.json').read_text()
    )
    records = read_records(runs / job.id, job.id)
    assert finished.state is RunState.APPROVED
    assert request['objective'] == 'Review readiness.'
    assert [(record.attempt, record.conclusion) for record in records] == [
        (1, 'failed'),
        (2, 'succeeded'),
    ]


def test_revised_issue_after_failure_starts_without_missing_prior_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Start a new iteration when a failed iteration has no accepted result."""

    store, job, runs = setup_job(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())
    with pytest.raises(IssueReviewError):
        run_issue_review(
            job,
            store,
            runs,
            objective='Review readiness.',
            agent='codex',
            model=None,
            timeout=30,
            command=(str(tmp_path / 'missing-reviewer'),),
        )

    revised = snapshot(body='Changed', updated_at='2026-01-03T00:00:00Z')
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: revised)
    finished = run_issue_review(
        store.get_issue(job.id),
        store,
        runs,
        objective='Review readiness.',
        agent='codex',
        model=None,
        timeout=30,
        command=(sys.executable, str(reviewer)),
    )

    assert finished.iteration == 2
    request = json.loads(
        (runs / job.id / 'iterations' / '000002' / 'request.json').read_text()
    )
    assert request['prior_review'] is None


def test_run_issue_review_recovers_after_terminal_state_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finish a persisted accepted result without relaunching its reviewer."""

    store, job, runs = setup_job(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())
    update_issue = store.update_issue
    calls = 0

    def fail_terminal_update(updated: IssueJob, expected: RunState) -> None:
        """Fail only the state update after accepted evidence is durable."""

        nonlocal calls
        calls += 1
        if calls == 2:
            message = 'injected terminal update failure'
            raise ConcurrentUpdateError(message)
        update_issue(updated, expected)

    monkeypatch.setattr(store, 'update_issue', fail_terminal_update)
    with pytest.raises(ConcurrentUpdateError, match='injected'):
        run_issue_review(
            job,
            store,
            runs,
            objective='Review readiness.',
            agent='codex',
            model=None,
            timeout=30,
            command=(sys.executable, str(reviewer)),
        )

    persisted = store.get_issue(job.id)
    assert persisted.state is RunState.REVIEWING
    monkeypatch.setattr(store, 'update_issue', update_issue)

    recovered = run_issue_review(
        persisted,
        store,
        runs,
        objective='Review readiness.',
        agent='codex',
        model=None,
        timeout=30,
        command=(str(tmp_path / 'must-not-run'),),
    )

    assert recovered.state is RunState.APPROVED
    assert len(read_records(runs / job.id, job.id)) == 1


def test_run_issue_review_does_not_relaunch_running_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed when activation of the current attempt remains uncertain."""

    store, job, runs = setup_job(tmp_path)
    reviewing = replace(job, state=RunState.REVIEWING, iteration=1)
    store.update_issue(reviewing, RunState.QUEUED)
    issue_review._start_invocation(
        runs / job.id,
        reviewing,
        1,
        agent='codex',
        model=None,
        attempt=1,
    )
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())

    with pytest.raises(IssueReviewError, match='activation is uncertain'):
        run_issue_review(
            reviewing,
            store,
            runs,
            objective='Review readiness.',
            agent='codex',
            model=None,
            timeout=30,
            command=(str(tmp_path / 'must-not-run'),),
        )

    assert len(read_records(runs / job.id, job.id)) == 1


def test_run_issue_review_rejects_nested_evidence_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a symlink nested below an otherwise contained job directory."""

    store, job, runs = setup_job(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (runs / job.id / 'iterations').symlink_to(outside)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())

    with pytest.raises(IssueReviewError, match='contains a symlink'):
        run_issue_review(
            job,
            store,
            runs,
            objective='Review readiness.',
            agent='codex',
            model=None,
            timeout=30,
            command=('unused',),
        )

    assert list(outside.iterdir()) == []


def test_run_issue_review_does_not_accept_preexisting_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not overwrite or reuse a result that predates the invocation."""

    store, job, runs = setup_job(tmp_path)
    result = runs / job.id / 'iterations' / '000001' / 'result.json'
    result.parent.mkdir(parents=True)
    result.write_text('{"verdict":"ready"}\n')
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())

    with pytest.raises(IssueReviewError, match='already exists'):
        run_issue_review(
            job,
            store,
            runs,
            objective='Review readiness.',
            agent='codex',
            model=None,
            timeout=30,
            command=('unused',),
        )

    assert result.read_text() == '{"verdict":"ready"}\n'


def test_publish_issue_feedback_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Publish current feedback once and retain its provider identity."""

    store, job, runs = setup_job(tmp_path)
    iteration = runs / job.id / 'iterations' / '000001'
    iteration.mkdir(parents=True)
    (iteration / 'feedback.md').write_text('Review feedback.\n')
    reviewed = replace(job, state=RunState.CHANGES_REQUESTED, iteration=1)
    store.update_issue(reviewed, RunState.QUEUED)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())
    calls: list[str] = []

    def publish(*_args: object, **_kwargs: object) -> object:
        calls.append('publish')
        return ProviderFeedback('42', 'https://example.test/comment/42')

    monkeypatch.setattr(issue_review, 'publish_feedback', publish)

    first = publish_issue_feedback(reviewed, store, runs)
    second = publish_issue_feedback(reviewed, store, runs)

    assert first == second
    assert calls == ['publish']
    assert (
        main(
            [
                '--database',
                str(store.database_path),
                'job',
                job.id,
                '--runs-directory',
                str(runs),
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    assert document['job']['provider_actions'][0]['provider_id'] == '42'


def test_issue_review_records_timeout_truthfully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist timeout state and partial process streams in attempt evidence."""

    store, job, runs = setup_job(tmp_path)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())

    def timeout(*_args: object, **_kwargs: object) -> object:
        command = 'reviewer'
        raise subprocess.TimeoutExpired(command, 1, output='partial', stderr='late')

    monkeypatch.setattr('agent_orchestra.issue_review.subprocess.run', timeout)
    with pytest.raises(IssueReviewError, match='timed out'):
        run_issue_review(
            job,
            store,
            runs,
            objective='Review readiness.',
            agent='codex',
            model=None,
            timeout=1,
            command=('reviewer',),
        )

    record = read_records(runs / job.id, job.id)[0]
    assert record.conclusion == 'timed_out'
    assert record.timed_out is True
    assert Path(record.stdout_path).read_text() == 'partial'


def test_issue_review_is_visible_in_tasks_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose issue-review attempts through the common public task hierarchy."""

    store, job, runs = setup_job(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer)
    monkeypatch.setattr(issue_review, 'fetch_issue', lambda _url: snapshot())
    run_issue_review(
        job,
        store,
        runs,
        objective='Review readiness.',
        agent='codex',
        model=None,
        timeout=30,
        command=(sys.executable, str(reviewer)),
    )

    assert (
        main(
            [
                '--database',
                str(store.database_path),
                'tasks',
                job.id,
                '--runs-directory',
                str(runs),
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    assert document['tasks'][0]['role'] == 'issue_reviewer'
    assert document['tasks'][0]['attempts'][0]['streams']['stdout']['content'] == ''
    assert document['tasks'][0]['attempts'][0]['exit_code'] == 0


def test_post_issue_feedback_requires_explicit_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject provider publication unless its command-level gate is present."""

    assert (
        main(
            [
                '--database',
                str(tmp_path / 'state.db'),
                'post-issue-feedback',
                'job-1',
            ]
        )
        == 2
    )
    assert '--authorize is required' in capsys.readouterr().err
