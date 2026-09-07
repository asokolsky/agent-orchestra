"""Tests for SQLite run persistence."""

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from agent_orchestra.models import IssueJob, Run, RunState, ScenarioType
from agent_orchestra.store import ConcurrentUpdateError, RunNotFoundError, RunStore
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from pathlib import Path


def test_run_round_trip(tmp_path: Path) -> None:
    """Persist and load all initial run attributes."""

    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = replace(
        Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest'),
        remote_url='https://example.test/pull/1',
    )
    store.add(run)

    assert store.get(run.id) == run


def test_run_round_trip_preserves_superseded_lineage(tmp_path: Path) -> None:
    """Persist the run ID replaced by a newly captured run."""

    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(
        tmp_path,
        tmp_path,
        'base',
        'head',
        'digest',
        supersedes_run_id='prior-run',
    )

    store.add(run)

    assert store.get(run.id).supersedes_run_id == 'prior-run'


def test_initialize_adds_lineage_column_to_legacy_database(tmp_path: Path) -> None:
    """Migrate an existing runs table without invalidating its rows."""

    database = tmp_path / 'state.db'
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, scenario TEXT NOT NULL,
                repository_path TEXT NOT NULL, worktree_path TEXT NOT NULL,
                state TEXT NOT NULL, base_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
                diff_digest TEXT, iteration INTEGER NOT NULL, remote_url TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )

    RunStore(database).initialize()

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(runs)')}
    assert 'supersedes_run_id' in columns


def test_read_legacy_run_without_lineage_column(tmp_path: Path) -> None:
    """Read old state without requiring a write-time schema migration."""

    database = tmp_path / 'state.db'
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, scenario TEXT NOT NULL,
                repository_path TEXT NOT NULL, worktree_path TEXT NOT NULL,
                state TEXT NOT NULL, base_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
                diff_digest TEXT, iteration INTEGER NOT NULL, remote_url TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.id,
                run.scenario,
                str(run.repo_path),
                str(run.worktree_path),
                run.state,
                run.base_sha,
                run.head_sha,
                run.diff_digest,
                run.iteration,
                run.remote_url,
                run.created_at.isoformat(),
                run.updated_at.isoformat(),
            ),
        )

    loaded = RunStore(database).get(run.id)

    assert loaded.supersedes_run_id is None
    assert loaded.id == run.id


def test_update_records_new_state(tmp_path: Path) -> None:
    """Persist a valid state transition."""

    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)

    updated = transition(run, RunState.PREPARING)
    store.update(updated, expected_state=RunState.QUEUED)

    assert store.get(run.id).state is RunState.PREPARING
    assert [item.to_state for item in store.list_transitions(run.id)] == [
        RunState.QUEUED,
        RunState.PREPARING,
    ]
    assert all(item.scope_digest == 'digest' for item in store.list_transitions(run.id))


def test_issue_job_records_ordered_transitions_with_source_digest(
    tmp_path: Path,
) -> None:
    """Record issue-job creation and updates with immutable scope correlation."""

    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    job = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url='https://github.com/acme/widgets/issues/12',
        namespace='acme',
        project='widgets',
        issue_number=12,
        title='Describe feature',
        author='author',
        source_updated_at='2026-01-02T00:00:00Z',
        source_digest='sha256:' + '1' * 64,
    )
    store.add_issue(job)
    reviewing = replace(job, state=RunState.REVIEWING, iteration=1)
    store.update_issue(reviewing, RunState.QUEUED)
    finished = replace(reviewing, state=RunState.CHANGES_REQUESTED)
    store.update_issue(finished, RunState.REVIEWING)

    transitions = store.list_transitions(job.id)

    assert [item.from_state for item in transitions] == [
        None,
        RunState.QUEUED,
        RunState.REVIEWING,
    ]
    assert [item.to_state for item in transitions] == [
        RunState.QUEUED,
        RunState.REVIEWING,
        RunState.CHANGES_REQUESTED,
    ]
    assert all(item.scenario is ScenarioType.ISSUE_REVIEW for item in transitions)
    assert all(item.scope_digest == job.source_digest for item in transitions)


