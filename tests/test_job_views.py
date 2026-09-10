"""Tests for read-only job, task, attempt, and stream views."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from agent_orchestra import cli as cli_module
from agent_orchestra.adapter.registry import DEFAULT_RUNTIME_REGISTRY, RuntimeRole
from agent_orchestra.agents import (
    AgentRequest,
    AgentResult,
    CommandAgentAdapter,
    ReviewerRequest,
)
from agent_orchestra.cli import main
from agent_orchestra.evidence import evidence_root_for_job, resolve_evidence_path
from agent_orchestra.execution_context import WorkerContext
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptStatus,
    EffectiveModelStatus,
    InvocationIdentity,
    InvocationRecord,
    _write_record_unindexed,
    transition_attempt,
)
from agent_orchestra.models import Run
from agent_orchestra.reviewer_batch_run import run_queued_reviewer_set
from agent_orchestra.reviewer_paths import reviewer_invocation_stem, reviewer_task_id
from agent_orchestra.reviewer_plan import ReviewerExecution, ReviewerExecutionPlan
from agent_orchestra.store import JobStore

if TYPE_CHECKING:
    from pathlib import Path


DIGEST = f'sha256:{"a" * 64}'


def create_job(tmp_path: Path) -> tuple[Path, Run, Path]:
    """Persist one job and return its database and evidence directory."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    job = Run.create_local(worktree, worktree, 'a' * 40, 'b' * 40, 'sha256:x')
    store.add(job)
    job_directory = resolve_evidence_path(tmp_path / 'evidence', str(job.id))
    job_directory.mkdir(parents=True)
    return database, job, job_directory


def add_attempt(
    job: Run,
    job_directory: Path,
    *,
    sequence: int = 1,
    role: RuntimeRole = RuntimeRole.REVIEWER,
    attempt: int = 1,
    status: AttemptStatus = AttemptStatus.COMPLETED,
    reviewer_id: str | None = None,
) -> str:
    """Write one valid attempt record and its separate streams."""

    task_id = (
        reviewer_task_id(str(job.id), sequence, reviewer_id)
        if reviewer_id is not None
        else f'{job.id}:{sequence:06d}-{role.value}'
    )
    attempt_id = f'{task_id}:attempt-{attempt:04d}'
    stem = (
        reviewer_invocation_stem(sequence, reviewer_id, attempt)
        if reviewer_id is not None
        else f'{sequence:06d}-{role.value}-{attempt:04d}'
    )
    logs = job_directory / 'logs'
    logs.mkdir(exist_ok=True)
    stdout = logs / f'{stem}.stdout.log'
    stderr = logs / f'{stem}.stderr.log'
    stdout.write_text('child stdout\n')
    stderr.write_text('child stderr\n')
    record_path = job_directory / 'invocations' / f'{stem}.json'
    pending = InvocationRecord(
        schema_version=5 if reviewer_id is not None else 4,
        run_id=str(job.id),
        task_id=task_id,
        invocation_id=attempt_id,
        role=role,
        agent_vendor='openai',
        requested_model='gpt-test',
        effective_models=('gpt-effective',),
        effective_model_status=EffectiveModelStatus.REPORTED,
        runtime='codex',
        iteration=sequence,
        started_at='2026-09-07T10:00:00Z',
        finished_at=None,
        exit_code=None,
        timed_out=False,
        interrupted=False,
        stdout_path=str(stdout),
        stderr_path=str(stderr),
        attempt=attempt,
        status=AttemptStatus.PENDING,
        conclusion=None,
        reviewer_id=reviewer_id,
    )
    _write_record_unindexed(record_path, pending)
    if status is AttemptStatus.PENDING:
        return task_id
    running = transition_attempt(pending, AttemptStatus.RUNNING)
    _write_record_unindexed(record_path, running)
    if status is AttemptStatus.RUNNING:
        return task_id
    completed = transition_attempt(
        replace(running, exit_code=0),
        AttemptStatus.COMPLETED,
        conclusion=AttemptConclusion.SUCCEEDED,
        finished_at='2026-09-07T10:01:00Z',
        response_received_at='2026-09-07T10:01:00Z',
        validation_started_at='2026-09-07T10:01:00Z',
    )
    _write_record_unindexed(record_path, completed)
    return task_id


