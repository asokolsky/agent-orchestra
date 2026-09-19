"""Tests for command-line job queries and cancellation."""

import json
import os
import sqlite3
import time
from dataclasses import asdict, fields, replace
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import pytest

from agent_orchestra import (
    cli,
    invocations,
)
from agent_orchestra.adapter.registry import (
    RuntimeRole,
)
from agent_orchestra.cli import (
    build_parser,
    main,
)
from agent_orchestra.evidence import (
    resolve_evidence_path,
)
from agent_orchestra.invocations import (
    InvocationRecord,
)
from agent_orchestra.models import HUMAN_ACTION_STATES, IssueJob, Run, RunState
from agent_orchestra.settings import load_settings
from agent_orchestra.store import JobStore
from tests.cli_helpers import (
    create_worker_run,
    evidence_directory,
    initialize_git_repo,
    run_arguments,
    write_reviewer,
)


def test_jobs_does_not_create_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep job listing read-only when no state database exists."""

    database = tmp_path / 'missing' / 'state.db'

    result = main(['--database', str(database), 'jobs'])

    assert result == 2
    assert 'state database not found' in capsys.readouterr().out
    assert not database.exists()


def test_jobs_rejects_evidence_directory_option(tmp_path: Path) -> None:
    """Do not accept an option that job listing cannot use."""

    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(['jobs', '--runs-directory', str(tmp_path / 'evidence')])


def test_jobs_lists_persisted_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Display a persisted job without changing state."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)

    result = main(['--database', str(database), 'jobs'])

    assert result == 0
    output = capsys.readouterr().out
    assert output.startswith(
        '{\n  "schema_version": 23,\n'
        f'  "agent_orchestra_version": "{version("py-agent-orchestra")}",\n'
        '  "jobs": [\n    {\n'
    )
    assert output.endswith('\n}\n')
    document = json.loads(output)
    expected_fields = {
        'job_id'
        if field.name == 'id'
        else 'repository_path'
        if field.name == 'repo_path'
        else 'supersedes_job_id'
        if field.name == 'supersedes_run_id'
        else field.name
        for field in fields(Run)
    }
    expected_fields.add('worktree_status')
    assert set(document['jobs'][0]) == expected_fields
    assert document == {
        'schema_version': 23,
        'agent_orchestra_version': version('py-agent-orchestra'),
        'jobs': [
            {
                'job_id': str(run.id),
                'scenario': 'local_changes',
                'repository_path': str(tmp_path),
                'worktree_path': str(tmp_path),
                'worktree_status': 'not_git_worktree',
                'state': 'queued',
                'base_sha': 'base',
                'head_sha': 'head',
                'diff_digest': 'digest',
                'iteration': 0,
                'remote_url': None,
                'supersedes_job_id': None,
                'created_at': run.created_at.isoformat().replace('+00:00', 'Z'),
                'updated_at': run.updated_at.isoformat().replace('+00:00', 'Z'),
            }
        ],
        'error': None,
    }


def test_jobs_filters_repeated_states_across_scenarios(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Union repeated durable-state selections for both job scenarios."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)
    issue = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url='https://github.com/acme/widgets/issues/12',
        namespace='acme',
        project='widgets',
        issue_number=12,
        title='Feature',
        author='author',
        source_updated_at='2026-09-08T08:00:00Z',
        source_digest='sha256:' + 'd' * 64,
    )
    store.add_issue(issue)
    published = replace(issue, state=RunState.PUBLISHED)
    store.update_issue(published, RunState.QUEUED)

    result = main(
        [
            '--database',
            str(database),
            'jobs',
            '--state',
            'queued',
            '--state',
            'published',
        ]
    )

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert {job['scenario'] for job in document['jobs']} == {
        'local_changes',
        'issue_review',
    }
    assert {job['state'] for job in document['jobs']} == {'queued', 'published'}

    assert main(['--database', str(database), 'jobs', '--state', 'failed']) == 0
    assert json.loads(capsys.readouterr().out)['jobs'] == []


def test_jobs_attention_selects_exact_human_action_states(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose the shared human-action set and union it with explicit states."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    for state in [*sorted(HUMAN_ACTION_STATES, key=str), RunState.PUBLISHED]:
        run = Run.create_local(repo, repo, 'base', 'head', f'digest-{state}')
        store.add(run)
        store.update(replace(run, state=state), RunState.QUEUED)
    issue = IssueJob.create(
        provider='gitlab',
        host='gitlab.com',
        remote_url='https://gitlab.com/acme/widgets/-/issues/12',
        namespace='acme',
        project='widgets',
        issue_number=12,
        title='Feature',
        author='author',
        source_updated_at='2026-09-08T08:00:00Z',
        source_digest='sha256:' + 'e' * 64,
    )
    store.add_issue(issue)
    interrupted = replace(issue, state=RunState.INTERRUPTED)
    store.update_issue(interrupted, RunState.QUEUED)
    before = database.read_bytes()

    assert main(['--database', str(database), 'jobs', '--attention']) == 0

    attention = json.loads(capsys.readouterr().out)
    assert {job['state'] for job in attention['jobs']} == {
        state.value for state in HUMAN_ACTION_STATES
    }
    assert {job['scenario'] for job in attention['jobs']} == {
        'local_changes',
        'issue_review',
    }
    assert database.read_bytes() == before

    assert (
        main(
            [
                '--database',
                str(database),
                'jobs',
                '--attention',
                '--state',
                'published',
            ]
        )
        == 0
    )
    combined = json.loads(capsys.readouterr().out)
    assert {job['state'] for job in combined['jobs']} == {
        *(state.value for state in HUMAN_ACTION_STATES),
        'published',
    }


def test_jobs_rejects_unknown_state_with_stable_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject a state typo without modifying or consulting durable state."""

    database = tmp_path / 'missing.db'

    result = main(['--database', str(database), 'jobs', '--state', 'needs-coffee'])

    assert result == 2
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 23,
        'agent_orchestra_version': version('py-agent-orchestra'),
        'error': {
            'code': 'invalid_job_state',
            'message': 'unknown durable job state: needs-coffee',
        },
    }
    assert not database.exists()