def test_list_transitions_does_not_create_an_absent_database(tmp_path: Path) -> None:
    """Return empty history without creating storage from a read path."""

    database = tmp_path / 'state.db'

    assert RunStore(database).list_transitions('missing-job') == ()
    assert not database.exists()


def test_list_transitions_handles_an_absent_table_without_mutation(
    tmp_path: Path,
) -> None:
    """Return empty history when an existing database predates transition storage."""

    database = tmp_path / 'state.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE placeholder (id INTEGER PRIMARY KEY)')
    before = database.read_bytes()

    assert RunStore(database).list_transitions('missing-job') == ()
    assert database.read_bytes() == before


def test_list_transitions_reads_run_only_schema_without_migrating(
    tmp_path: Path,
) -> None:
    """Read existing run history without changing its schema."""

    database = tmp_path / 'state.db'
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (id TEXT PRIMARY KEY, scenario TEXT NOT NULL);
            CREATE TABLE transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                from_state TEXT, to_state TEXT NOT NULL, occurred_at TEXT NOT NULL
            );
            """
        )
        connection.execute('INSERT INTO runs VALUES (?, ?)', (run.id, run.scenario))
        connection.execute(
            'INSERT INTO transitions VALUES (?, ?, ?, ?, ?)',
            (1, run.id, None, run.state, run.created_at.isoformat()),
        )

    history = RunStore(database).list_transitions(run.id)

    assert len(history) == 1
    assert history[0].scenario is ScenarioType.LOCAL_CHANGES
    assert history[0].scope_digest is None
    with sqlite3.connect(database) as connection:
        columns = {
            row[1] for row in connection.execute('PRAGMA table_info(transitions)')
        }
    assert 'run_id' in columns
    assert 'job_id' not in columns


def test_source_code_transition_allows_an_unknown_digest(tmp_path: Path) -> None:
    """Preserve an explicitly unknown run digest in transition history."""

    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = replace(
        Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest'),
        diff_digest=None,
    )

    store.add(run)

    assert store.list_transitions(run.id)[0].scope_digest is None


def test_initialize_migrates_run_transitions_without_inventing_digest(
    tmp_path: Path,
) -> None:
    """Preserve old transition order while leaving historical scope unknown."""

    database = tmp_path / 'state.db'
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE runs (
                id TEXT PRIMARY KEY, scenario TEXT NOT NULL,
                repository_path TEXT NOT NULL, worktree_path TEXT NOT NULL,
                state TEXT NOT NULL, base_sha TEXT NOT NULL, head_sha TEXT NOT NULL,
                diff_digest TEXT, iteration INTEGER NOT NULL, remote_url TEXT,
                supersedes_run_id TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES runs(id), from_state TEXT,
                to_state TEXT NOT NULL, occurred_at TEXT NOT NULL
            );
            """
        )
        connection.execute(
            'INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            RunStore._values(run),
        )
        connection.execute(
            'INSERT INTO transitions VALUES (?, ?, ?, ?, ?)',
            (7, run.id, None, run.state, run.created_at.isoformat()),
        )
        connection.execute(
            'INSERT INTO transitions VALUES (?, ?, ?, ?, ?)',
            (8, 'orphan-run', None, run.state, run.created_at.isoformat()),
        )

    store = RunStore(database)
    store.initialize()

    transition_history = store.list_transitions(run.id)
    assert len(transition_history) == 1
    assert transition_history[0].scenario is ScenarioType.LOCAL_CHANGES
    assert transition_history[0].scope_digest is None
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            'SELECT id, job_id, scenario FROM transitions ORDER BY id'
        ).fetchall()
    assert rows == [
        (7, run.id, ScenarioType.LOCAL_CHANGES),
        (8, 'orphan-run', ScenarioType.LOCAL_CHANGES),
    ]


