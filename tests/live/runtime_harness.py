"""Provider-neutral fixtures and assertions for live runtime scenarios."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from agent_orchestra.audit import build_audit_document
from agent_orchestra.evidence import resolve_evidence_path
from agent_orchestra.invocations import EffectiveModelStatus, InvocationEvidenceStore
from agent_orchestra.issue_review import run_issue_review
from agent_orchestra.issue_sources import IssueLocator, IssueSnapshot, write_snapshot
from agent_orchestra.models import IssueJob, Run, RunState
from agent_orchestra.store import JobStore

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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
    """Create a committed Python fixture and introduce one obvious regression."""

    repository = root / 'repository'
    repository.mkdir()
    git(repository, 'init', '--initial-branch=main')
    git(repository, 'config', 'user.name', 'Agent Orchestra Live Test')
    git(repository, 'config', 'user.email', 'live-test@example.invalid')
    (repository / '.gitignore').write_text(
        '__pycache__/\n.pytest_cache/\n', encoding='utf-8'
    )
    (repository / 'calculator.py').write_text(
        '"""Small deterministic live-runtime fixture."""\n\n'
        'def add(left: int, right: int) -> int:\n'
        '    """Return the sum of two integers."""\n\n'
        '    raise NotImplementedError\n',
        encoding='utf-8',
    )
    (repository / 'test_calculator.py').write_text(
        '"""Tests for the live-runtime fixture."""\n\n'
        'import unittest\n\n'
        'from calculator import add\n\n\n'
        'class CalculatorTest(unittest.TestCase):\n'
        '    """Exercise the public calculator behavior."""\n\n'
        '    def test_add_returns_sum(self) -> None:\n'
        '        """Addition must not subtract the right operand."""\n\n'
        '        self.assertEqual(add(2, 3), 5)\n\n\n'
        'if __name__ == "__main__":\n'
        '    unittest.main()\n',
        encoding='utf-8',
    )
    git(repository, 'add', '.gitignore', 'calculator.py', 'test_calculator.py')
    git(repository, 'commit', '-m', 'test: create live runtime fixture')
    (repository / 'calculator.py').write_text(
        '"""Small deterministic live-runtime fixture."""\n\n'
        'def add(left: int, right: int) -> int:\n'
        '    """Return the sum of two integers."""\n\n'
        '    return left - right\n',
        encoding='utf-8',
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
            'must perform arithmetic addition and pass `python -m unittest`. Identify '
            'the behavioral defect, request its smallest correction, and after '
            'remediation approve only when the implementation returns the sum.'
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
) -> tuple[str, ...]:
    """Verify a variable-length review cycle and return its expected role order."""

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
        assert all(item['disposition'] == 'addressed' for item in dispositions)

    expected_roles = ['reviewer']
    for _handoff in handoffs:
        expected_roles.extend(('developer', 'reviewer'))
    return tuple(expected_roles)


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
        [sys.executable, '-m', 'unittest'],
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
        requests, results, remediation_requests, handoffs
    )

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
