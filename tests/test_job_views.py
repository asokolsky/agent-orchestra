"""Tests for read-only job, task, attempt, and stream views."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING

from agent_orchestra.cli import main
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptStatus,
    InvocationRecord,
    transition_attempt,
    write_record,
)
from agent_orchestra.models import Run
from agent_orchestra.store import RunStore

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def create_job(tmp_path: Path) -> tuple[Path, Run, Path]:
    """Persist one job and return its database and evidence directory."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    job = Run.create_local(worktree, worktree, 'a' * 40, 'b' * 40, 'sha256:x')
    store.add(job)
    job_directory = tmp_path / 'evidence' / str(job.id)
    job_directory.mkdir(parents=True)
    return database, job, job_directory


def add_attempt(
    job: Run,
    job_directory: Path,
    *,
    sequence: int = 1,
    role: str = 'reviewer',
    attempt: int = 1,
    status: AttemptStatus = AttemptStatus.COMPLETED,
) -> str:
    """Write one valid attempt record and its separate streams."""

    task_id = f'{job.id}:{sequence:06d}-{role}'
    attempt_id = f'{task_id}:attempt-{attempt:04d}'
    logs = job_directory / 'logs'
    logs.mkdir(exist_ok=True)
    stdout = logs / f'{sequence:06d}-{role}-{attempt:04d}.stdout.log'
    stderr = logs / f'{sequence:06d}-{role}-{attempt:04d}.stderr.log'
    stdout.write_text('child stdout\n')
    stderr.write_text('child stderr\n')
    record_path = (
        job_directory / 'invocations' / f'{sequence:06d}-{role}-{attempt:04d}.json'
    )
    pending = InvocationRecord(
        schema_version=4,
        run_id=str(job.id),
        task_id=task_id,
        invocation_id=attempt_id,
        role=role,  # type: ignore[arg-type]
        agent_vendor='openai',
        requested_model='gpt-test',
        effective_models=('gpt-effective',),
        effective_model_status='reported',
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
        status='pending',
        conclusion=None,
    )
    write_record(record_path, pending)
    if status is AttemptStatus.PENDING:
        return task_id
    running = transition_attempt(pending, AttemptStatus.RUNNING)
    write_record(record_path, running)
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
    write_record(record_path, completed)
    return task_id


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
        job, job_directory, sequence=2, role='developer', status=AttemptStatus.PENDING
    )
    root = job_directory.parent

    assert main(arguments(database, 'jobs', None, root)) == 0
    jobs = json.loads(capsys.readouterr().out)
    assert jobs['schema_version'] == 10
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


def test_views_treat_absent_issue_tables_as_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Read an existing state database without creating issue tables."""

    database = tmp_path / 'state.db'
    RunStore(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute('DROP TABLE issue_actions')
        connection.execute('DROP TABLE issue_jobs')

    assert main(['--database', str(database), 'jobs', '--attention']) == 0
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 10,
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

    assert main(arguments(database, 'task', task_id, job_directory.parent)) == 0

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

    assert main(arguments(database, 'job', str(job.id), job_directory.parent)) == 0
    assert json.loads(capsys.readouterr().out)['job']['current'][0]['status'] == (
        'running'
    )


def test_task_views_report_structured_lookup_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail with stable JSON for invalid, unknown, and missing identifiers."""

    database, job, job_directory = create_job(tmp_path)
    root = job_directory.parent

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
        assert main(arguments(database, command, identifier, job_directory.parent)) == 0
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
    root = job_directory.parent
    job_directory.rmdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    job_directory.symlink_to(outside, target_is_directory=True)

    assert main(arguments(database, 'job', str(job.id), root)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_evidence'
    assert 'escapes' in document['error']['message']