def test_initialize_resumes_an_interrupted_transition_migration(
    tmp_path: Path,
) -> None:
    """Recover the old table when initialization stopped after its rename."""

    database = tmp_path / 'state.db'
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store = RunStore(database)
    store.initialize()
    store.add(run)
    with sqlite3.connect(database) as connection:
        connection.execute('ALTER TABLE transitions RENAME TO run_transitions')
        connection.execute(
            'ALTER TABLE run_transitions RENAME COLUMN job_id TO run_id'
        )
        connection.execute(
            """CREATE TABLE transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                scenario TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
                scope_digest TEXT, occurred_at TEXT NOT NULL
            )"""
        )

    store.initialize()

    assert [item.to_state for item in store.list_transitions(run.id)] == [
        RunState.QUEUED
    ]
    with sqlite3.connect(database) as connection:
        backup = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'run_transitions'"
        ).fetchone()
    assert backup is None


def test_initialize_renames_legacy_awaiting_review_state(tmp_path: Path) -> None:
    """Keep databases created before the reviewing state rename readable."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET state = 'awaiting_review' WHERE id = ?", (str(run.id),)
        )
        connection.execute(
            "UPDATE transitions SET to_state = 'awaiting_review' WHERE job_id = ?",
            (str(run.id),),
        )
        connection.execute(
            """INSERT INTO transitions (
                job_id, scenario, from_state, to_state, scope_digest, occurred_at
            ) VALUES (?, ?, 'awaiting_review', 'approved', ?, ?)""",
            (
                str(run.id),
                run.scenario,
                run.diff_digest,
                run.updated_at.isoformat(),
            ),
        )

    store.initialize()

    assert store.get(run.id).state is RunState.REVIEWING
    with sqlite3.connect(database) as connection:
        legacy_values = connection.execute(
            'SELECT COUNT(*) FROM transitions '
            "WHERE from_state = 'awaiting_review' OR to_state = 'awaiting_review'"
        ).fetchone()
    assert legacy_values == (0,)


def test_read_and_update_accept_legacy_awaiting_review_state(tmp_path: Path) -> None:
    """Use an in-progress legacy run without requiring initialization."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    run = replace(
        Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest'),
        state=RunState.REVIEWING,
        iteration=1,
    )
    store.add(run)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET state = 'awaiting_review' WHERE id = ?", (str(run.id),)
        )

    loaded = store.get(run.id)
    approved = transition(loaded, RunState.APPROVED)
    store.update(approved, expected_state=RunState.REVIEWING)

    assert loaded.state is RunState.REVIEWING
    assert store.get(run.id).state is RunState.APPROVED


def test_update_detects_stale_state(tmp_path: Path) -> None:
    """Reject an update whose expected state is no longer current."""

    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)
    updated = transition(run, RunState.PREPARING)
    store.update(updated, expected_state=RunState.QUEUED)

    with pytest.raises(ConcurrentUpdateError):
        store.update(updated, expected_state=RunState.QUEUED)


def test_update_distinguishes_missing_run(tmp_path: Path) -> None:
    """Report a missing run separately from a stale state."""

    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    updated = transition(run, RunState.PREPARING)

    with pytest.raises(RunNotFoundError):
        store.update(updated, expected_state=RunState.QUEUED)


def test_update_does_not_rewrite_creation_time(tmp_path: Path) -> None:
    """Keep persisted creation history immutable during updates."""

    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)
    changed_history = replace(
        transition(run, RunState.PREPARING),
        created_at=datetime(2000, 1, 1, tzinfo=UTC),
    )

    store.update(changed_history, expected_state=RunState.QUEUED)

    assert store.get(run.id).created_at == run.created_at


def test_public_operations_close_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close every short-lived SQLite connection deterministically."""

    connections: list[sqlite3.Connection] = []
    original_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        """SQLite connection retained by the test after closure."""

    def tracking_connect(database: Path) -> sqlite3.Connection:
        connection = original_connect(database, factory=TrackingConnection)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, 'connect', tracking_connect)
    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)
    store.get(run.id)
    store.list_runs()
    store.update(transition(run, RunState.PREPARING), RunState.QUEUED)

    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')