def test_jobs_reports_broken_worktree_and_excludes_it_from_attention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep unrunnable jobs visible without presenting them as actionable."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    missing = tmp_path / 'missing'
    run = Run.create_local(missing, missing, 'base', 'head', 'digest')
    store.add(run)
    store.update(replace(run, state=RunState.CHANGES_REQUESTED), RunState.QUEUED)
    before = database.read_bytes()

    assert main(['--database', str(database), 'jobs']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['jobs'][0]['worktree_status'] == 'missing'
    assert main(['--database', str(database), 'jobs', '--attention']) == 0
    assert json.loads(capsys.readouterr().out)['jobs'] == []
    assert database.read_bytes() == before

    assert (
        main(
            [
                '--database',
                str(database),
                'jobs',
                '--attention',
                '--state',
                'changes_requested',
            ]
        )
        == 0
    )
    selected = json.loads(capsys.readouterr().out)
    assert [job['job_id'] for job in selected['jobs']] == [str(run.id)]


def test_cancel_records_reason_without_deleting_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cancel an unrunnable job and preserve its durable evidence."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    missing = tmp_path / 'missing'
    run = Run.create_local(missing, missing, 'base', 'head', 'digest')
    store.add(run)
    evidence = tmp_path / 'runs' / str(run.id) / '.integrity.json'
    evidence.parent.mkdir(parents=True)
    evidence.write_text('keep me')
    arguments = [
        '--database',
        str(database),
        'cancel',
        str(run.id),
        '--reason',
        'worktree removed',
    ]

    assert main(arguments) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['state'] == 'cancelled'
    assert evidence.read_text() == 'keep me'
    transition = store.list_transitions(str(run.id))[-1]
    assert transition.to_state is RunState.CANCELLED
    assert transition.reason == 'worktree removed'
    assert main(arguments) == 2
    error = json.loads(capsys.readouterr().out)
    assert error['error']['code'] == 'job_not_cancellable'


def test_cancel_refuses_job_with_available_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require an unrunnable worktree before terminating a source-code job."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repo(repository)
    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    run = Run.create_local(repository, repository, 'base', 'head', 'digest')
    store.add(run)

    assert (
        main(
            [
                '--database',
                str(database),
                'cancel',
                str(run.id),
                '--reason',
                'wrong job',
            ]
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'job_not_cancellable'
    assert store.get(run.id).state is RunState.QUEUED


def test_cancel_reports_unrecognized_persisted_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return the established JSON error when a job state cannot be decoded."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    missing = tmp_path / 'missing'
    run = Run.create_local(missing, missing, 'base', 'head', 'digest')
    store.add(run)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET state = 'future_state' WHERE id = ?", (str(run.id),)
        )

    assert (
        main(['--database', str(database), 'cancel', str(run.id), '--reason', 'stale'])
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['job_id'] == str(run.id)
    assert document['error']['code'] == 'unknown_job_state'


def test_cancel_reports_issue_job_as_not_cancellable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinguish an existing issue-review job from an unknown identifier."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    issue = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url='https://github.com/acme/widgets/issues/12',
        namespace='acme',
        project='widgets',
        issue_number=12,
        title='Feature',
        author='author',
        source_updated_at='2026-09-08T08:00:00Z',
        source_digest='sha256:' + 'a' * 64,
    )
    store.add_issue(issue)

    assert (
        main(
            [
                '--database',
                str(database),
                'cancel',
                issue.id,
                '--reason',
                'wrong scenario',
            ]
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['job_id'] == issue.id
    assert document['error'] == {
        'code': 'job_not_cancellable',
        'message': 'cancellation applies to source-code jobs; '
        'this is an issue-review job',
    }


def test_run_writes_to_identifier_shard_in_non_utc_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep end-to-end placement bound to the ID rather than local time."""

    original_timezone = os.environ.get('TZ')
    try:
        monkeypatch.setenv('TZ', 'Pacific/Kiritimati')
        time.tzset()
        job_id = '20260909T063000Z-b4517e73'
        context = create_worker_run(tmp_path, job_id=job_id)
        reviewer = tmp_path / 'reviewer.py'
        write_reviewer(reviewer, 'approved')

        result = main(run_arguments(context, reviewer=reviewer))
    finally:
        if original_timezone is None:
            monkeypatch.delenv('TZ', raising=False)
        else:
            monkeypatch.setenv('TZ', original_timezone)
        time.tzset()

    assert result == 0
    assert evidence_directory(context) == context.runs_directory / '2026/09/09' / job_id
    assert (evidence_directory(context) / 'execution.json').is_file()
    assert not (context.runs_directory / job_id).exists()


def test_job_selects_one_job_by_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a single job with no current tasks before execution."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    first = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'first')
    second = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'second')
    store.add(first)
    store.add(second)
    runs = tmp_path / 'runs'
    resolve_evidence_path(runs, str(first.id)).mkdir(parents=True)

    result = main(
        [
            '--database',
            str(database),
            'job',
            str(first.id),
            '--runs-directory',
            str(runs),
        ]
    )

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == 23
    assert document['job']['job_id'] == str(first.id)
    assert document['job']['current'] == []


def test_tasks_normalizes_an_invalid_reviewer_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report malformed reviewer invocation evidence as stable JSON."""

    context = create_worker_run(tmp_path)
    job_id = str(context.run.id)
    job_directory = evidence_directory(context)
    invocations_directory = job_directory / 'invocations'
    invocations_directory.mkdir(parents=True)
    reviewer_id = 'bad id!'
    task_id = f'{job_id}:000001-reviewer-{reviewer_id}'
    record = InvocationRecord(
        schema_version=5,
        run_id=job_id,
        task_id=task_id,
        invocation_id=f'{task_id}:attempt-0001',
        role=RuntimeRole.REVIEWER,
        agent_vendor='openai',
        requested_model=None,
        effective_models=(),
        effective_model_status=invocations.EffectiveModelStatus.UNAVAILABLE,
        runtime='codex',
        iteration=1,
        started_at='2026-09-12T08:00:00Z',
        finished_at=None,
        exit_code=None,
        timed_out=False,
        interrupted=False,
        stdout_path='logs/reviewer.stdout.log',
        stderr_path='logs/reviewer.stderr.log',
        attempt=1,
        status=invocations.AttemptStatus.PENDING,
        conclusion=None,
        reviewer_id=reviewer_id,
    )
    document = asdict(record)
    document.pop('usage_status')
    document.pop('usage')
    (invocations_directory / 'reviewer.json').write_text(
        json.dumps(document), encoding='utf-8'
    )

    result = main(
        [
            '--database',
            str(context.database),
            'tasks',
            job_id,
            '--runs-directory',
            str(context.runs_directory),
        ]
    )

    assert result == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error'] == {
        'code': 'invalid_evidence',
        'message': f'invalid reviewer ID: {reviewer_id!r}',
    }


def test_job_reads_persisted_review_state_without_initializing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose the renamed state without requiring a separate init command."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)
    runs = tmp_path / 'runs'
    resolve_evidence_path(runs, str(run.id)).mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET state = 'awaiting_review' WHERE id = ?", (str(run.id),)
        )

    result = main(
        [
            '--database',
            str(database),
            'job',
            str(run.id),
            '--runs-directory',
            str(runs),
        ]
    )

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == 23
    assert document['job']['state'] == 'reviewing'
    with sqlite3.connect(database) as connection:
        stored_state = connection.execute(
            'SELECT state FROM runs WHERE id = ?', (str(run.id),)
        ).fetchone()
    assert stored_state == ('awaiting_review',)


def test_jobs_lists_empty_jobs_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a stable empty collection for an initialized database."""

    database = tmp_path / 'state.db'
    JobStore(database).initialize()

    result = main(['--database', str(database), 'jobs'])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 23,
        'agent_orchestra_version': version('py-agent-orchestra'),
        'jobs': [],
        'error': None,
    }


def test_task_commands_share_resolved_default_runs_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve one default evidence root for every evidence-aware view."""

    database = tmp_path / 'state.db'
    JobStore(database).initialize()
    actual_parent = tmp_path / 'actual'
    actual_parent.mkdir()
    linked_parent = tmp_path / 'linked'
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    monkeypatch.setattr(cli, 'DEFAULT_RUNS_DIRECTORY', linked_parent / 'runs')

    job = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    JobStore(database).add(job)
    resolve_evidence_path(actual_parent / 'runs', str(job.id)).mkdir(parents=True)

    for command, identifier in (
        ('job', str(job.id)),
        ('tasks', str(job.id)),
        ('task', f'{job.id}:000001-reviewer'),
    ):
        arguments = ['--database', str(database), command, identifier]
        result = main(arguments)
        document = json.loads(capsys.readouterr().out)
        assert result == (2 if command == 'task' else 0)
        if command == 'task':
            assert document['error']['code'] == 'task_not_found'


def test_run_and_task_share_default_runs_directory() -> None:
    """Keep evidence producers and consumers on the same default root."""

    parser = build_parser()

    run_args = parser.parse_args(['run', 'run-id', '--objective', 'Review.'])
    task_args = parser.parse_args(['task', 'job-id:000001-reviewer'])

    assert run_args.runs_directory == cli.DEFAULT_RUNS_DIRECTORY
    assert task_args.runs_directory == cli.DEFAULT_RUNS_DIRECTORY


def test_default_runs_directory_is_session_owned(
    isolated_default_runs_directory: Path,
) -> None:
    """Keep the session default off the live root so concurrency cannot leak in."""

    assert isolated_default_runs_directory == cli.DEFAULT_RUNS_DIRECTORY
    live_root = Path('~/.local/state/agent-orchestra/runs').expanduser()
    assert isolated_default_runs_directory != live_root
    assert not isolated_default_runs_directory.is_relative_to(live_root)


def test_unrelated_evidence_activity_does_not_reach_the_session_default(
    isolated_default_runs_directory: Path, tmp_path: Path
) -> None:
    """Ignore job directories another process creates while the session runs."""

    concurrent_root = tmp_path / 'other-process-runs'
    (concurrent_root / '2026' / '09' / '08' / 'job-id').mkdir(parents=True)

    assert not any(isolated_default_runs_directory.rglob('*'))


def test_default_database_is_session_owned(
    isolated_default_database: Path,
) -> None:
    """Keep the session default off the live database in the home directory."""

    assert isolated_default_database == cli.DEFAULT_DATABASE
    live_database = Path('~/.local/state/agent-orchestra/state.db').expanduser()
    assert isolated_default_database != live_database
    assert not isolated_default_database.exists()


def test_command_without_database_reaches_the_session_default(
    isolated_default_database: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the redirect is load-bearing rather than incidentally unused."""

    opened: list[Path] = []

    class RecordingStore:
        """Record the resolved database instead of creating one."""

        def __init__(self, database: Path) -> None:
            opened.append(database)

        def initialize(self) -> None:
            return None

    # `init` is the cheapest command that resolves a database, so running it
    # without --database shows exactly where an omission lands. Recording the
    # path rather than letting the real store create the file keeps this test
    # from writing and then deleting the very file the session guard inspects,
    # which would let an unrelated test's leak pass unnoticed.
    monkeypatch.setattr(cli, 'JobStore', RecordingStore)

    assert main(['init']) == 0
    assert capsys.readouterr().out.strip() == f'initialized {isolated_default_database}'
    assert opened == [isolated_default_database]
    assert not isolated_default_database.exists()


def test_developer_settings_file_cannot_redirect_the_session_defaults(
    isolated_settings_source: Path,
    isolated_default_database: Path,
    isolated_default_runs_directory: Path,
) -> None:
    """Keep a real config.toml from steering the suite at developer state."""

    # Redirecting the module attributes alone would leave both guards open,
    # because settings take precedence over built-in defaults. The suite must
    # find no settings file at all.
    assert Path(os.environ['XDG_CONFIG_HOME']) == isolated_settings_source
    assert not (isolated_settings_source / 'agent-orchestra/config.toml').exists()

    settings = load_settings(
        default_database=cli.DEFAULT_DATABASE,
        default_runs_directory=cli.DEFAULT_RUNS_DIRECTORY,
    )

    assert settings.database.value == isolated_default_database
    assert settings.database.source == 'built_in'
    assert settings.runs_directory.value == isolated_default_runs_directory
    assert settings.runs_directory.source == 'built_in'


def test_job_reports_unknown_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a distinct error when the requested run does not exist."""

    database = tmp_path / 'state.db'
    JobStore(database).initialize()

    result = main(['--database', str(database), 'job', str(uuid4())])

    assert result == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'job_not_found'


@pytest.mark.parametrize(
    ('column', 'value', 'code'),
    [
        ('state', 'future_state', 'unknown_job_state'),
        ('scenario', 'future_scenario', 'unknown_job_scenario'),
    ],
)
def test_read_only_views_report_unrecognized_job_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    column: str,
    value: str,
    code: str,
) -> None:
    """Return stable JSON errors without hiding other readable jobs."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    unreadable = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    readable = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(unreadable)
    store.add(readable)
    with sqlite3.connect(database) as connection:
        connection.execute(
            f'UPDATE runs SET {column} = ? WHERE id = ?',  # noqa: S608
            (value, str(unreadable.id)),
        )

    commands = (
        ['jobs'],
        ['jobs', '--state', 'queued'],
        ['jobs', '--attention'],
        ['job', str(unreadable.id)],
        ['tasks', str(unreadable.id)],
        ['audit', str(unreadable.id)],
    )
    for command in commands:
        assert main(['--database', str(database), *command]) == 2
        document = json.loads(capsys.readouterr().out)
        assert document['schema_version'] == 23
        assert document['error']['code'] == code
        if command[0] == 'jobs':
            listed_ids = {item['job_id'] for item in document['jobs']}
            assert str(unreadable.id) in listed_ids
            if '--attention' not in command:
                assert str(readable.id) in listed_ids
            broken = next(
                item
                for item in document['jobs']
                if item['job_id'] == str(unreadable.id)
            )
            assert broken['error']['code'] == code


@pytest.mark.parametrize('state', ['future_state', 'awaiting_review'])
def test_read_only_views_report_unrecognized_issue_job_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    """Apply the same persisted-state error contract to issue-review jobs."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    issue = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url='https://github.com/acme/widgets/issues/1',
        namespace='acme',
        project='widgets',
        issue_number=1,
        title='Issue',
        author='octocat',
        source_updated_at='2026-09-08T00:00:00Z',
        source_digest='sha256:' + 'a' * 64,
    )
    store.add_issue(issue)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE issue_jobs SET state = ? WHERE id = ?',
            (state, issue.id),
        )

    assert main(['--database', str(database), 'jobs']) == 2
    listing = json.loads(capsys.readouterr().out)
    assert listing['error']['code'] == 'unknown_job_state'
    assert listing['jobs'][0]['job_id'] == issue.id

    for command in ('job', 'tasks', 'audit'):
        assert main(['--database', str(database), command, issue.id]) == 2
        document = json.loads(capsys.readouterr().out)
        assert document['error']['code'] == 'unknown_job_state'
