"""Tests for read-only run log discovery and filtering."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from agent_orchestra.cli import main
from agent_orchestra.invocations import InvocationRecord, write_record
from agent_orchestra.models import Run
from agent_orchestra.store import RunStore

if TYPE_CHECKING:
    from pathlib import Path


def create_run(tmp_path: Path) -> tuple[Path, Run, Path]:
    """Persist one run and return its database and evidence directory."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    run = Run.create_local(worktree, worktree, 'a' * 40, 'b' * 40, 'sha256:x')
    store.add(run)
    run_directory = tmp_path / 'runs' / str(run.id)
    run_directory.mkdir(parents=True)
    return database, run, run_directory


def add_invocation(
    run: Run,
    run_directory: Path,
    *,
    invocation_id: str,
    role: str = 'reviewer',
    iteration: int = 1,
    runtime: str = 'codex',
) -> None:
    """Write one invocation record and its two streams."""

    logs = run_directory / 'logs'
    logs.mkdir(exist_ok=True)
    stdout = logs / f'{invocation_id}.stdout.log'
    stderr = logs / f'{invocation_id}.stderr.log'
    stdout.write_text(f'{invocation_id} output\n')
    stderr.write_text('')
    write_record(
        run_directory / 'invocations' / f'{invocation_id}.json',
        InvocationRecord(
            schema_version=1,
            run_id=str(run.id),
            invocation_id=invocation_id,
            role=role,  # type: ignore[arg-type]
            agent_vendor='openai',
            agent_model='gpt-test',
            runtime=runtime,
            iteration=iteration,
            started_at='2026-09-03T10:00:00Z',
            finished_at='2026-09-03T10:01:00Z',
            exit_code=0,
            timed_out=False,
            interrupted=False,
            stdout_path=str(stdout),
            stderr_path=str(stderr),
        ),
    )


def logs_arguments(database: Path, run: Run, run_directory: Path) -> list[str]:
    """Return common command arguments for a temporary run."""

    return [
        '--database',
        str(database),
        'logs',
        str(run.id),
        '--runs-directory',
        str(run_directory.parent),
    ]