def write_review_batch(job: Run, job_directory: Path) -> dict[str, object]:
    """Write and return one valid public reviewer-batch summary."""

    stored = {
        'schema_version': 1,
        'run_id': str(job.id),
        'iteration': 1,
        'reviewer_set_id': 'default',
        'aggregation_policy': 'all_required',
        'diff_digest': f'sha256:{"a" * 64}',
        'verdict': 'approved',
        'reviewers': [
            {
                'reviewer_id': 'security',
                'outcome': 'approved',
                'result_path': 'messages/000002-security-review-result.json',
            },
            {
                'reviewer_id': 'portability',
                'outcome': 'approved',
                'result_path': 'messages/000002-portability-review-result.json',
            },
        ],
        'changes_requested_by': [],
        'blocked_by': [],
        'incomplete_reviewers': [],
    }
    batch_directory = job_directory / 'review-batches'
    batch_directory.mkdir()
    (batch_directory / '000001.json').write_text(json.dumps(stored))
    return {key: value for key, value in stored.items() if key != 'run_id'} | {
        'job_id': str(job.id),
        'path': 'review-batches/000001.json',
    }


def create_reviewed_batch_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    request_changes: bool = False,
) -> tuple[Path, Run, Path]:
    """Run one real reviewer set and return its persisted job state."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)
    runs_directory = tmp_path / 'runs'

    def approve(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        """Write one correlated reviewer approval through the real worker path."""

        assert isinstance(request, ReviewerRequest)
        if request.on_started is not None:
            request.on_started()
        document = json.loads(request.request_path.read_text(encoding='utf-8'))
        request.artifact_path.write_text('# Review\n', encoding='utf-8')
        response: dict[str, Any] = {
            'schema_version': 1,
            'message_id': str(uuid4()),
            'in_reply_to': document['message_id'],
            'run_id': document['run_id'],
            'sequence': document['sequence'] + 1,
            'iteration': document['iteration'],
            'message_type': 'review_result',
            'sender': 'reviewer',
            'recipient': 'orchestrator',
            'created_at': '2026-09-10T12:00:00Z',
            'scope': document['scope'],
            'payload': {
                'verdict': 'approved',
                'summary': 'approved',
                'findings': [],
                'validation': [],
                'verification_gaps': [],
                'artifact_path': str(request.artifact_path),
            },
        }
        if request_changes:
            response['payload']['verdict'] = 'changes_requested'
            response['payload']['summary'] = 'changes requested'
            response['payload']['findings'] = [
                {
                    'finding_id': 'finding-1',
                    'severity': 'medium',
                    'title': 'Cross-cutting finding',
                    'path': None,
                    'line': None,
                    'explanation': 'The finding spans multiple files.',
                    'acceptance_criterion': 'Address the shared behavior.',
                }
            ]
        request.response_path.write_text(json.dumps(response), encoding='utf-8')
        return AgentResult(
            succeeded=True,
            summary='approved',
            stdout='',
            stderr='',
            exit_code=0,
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', approve)
    plan = ReviewerExecutionPlan(
        'default',
        tuple(
            ReviewerExecution(
                reviewer_id=reviewer_id,
                command=('reviewer', reviewer_id),
                identity=InvocationIdentity(
                    vendor=vendor,
                    model=None,
                    runtime=runtime,
                ),
                timeout_seconds=30,
            )
            for reviewer_id, runtime, vendor in (
                ('security', 'codex', 'openai'),
                ('portability', 'claude-code', 'anthropic'),
            )
        ),
    )
    result = run_queued_reviewer_set(
        context=WorkerContext(
            store=store,
            runs_directory=runs_directory,
            digest_worktree=lambda _path, _base: DIGEST,
            registry=DEFAULT_RUNTIME_REGISTRY,
        ),
        run=run,
        objective='Review the change.',
        reviewer_plan=plan,
        developer_command=(),
        developer_timeout_seconds=30,
        max_iterations=3,
        developer_identity=InvocationIdentity(
            vendor='openai', model=None, runtime='codex'
        ),
    )
    return database, result, runs_directory


def arguments(
    database: Path, command: str, identifier: str | None, root: Path
) -> list[str]:
    """Build one task-aware read command."""

    result = ['--database', str(database), command]
    if identifier is not None:
        result.append(identifier)
    if command != 'jobs':
        result.extend(['--runs-directory', str(root)])
    return result


def test_four_views_use_public_vocabulary_and_current_array(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose jobs, task history, direct lookup, and only nonterminal current work."""

    database, job, job_directory = create_job(tmp_path)
    completed_id = add_attempt(job, job_directory, sequence=1)
    pending_id = add_attempt(
        job,
        job_directory,
        sequence=2,
        role=RuntimeRole.DEVELOPER,
        status=AttemptStatus.PENDING,
    )
    root = evidence_root_for_job(job_directory)

    assert main(arguments(database, 'jobs', None, root)) == 0
    jobs = json.loads(capsys.readouterr().out)
    assert jobs['schema_version'] == 19
    assert jobs['jobs'][0]['job_id'] == str(job.id)
    assert 'id' not in jobs['jobs'][0]

    assert main(arguments(database, 'job', str(job.id), root)) == 0
    current = json.loads(capsys.readouterr().out)['job']['current']
    assert current == [
        {
            'task_id': pending_id,
            'role': 'developer',
            'attempt': 1,
            'status': 'pending',
            'conclusion': None,
        }
    ]

    assert main(arguments(database, 'tasks', str(job.id), root)) == 0
    history = json.loads(capsys.readouterr().out)
    assert [task['task_id'] for task in history['tasks']] == [completed_id, pending_id]

    assert main(arguments(database, 'task', completed_id, root)) == 0
    attempt = json.loads(capsys.readouterr().out)['task']['attempts'][0]
    assert attempt['attempt_id'] == f'{completed_id}:attempt-0001'
    assert 'invocation_id' not in attempt
    assert attempt['streams']['stdout']['content'] == 'child stdout\n'
    # Exact key set, so a field added to InvocationRecord cannot reach the
    # documented CLI vocabulary without being declared in attempt_documents.
    assert list(attempt) == [
        'attempt_id',
        'attempt',
        'status',
        'conclusion',
        'agent_vendor',
        'requested_model',
        'effective_models',
        'effective_model_status',
        'runtime',
        'started_at',
        'finished_at',
        'response_received_at',
        'validation_started_at',
        'exit_code',
        'timed_out',
        'interrupted',
        'legacy',
        'streams',
    ]


