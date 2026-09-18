"""Provider-neutral fixtures and assertions for live runtime scenarios."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from agent_orchestra.audit import build_audit_document
from agent_orchestra.evidence import resolve_evidence_path
from agent_orchestra.invocations import EffectiveModelStatus, InvocationEvidenceStore
from agent_orchestra.issue_review import run_issue_review
from agent_orchestra.issue_sources import IssueLocator, IssueSnapshot, write_snapshot
from agent_orchestra.models import IssueJob, Run, RunState
from agent_orchestra.store import JobStore

if TYPE_CHECKING:
    import pytest


# The fixture owns its validation command instead of naming an interpreter. A
# runtime's sandbox need not put this suite's interpreter on PATH, and `python`
# there may be absent or be a different build, so a repository-relative wrapper
# is the only spelling that means the same thing to every runtime. It is also
# what makes the command an exact string: the harness can require the developer
# to report this and only this, rather than trusting it to describe whatever it
# happened to run.
LIVE_VALIDATION_COMMAND = './validate -m unittest'
LIVE_FIXTURE_DIRECTORY = Path(__file__).resolve().parents[1] / 'data' / 'live_runtime'


@dataclass(frozen=True, slots=True)
class LiveRuntime:
    """Describe one installed runtime without encoding provider branches."""

    identifier: str
    executable: str
    opt_in_environment: str
    model_environment: str
    skill_names: tuple[str, ...]
    effective_model_status: EffectiveModelStatus

    @property
    def requested_model(self) -> str | None:
        """Return the optional model selected for this live run."""

        return os.environ.get(self.model_environment) or None


@dataclass(frozen=True, slots=True)
class LocalScenario:
    """Paths and durable state produced by one local workflow scenario."""

    repository: Path
    database: Path
    runs_directory: Path
    job_id: str


class LiveCommandError(RuntimeError):
    """Report a failed bounded command without hiding its diagnostics."""


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 60,
    environment: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded command and return its complete text streams."""

    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except subprocess.TimeoutExpired as error:
        message = f'command timed out after {timeout}s: {command[0]}'
        raise LiveCommandError(message) from error
    except OSError as error:
        message = f'cannot execute {command[0]}: {error}'
        raise LiveCommandError(message) from error
    return completed


def require_success(
    completed: subprocess.CompletedProcess[str], *, context: str
) -> None:
    """Raise a concise error when one live command fails."""

    if completed.returncode == 0:
        return
    diagnostic = completed.stderr.strip() or completed.stdout.strip()
    message = f'{context} failed with exit code {completed.returncode}: {diagnostic}'
    raise LiveCommandError(message)


def git(repository: Path, *arguments: str) -> str:
    """Run one bounded Git command in the temporary scenario repository."""

    completed = run_command(['git', '-C', str(repository), *arguments], timeout=30)
    require_success(completed, context=f'git {arguments[0]}')
    return completed.stdout.strip()


def create_defective_repository(root: Path) -> Path:
    """
    Copy the committed Python fixture and introduce one obvious regression.

    The fixture is checked in rather than written here so that its sources stay
    readable, lintable, and diffable as ordinary files. Its `.runtime/python`
    symlink is created per repository and left untracked, because it points at
    whichever interpreter is running this suite and so is neither portable nor
    committable; `./validate` resolves it relative to its own location.
    """

    repository = root / 'repository'
    shutil.copytree(LIVE_FIXTURE_DIRECTORY / 'repository', repository)
    runtime_directory = repository / '.runtime'
    runtime_directory.mkdir()
    (runtime_directory / 'python').symlink_to(sys.executable)
    git(repository, 'init', '--initial-branch=main')
    git(repository, 'config', 'user.name', 'Agent Orchestra Live Test')
    git(repository, 'config', 'user.email', 'live-test@example.invalid')
    git(repository, 'add', '.')
    git(repository, 'commit', '-m', 'test: create live runtime fixture')
    shutil.copy2(
        LIVE_FIXTURE_DIRECTORY / 'changes' / 'calculator.py',
        repository / 'calculator.py',
    )
    return repository