def test_logs_identifies_and_orders_separate_streams(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Render invocation identity and preserve deterministic stream ordering."""

    database, run, run_directory = create_run(tmp_path)
    add_invocation(run, run_directory, invocation_id='second', iteration=2)
    add_invocation(run, run_directory, invocation_id='first', iteration=1)

    assert main(logs_arguments(database, run, run_directory)) == 0

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    assert document['schema_version'] == 4
    assert document['run_id'] == str(run.id)
    assert document['failures'] == []
    assert document['error'] is None
    assert [
        (entry['invocation_id'], entry['stream']) for entry in document['streams']
    ] == [
        ('first', 'stdout'),
        ('first', 'stderr'),
        ('second', 'stdout'),
        ('second', 'stderr'),
    ]
    first = document['streams'][0]
    assert first == {
        'invocation_id': 'first',
        'role': 'reviewer',
        'agent_vendor': 'openai',
        'agent_model': 'gpt-test',
        'runtime': 'codex',
        'iteration': 1,
        'started_at': '2026-09-03T10:00:00Z',
        'finished_at': '2026-09-03T10:01:00Z',
        'exit_code': 0,
        'timed_out': False,
        'interrupted': False,
        'stream': 'stdout',
        'path': str(run_directory / 'logs/first.stdout.log'),
        'content': 'first output\n',
        'legacy': False,
    }
    assert document['streams'][1]['content'] == ''


def test_logs_filters_by_all_supported_identity_fields(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Select one stream using iteration, role, invocation, runtime, and stream."""

    database, run, run_directory = create_run(tmp_path)
    add_invocation(
        run,
        run_directory,
        invocation_id='chosen',
        role='developer',
        iteration=2,
        runtime='claude-code',
    )
    add_invocation(run, run_directory, invocation_id='other')
    arguments = logs_arguments(database, run, run_directory)
    arguments.extend(
        [
            '--iteration',
            '2',
            '--role',
            'developer',
            '--invocation',
            'chosen',
            '--runtime',
            'claude-code',
            '--stream',
            'stdout',
        ]
    )

    assert main(arguments) == 0

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    assert len(document['streams']) == 1
    assert document['streams'][0]['invocation_id'] == 'chosen'
    assert document['streams'][0]['stream'] == 'stdout'
    assert document['streams'][0]['content'] == 'chosen output\n'


def test_logs_resolves_relative_record_paths_against_run_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read the same run-contained file whose relative path was validated."""

    database, run, run_directory = create_run(tmp_path)
    add_invocation(run, run_directory, invocation_id='one')
    manifest = run_directory / 'invocations/one.json'
    record = json.loads(manifest.read_text())
    record['stdout_path'] = 'logs/one.stdout.log'
    record['stderr_path'] = 'logs/one.stderr.log'
    manifest.write_text(json.dumps(record))
    unrelated = tmp_path / 'unrelated'
    (unrelated / 'logs').mkdir(parents=True)
    (unrelated / 'logs/one.stdout.log').write_text('wrong output\n')
    monkeypatch.chdir(unrelated)

    assert main(logs_arguments(database, run, run_directory)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['streams'][0]['path'] == str(run_directory / 'logs/one.stdout.log')
    assert document['streams'][0]['content'] == 'one output\n'


def test_logs_reports_empty_and_missing_streams(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Show empty streams and diagnose independently missing files."""

    database, run, run_directory = create_run(tmp_path)
    add_invocation(run, run_directory, invocation_id='one')
    (run_directory / 'logs/one.stdout.log').unlink()

    assert main(logs_arguments(database, run, run_directory)) == 2

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    assert document['error'] is None
    assert len(document['streams']) == 1
    assert document['streams'][0]['stream'] == 'stderr'
    assert document['streams'][0]['content'] == ''
    assert document['failures'] == [
        {
            'code': 'missing_log',
            'stream': 'stdout',
            'path': str(run_directory / 'logs/one.stdout.log'),
            'message': f'missing stdout log: {run_directory}/logs/one.stdout.log',
        }
    ]


def test_logs_reads_legacy_files_with_unknown_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep legacy log files useful without inventing invocation identity."""

    database, run, run_directory = create_run(tmp_path)
    logs = run_directory / 'logs'
    logs.mkdir()
    (logs / '000001-reviewer.stdout.log').write_text('legacy output\n')
    (logs / '000001-reviewer.stderr.log').write_text('')

    assert main(logs_arguments(database, run, run_directory)) == 0

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    stdout = document['streams'][0]
    assert stdout['invocation_id'] == 'legacy-000001-reviewer'
    assert stdout['agent_vendor'] is None
    assert stdout['agent_model'] is None
    assert stdout['runtime'] is None
    assert stdout['iteration'] is None
    assert stdout['content'] == 'legacy output\n'
    assert stdout['legacy'] is True


@pytest.mark.parametrize('escape_kind', ['mismatched-run', 'symlink'])
def test_logs_rejects_unsafe_invocation_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    escape_kind: str,
) -> None:
    """Reject mismatched run identity and symlink escapes."""

    database, run, run_directory = create_run(tmp_path)
    add_invocation(run, run_directory, invocation_id='one')
    manifest = run_directory / 'invocations/one.json'
    if escape_kind == 'mismatched-run':
        document = json.loads(manifest.read_text())
        document['run_id'] = 'another-run'
        manifest.write_text(json.dumps(document))
    else:
        outside = tmp_path / 'outside.log'
        outside.write_text('private\n')
        stdout = run_directory / 'logs/one.stdout.log'
        stdout.unlink()
        stdout.symlink_to(outside)

    assert main(logs_arguments(database, run, run_directory)) == 2

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    assert document['streams'] == []
    assert document['failures'] == []
    assert document['error']['code'] == 'invalid_evidence'


def test_logs_returns_json_when_no_stream_matches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a structured command error when filters select no streams."""

    database, run, run_directory = create_run(tmp_path)
    add_invocation(run, run_directory, invocation_id='one')
    arguments = logs_arguments(database, run, run_directory)
    arguments.extend(['--iteration', '2'])

    assert main(arguments) == 2

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    assert document['run_id'] == str(run.id)
    assert document['streams'] == []
    assert document['failures'] == []
    assert document['error'] == {
        'code': 'no_matching_logs',
        'message': 'no matching logs found',
    }


def test_logs_returns_json_when_run_evidence_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a structured command error when a run has no evidence directory."""

    database, run, run_directory = create_run(tmp_path)
    run_directory.rmdir()

    assert main(logs_arguments(database, run, run_directory)) == 2

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    assert document['streams'] == []
    assert document['failures'] == []
    assert document['error']['code'] == 'run_evidence_not_found'


def test_logs_returns_json_when_database_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a structured command error without creating a missing database."""

    database = tmp_path / 'missing.db'

    assert main(['--database', str(database), 'logs', 'unknown-run']) == 2

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    assert document == {
        'schema_version': 4,
        'run_id': 'unknown-run',
        'streams': [],
        'failures': [],
        'error': {
            'code': 'state_database_not_found',
            'message': f'state database not found: {database}',
        },
    }
    assert not database.exists()


def test_logs_returns_json_when_run_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a structured command error for an unknown run ID."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()

    assert main(['--database', str(database), 'logs', 'unknown-run']) == 2

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    assert document['run_id'] == 'unknown-run'
    assert document['streams'] == []
    assert document['failures'] == []
    assert document['error']['code'] == 'run_not_found'


def test_logs_rejects_legacy_metadata_filter_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Represent an unsupported legacy metadata filter as a JSON error."""

    database, run, run_directory = create_run(tmp_path)
    logs = run_directory / 'logs'
    logs.mkdir()
    (logs / '000001-reviewer.stdout.log').write_text('legacy output\n')
    arguments = logs_arguments(database, run, run_directory)
    arguments.extend(['--runtime', 'codex'])

    assert main(arguments) == 2

    captured = capsys.readouterr()
    document = json.loads(captured.out)
    assert captured.err == ''
    assert document['streams'] == []
    assert document['failures'] == []
    assert document['error']['code'] == 'legacy_metadata_unavailable'


def test_logs_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Leave database and evidence bytes unchanged after viewing logs."""

    database, run, run_directory = create_run(tmp_path)
    add_invocation(run, run_directory, invocation_id='one')
    before_database = database.read_bytes()
    before_files = {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob('*')
        if path.is_file()
    }

    assert main(logs_arguments(database, run, run_directory)) == 0
    capsys.readouterr()

    assert database.read_bytes() == before_database
    assert before_files == {
        path.relative_to(run_directory): path.read_bytes()
        for path in run_directory.rglob('*')
        if path.is_file()
    }