def test_reviewer_batch_is_visible_from_job_and_reviewer_task(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expose reviewer identity and the validated aggregate without storage names."""

    database, job, job_directory = create_job(tmp_path)
    task_id = add_attempt(
        job,
        job_directory,
        sequence=1,
        reviewer_id='security',
    )
    pending_id = add_attempt(
        job,
        job_directory,
        sequence=2,
        status=AttemptStatus.PENDING,
        reviewer_id='portability',
    )
    expected = write_review_batch(job, job_directory)
    root = evidence_root_for_job(job_directory)
    monkeypatch.setattr(
        cli_module,
        'build_audit_document',
        lambda *_args, **_kwargs: {'findings': []},
    )

    assert main(arguments(database, 'job', str(job.id), root)) == 0
    job_document = json.loads(capsys.readouterr().out)['job']
    assert job_document['current'] == [
        {
            'task_id': pending_id,
            'role': 'reviewer',
            'attempt': 1,
            'status': 'pending',
            'conclusion': None,
            'reviewer_id': 'portability',
        }
    ]
    assert job_document['review_batches'] == [expected]
    assert 'run_id' not in job_document['review_batches'][0]

    assert main(arguments(database, 'tasks', str(job.id), root)) == 0
    tasks_document = json.loads(capsys.readouterr().out)
    assert tasks_document['review_batches'] == [expected]
    assert tasks_document['tasks'][0]['reviewer_id'] == 'security'

    assert main(arguments(database, 'task', task_id, root)) == 0
    task_document = json.loads(capsys.readouterr().out)['task']
    assert task_document['review_batch'] == expected


def test_real_reviewer_batch_is_visible_from_all_batch_views(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render worker-produced batch evidence through the real audit validator."""

    database, job, root = create_reviewed_batch_job(tmp_path, monkeypatch)
    task_id = reviewer_task_id(str(job.id), 1, 'security')

    assert main(arguments(database, 'job', str(job.id), root)) == 0
    job_document = json.loads(capsys.readouterr().out)['job']
    assert job_document['review_batches'][0]['verdict'] == 'approved'

    assert main(arguments(database, 'tasks', str(job.id), root)) == 0
    tasks_document = json.loads(capsys.readouterr().out)
    assert tasks_document['review_batches'] == job_document['review_batches']

    assert main(arguments(database, 'task', task_id, root)) == 0
    task_document = json.loads(capsys.readouterr().out)['task']
    assert task_document['review_batch'] == job_document['review_batches'][0]


def test_batch_view_preserves_null_finding_locations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep required nullable finding fields in the public batch contract."""

    database, job, root = create_reviewed_batch_job(
        tmp_path, monkeypatch, request_changes=True
    )

    assert main(arguments(database, 'job', str(job.id), root)) == 0
    batch = json.loads(capsys.readouterr().out)['job']['review_batches'][0]
    finding = batch['findings'][0]
    assert finding['path'] is None
    assert finding['line'] is None


def test_batch_views_survive_relocated_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve an aggregate artifact after its external evidence root moves."""

    database, job, root = create_reviewed_batch_job(tmp_path, monkeypatch)
    restored_root = tmp_path / 'restored-runs'
    root.rename(restored_root)
    task_id = reviewer_task_id(str(job.id), 1, 'security')

    for command, identifier in (
        ('job', str(job.id)),
        ('tasks', str(job.id)),
        ('task', task_id),
    ):
        assert main(arguments(database, command, identifier, restored_root)) == 0
        assert json.loads(capsys.readouterr().out)['error'] is None


@pytest.mark.parametrize('change', ['missing', 'modified'])
def test_batch_views_reject_invalid_aggregate_artifact(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Reject a batch whose indexed aggregate artifact no longer verifies."""

    database, job, root = create_reviewed_batch_job(tmp_path, monkeypatch)
    job_directory = resolve_evidence_path(root, str(job.id))
    artifact = job_directory / 'artifacts/review-batch-0001.md'
    if change == 'missing':
        artifact.unlink()
    else:
        artifact.write_text('# Modified review batch\n', encoding='utf-8')
    task_id = reviewer_task_id(str(job.id), 1, 'security')

    for command, identifier in (
        ('job', str(job.id)),
        ('tasks', str(job.id)),
        ('task', task_id),
    ):
        assert main(arguments(database, command, identifier, root)) == 2
        document = json.loads(capsys.readouterr().out)
        assert document['error']['code'] == 'invalid_evidence'
        assert f'evidence_{change}' in document['error']['message']


def test_job_view_rejects_modified_real_reviewer_batch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject worker-produced batch evidence modified after finalization."""

    database, job, root = create_reviewed_batch_job(tmp_path, monkeypatch)
    job_directory = resolve_evidence_path(root, str(job.id))
    batch_path = job_directory / 'review-batches' / '000001.json'
    batch = json.loads(batch_path.read_text(encoding='utf-8'))
    batch['diff_digest'] = f'sha256:{"b" * 64}'
    batch_path.write_text(json.dumps(batch), encoding='utf-8')

    assert main(arguments(database, 'job', str(job.id), root)) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_evidence'
    assert (
        'evidence_modified: evidence file was modified' in document['error']['message']
    )


def test_batch_views_ignore_orphaned_atomic_write_temporary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep batch views readable after a writer is killed before cleanup."""

    database, job, root = create_reviewed_batch_job(tmp_path, monkeypatch)
    job_directory = resolve_evidence_path(root, str(job.id))
    task_id = reviewer_task_id(str(job.id), 1, 'security')
    temporary = job_directory / 'review-batches' / f'.000002.json.{uuid4()}.tmp'
    temporary.write_text('{', encoding='utf-8')

    for command, identifier in (
        ('job', str(job.id)),
        ('tasks', str(job.id)),
        ('task', task_id),
    ):
        assert main(arguments(database, command, identifier, root)) == 0
        assert json.loads(capsys.readouterr().out)['error'] is None


def test_job_view_rejects_temporary_named_batch_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject a non-regular entry even when its name resembles writer output."""

    database, job, job_directory = create_job(tmp_path)
    temporary = job_directory / 'review-batches' / f'.000001.json.{uuid4()}.tmp'
    temporary.mkdir(parents=True)

    assert (
        main(
            arguments(
                database,
                'job',
                str(job.id),
                evidence_root_for_job(job_directory),
            )
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_evidence'
    assert document['error']['message'] == 'review batch evidence path is unsafe'


def test_job_view_rejects_temporary_from_another_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject a known temporary name that no reviewer-batch writer emits."""

    database, job, job_directory = create_job(tmp_path)
    batch_directory = job_directory / 'review-batches'
    batch_directory.mkdir()
    (batch_directory / '.review-result.json').write_text('{}', encoding='utf-8')

    assert (
        main(
            arguments(
                database,
                'job',
                str(job.id),
                evidence_root_for_job(job_directory),
            )
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_evidence'
    assert 'unexpected review batch evidence path' in document['error']['message']


def test_job_view_rejects_invalid_reviewer_batch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail closed when aggregate reviewer evidence is malformed."""

    database, job, job_directory = create_job(tmp_path)
    batch_directory = job_directory / 'review-batches'
    batch_directory.mkdir()
    (batch_directory / '000001.json').write_text('{}')

    assert (
        main(
            arguments(
                database,
                'job',
                str(job.id),
                evidence_root_for_job(job_directory),
            )
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_evidence'


def test_job_view_rejects_uncorrelated_reviewer_batch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject an aggregate that the authoritative audit validator cannot correlate."""

    database, job, job_directory = create_job(tmp_path)
    write_review_batch(job, job_directory)
    monkeypatch.setattr(
        cli_module,
        'build_audit_document',
        lambda *_args, **_kwargs: {
            'findings': [
                {
                    'code': 'scope_digest_mismatch',
                    'message': (
                        'review batch result digest differs from its review transition'
                    ),
                    'path': 'review-batches/000001.json',
                }
            ]
        },
    )

    assert (
        main(
            arguments(
                database,
                'job',
                str(job.id),
                evidence_root_for_job(job_directory),
            )
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_evidence'
    assert 'scope_digest_mismatch' in document['error']['message']


def test_views_treat_absent_issue_tables_as_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read an existing state database without creating issue tables."""

    database = tmp_path / 'state.db'
    JobStore(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TABLE issue_actions')
        connection.execute('DROP TABLE issue_jobs')

    assert main(['--database', str(database), 'jobs', '--attention']) == 0
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 19,
        'jobs': [],
        'error': None,
    }
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert 'issue_jobs' not in tables
    assert 'issue_actions' not in tables


def test_task_groups_retries_and_uses_custom_evidence_root(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Resolve a task globally and derive its status from the latest attempt."""

    database, job, job_directory = create_job(tmp_path)
    task_id = add_attempt(job, job_directory, attempt=1)
    add_attempt(job, job_directory, attempt=2, status=AttemptStatus.RUNNING)

    assert (
        main(arguments(database, 'task', task_id, evidence_root_for_job(job_directory)))
        == 0
    )

    task = json.loads(capsys.readouterr().out)['task']
    assert task['job_id'] == str(job.id)
    assert task['status'] == 'running'
    assert [attempt['attempt'] for attempt in task['attempts']] == [1, 2]


def test_job_does_not_read_attempt_stream_content(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the summary independent of potentially large stream content."""

    database, job, job_directory = create_job(tmp_path)
    add_attempt(job, job_directory, status=AttemptStatus.RUNNING)
    original_read_text = type(job_directory).read_text

    def reject_log_read(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if path.suffix == '.log':
            message = 'stream must not be read'
            raise OSError(message)
        return original_read_text(
            path, encoding=encoding, errors=errors, newline=newline
        )

    monkeypatch.setattr(type(job_directory), 'read_text', reject_log_read)

    assert (
        main(
            arguments(
                database, 'job', str(job.id), evidence_root_for_job(job_directory)
            )
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)['job']['current'][0]['status'] == (
        'running'
    )


def test_task_views_report_structured_lookup_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail with stable JSON for invalid, unknown, and missing identifiers."""

    database, job, job_directory = create_job(tmp_path)
    root = evidence_root_for_job(job_directory)

    assert main(arguments(database, 'task', 'invalid', root)) == 2
    invalid = json.loads(capsys.readouterr().out)
    assert invalid['task_id'] == 'invalid'
    assert invalid['error']['code'] == 'invalid_task_id'
    missing = f'{job.id}:000001-reviewer'
    assert main(arguments(database, 'task', missing, root)) == 2
    missing_task = json.loads(capsys.readouterr().out)
    assert missing_task['job_id'] == str(job.id)
    assert missing_task['task_id'] == missing
    assert missing_task['error']['code'] == 'task_not_found'
    assert main(arguments(database, 'job', 'unknown-job', root)) == 2
    missing_job = json.loads(capsys.readouterr().out)
    assert missing_job['job_id'] == 'unknown-job'
    assert missing_job['error']['code'] == 'job_not_found'


def test_task_views_are_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Leave database and evidence bytes unchanged after every public view."""

    database, job, job_directory = create_job(tmp_path)
    task_id = add_attempt(job, job_directory)
    before_database = database.read_bytes()
    before_files = {
        path.relative_to(job_directory): path.read_bytes()
        for path in job_directory.rglob('*')
        if path.is_file()
    }
    for command, identifier in (
        ('jobs', None),
        ('job', str(job.id)),
        ('tasks', str(job.id)),
        ('task', task_id),
    ):
        assert (
            main(
                arguments(
                    database, command, identifier, evidence_root_for_job(job_directory)
                )
            )
            == 0
        )
        capsys.readouterr()
    assert database.read_bytes() == before_database
    assert {
        path.relative_to(job_directory): path.read_bytes()
        for path in job_directory.rglob('*')
        if path.is_file()
    } == before_files


def test_job_view_rejects_symlinked_evidence_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Do not follow a selected job directory outside the evidence root."""

    database, job, job_directory = create_job(tmp_path)
    root = evidence_root_for_job(job_directory)
    job_directory.rmdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    job_directory.symlink_to(outside, target_is_directory=True)

    assert main(arguments(database, 'job', str(job.id), root)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_evidence'
    assert 'escapes' in document['error']['message']


def test_job_view_reports_missing_sharded_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a stored job whose derived evidence directory is absent."""

    database, job, job_directory = create_job(tmp_path)
    root = evidence_root_for_job(job_directory)
    job_directory.rmdir()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET state = 'reviewing', iteration = 1 WHERE id = ?",
            (str(job.id),),
        )

    assert main(arguments(database, 'job', str(job.id), root)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_evidence'
    assert document['job_id'] == str(job.id)


def test_queued_job_without_evidence_has_empty_task_views(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Treat a never-started source job as having no task evidence yet."""

    database, job, job_directory = create_job(tmp_path)
    root = evidence_root_for_job(job_directory)
    job_directory.rmdir()

    assert main(arguments(database, 'job', str(job.id), root)) == 0
    assert json.loads(capsys.readouterr().out)['job']['current'] == []
    assert main(arguments(database, 'tasks', str(job.id), root)) == 0
    assert json.loads(capsys.readouterr().out)['tasks'] == []