def run_agent_orchestra(
    arguments: list[str], *, timeout: int, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke the public module entry point in the current test environment."""

    return run_command(
        [sys.executable, '-m', 'agent_orchestra', *arguments],
        cwd=cwd,
        timeout=timeout,
        environment=dict(os.environ),
    )


def run_local_scenario(
    root: Path, runtime: LiveRuntime, *, attempt_timeout: int
) -> LocalScenario:
    """Run a real review-remediation loop against a temporary repository."""

    repository = create_defective_repository(root)
    database = root / 'state' / 'state.db'
    runs_directory = root / 'evidence'
    enqueue = run_agent_orchestra(
        ['--database', str(database), 'enqueue-local', str(repository)],
        timeout=30,
    )
    require_success(enqueue, context='enqueue-local')
    job_id = enqueue.stdout.strip()
    if not job_id:
        message = 'enqueue-local returned no job ID'
        raise LiveCommandError(message)

    arguments = [
        '--database',
        str(database),
        'run',
        job_id,
        '--objective',
        (
            'Review the changed calculator implementation. The public add function '
            f'must perform arithmetic addition and pass `{LIVE_VALIDATION_COMMAND}`. '
            'Identify the behavioral defect, request its smallest correction, and '
            f'after remediation approve only when the developer ran exactly '
            f'`{LIVE_VALIDATION_COMMAND}`, reported that command with outcome '
            '`passed` in the handoff validation, and the implementation returns the '
            'sum.'
        ),
        '--runs-directory',
        str(runs_directory),
        '--reviewer-agent',
        runtime.identifier,
        '--developer-agent',
        runtime.identifier,
        '--timeout',
        str(attempt_timeout),
        '--developer-timeout',
        str(attempt_timeout),
        '--max-iterations',
        '3',
    ]
    if runtime.requested_model is not None:
        arguments.extend(['--reviewer-model', runtime.requested_model])
        arguments.extend(['--developer-model', runtime.requested_model])
    result = run_agent_orchestra(
        arguments,
        timeout=(attempt_timeout * 5) + 30,
        cwd=repository,
    )
    require_success(result, context='local review-remediation scenario')
    try:
        document: Any = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        message = 'local scenario returned non-JSON output'
        raise LiveCommandError(message) from error
    if not isinstance(document, dict):
        message = 'local scenario returned a non-object result'
        raise LiveCommandError(message)
    if document.get('job_id') != job_id:
        message = 'local scenario returned the wrong job ID'
        raise LiveCommandError(message)
    if document.get('state') != RunState.AWAITING_COMMIT_AUTHORIZATION:
        message = f'local scenario stopped in state {document.get("state")!r}'
        raise LiveCommandError(message)
    return LocalScenario(repository, database, runs_directory, job_id)


def _read_documents(paths: list[Path]) -> tuple[dict[str, Any], ...]:
    """Read ordered JSON evidence documents from the scenario directory."""

    return tuple(cast('dict[str, Any]', json.loads(path.read_text())) for path in paths)


def _assert_runtime_metadata(
    records: tuple[dict[str, object], ...], runtime: LiveRuntime
) -> None:
    """Apply the runtime's declared effective-model contract to attempt records."""

    expected = str(runtime.effective_model_status)
    assert all(record['effective_model_status'] == expected for record in records)
    if runtime.effective_model_status is EffectiveModelStatus.REPORTED:
        assert all(record['effective_models'] for record in records)
    else:
        assert all(not record['effective_models'] for record in records)


def assert_review_cycle_messages(
    requests: tuple[dict[str, Any], ...],
    results: tuple[dict[str, Any], ...],
    remediation_requests: tuple[dict[str, Any], ...],
    handoffs: tuple[dict[str, Any], ...],
    *,
    required_validation_command: str | None = None,
) -> tuple[str, ...]:
    """
    Verify a variable-length review cycle and return its expected role order.

    `required_validation_command` makes every remediation prove it ran the
    fixture's own validation and reported it as passed. Requiring that exact
    string is what keeps the check independent of how a runtime chooses to
    describe its work; asserting only that some validation passed would be
    satisfied by whatever the developer decided to run and report.
    """

    assert len(requests) == len(results)
    assert len(results) >= 2
    assert len(remediation_requests) == len(handoffs) == len(results) - 1
    for request, result in zip(requests, results, strict=True):
        assert result['in_reply_to'] == request['message_id']
        assert result['scope']['diff_digest'] == request['scope']['diff_digest']

    assert results[0]['payload']['verdict'] == 'changes_requested'
    assert results[-1]['payload']['verdict'] == 'approved'
    assert results[-1]['payload']['findings'] == []
    for result, remediation, handoff in zip(
        results[:-1], remediation_requests, handoffs, strict=True
    ):
        assert result['payload']['verdict'] == 'changes_requested'
        assert remediation['in_reply_to'] == result['message_id']
        assert handoff['in_reply_to'] == remediation['message_id']
        finding_ids = {
            finding['finding_id'] for finding in result['payload']['findings']
        }
        assert finding_ids
        dispositions = handoff['payload']['dispositions']
        assert {item['finding_id'] for item in dispositions} == finding_ids
        if required_validation_command is not None:
            assert any(
                item['command'] == required_validation_command
                and item['outcome'] == 'passed'
                for item in handoff['payload']['validation']
            )

    expected_roles = ['reviewer']
    for _handoff in handoffs:
        expected_roles.extend(('developer', 'reviewer'))
    return tuple(expected_roles)


def assert_consecutive_review_digests_change(
    requests: tuple[dict[str, Any], ...],
) -> None:
    """Require every remediation round to produce a new review digest."""

    digests = tuple(request['scope']['diff_digest'] for request in requests)
    assert all(previous != current for previous, current in pairwise(digests))


def assert_local_scenario(scenario: LocalScenario, runtime: LiveRuntime) -> None:
    """Verify the shared review, remediation, approval, and evidence contract."""

    store = JobStore(scenario.database)
    job = store.get(scenario.job_id)
    assert job.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    assert job.iteration >= 2
    assert git(scenario.repository, 'rev-list', '--count', 'HEAD') == '1'
    assert git(scenario.repository, 'status', '--short') == 'M calculator.py'
    assert 'return left + right' in (scenario.repository / 'calculator.py').read_text(
        encoding='utf-8'
    )
    tests = run_command(
        ['./validate', '-m', 'unittest'],
        cwd=scenario.repository,
        timeout=30,
    )
    require_success(tests, context='remediated fixture validation')

    job_directory = resolve_evidence_path(scenario.runs_directory, scenario.job_id)
    requests = _read_documents(
        sorted((job_directory / 'messages').glob('*-review-request.json'))
    )
    results = _read_documents(
        sorted((job_directory / 'messages').glob('*-review-result.json'))
    )
    remediation_requests = _read_documents(
        sorted((job_directory / 'messages').glob('*-remediation-request.json'))
    )
    handoffs = _read_documents(
        sorted((job_directory / 'messages').glob('*-developer-handoff.json'))
    )
    expected_roles = assert_review_cycle_messages(
        requests,
        results,
        remediation_requests,
        handoffs,
        required_validation_command=LIVE_VALIDATION_COMMAND,
    )
    assert_consecutive_review_digests_change(requests)

    records = invocation_records(scenario.runs_directory, scenario.job_id)
    assert tuple(record['role'] for record in records) == expected_roles
    assert all(record['runtime'] == runtime.identifier for record in records)
    assert all(record['conclusion'] == 'succeeded' for record in records)
    _assert_runtime_metadata(records, runtime)
    verified_audit(store, scenario.job_id, scenario.runs_directory)


def assert_issue_scenario(
    root: Path,
    runtime: LiveRuntime,
    *,
    attempt_timeout: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run and verify the shared immutable local issue-review scenario."""

    body = (
        'Problem: reject empty widget names before persistence.\n\n'
        'Scope: update only the create-widget validation path and its unit tests.\n\n'
        'Constraints: preserve the public API and existing error codes.\n\n'
        'Acceptance criteria:\n'
        '- whitespace-only names return invalid_widget_name;\n'
        '- valid names continue to be persisted;\n'
        '- unit tests cover both behaviors.\n'
    )
    digest = f'sha256:{sha256(body.encode()).hexdigest()}'
    snapshot = IssueSnapshot(
        locator=IssueLocator(
            provider='github',
            host='github.com',
            namespace='agent-orchestra-live',
            project='fixture',
            number=1,
            url='https://github.com/agent-orchestra-live/fixture/issues/1',
        ),
        title='Reject empty widget names',
        body=body,
        author='live-test',
        labels=('test',),
        state='open',
        created_at='2026-01-01T00:00:00Z',
        updated_at='2026-01-01T00:00:00Z',
        digest=digest,
    )
    job = IssueJob.create(
        provider=snapshot.locator.provider,
        host=snapshot.locator.host,
        remote_url=snapshot.locator.url,
        namespace=snapshot.locator.namespace,
        project=snapshot.locator.project,
        issue_number=snapshot.locator.number,
        title=snapshot.title,
        author=snapshot.author,
        source_updated_at=snapshot.updated_at,
        source_digest=snapshot.digest,
    )
    database = root / 'state' / 'state.db'
    runs_directory = root / 'evidence'
    store = JobStore(database)
    store.initialize()
    store.add_issue(job)
    write_snapshot(
        runs_directory,
        job.id,
        resolve_evidence_path(runs_directory, job.id) / 'issue.json',
        snapshot,
    )
    fetches: list[str] = []

    def fetch_local(url: str) -> IssueSnapshot:
        """Return the immutable fixture and record every attempted provider read."""

        fetches.append(url)
        return snapshot

    monkeypatch.setattr('agent_orchestra.issue_review.fetch_issue', fetch_local)
    finished = run_issue_review(
        job,
        store,
        runs_directory,
        objective='Review this issue for implementation readiness.',
        agent=runtime.identifier,
        model=runtime.requested_model,
        timeout=attempt_timeout,
    )

    assert finished.state in {RunState.APPROVED, RunState.CHANGES_REQUESTED}
    assert fetches == [snapshot.locator.url, snapshot.locator.url]
    result_path = (
        resolve_evidence_path(runs_directory, job.id) / 'iterations/000001/result.json'
    )
    result = json.loads(result_path.read_text(encoding='utf-8'))
    assert result['source_digest'] == snapshot.digest
    records = invocation_records(runs_directory, job.id)
    assert len(records) == 1
    assert records[0]['role'] == 'issue_reviewer'
    assert records[0]['runtime'] == runtime.identifier
    assert records[0]['conclusion'] == 'succeeded'
    _assert_runtime_metadata(records, runtime)
    assert store.list_issue_actions(job.id) == ()
    audit = verified_audit(store, job.id, runs_directory)
    evidence = cast('list[dict[str, object]]', audit['evidence'])
    assert any(
        item['evidence_type'] == 'issue_feedback' and item['status'] == 'verified'
        for item in evidence
    )


def verified_audit(
    store: JobStore, job_id: str, runs_directory: Path
) -> dict[str, object]:
    """Build and require a verified audit for either supported job shape."""

    job: Run | IssueJob
    try:
        job = store.get(job_id)
    except LookupError:
        job = store.get_issue(job_id)
    actions = () if isinstance(job, Run) else store.list_issue_actions(job_id)
    document = build_audit_document(
        job,
        store.list_transitions(job_id),
        actions,
        runs_directory,
        verify=True,
    )
    if document.get('result') != 'verified':
        findings = cast('list[dict[str, object]]', document.get('findings', []))
        codes = [finding.get('code') for finding in findings]
        message = f'audit verification failed: {codes}'
        raise LiveCommandError(message)
    return document


def invocation_records(
    runs_directory: Path, job_id: str
) -> tuple[dict[str, object], ...]:
    """Return live attempt records as JSON-shaped dictionaries."""

    job_directory = resolve_evidence_path(runs_directory, job_id)
    return tuple(
        {
            'role': str(record.role),
            'runtime': record.runtime,
            'conclusion': str(record.conclusion),
            'effective_models': record.effective_models,
            'effective_model_status': str(record.effective_model_status),
        }
        for record in InvocationEvidenceStore(job_directory).read_all(job_id)
    )
