"""SQLite persistence for orchestration runs."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from agent_orchestra.models import Run, RunState, ScenarioType

LEGACY_REVIEW_STATE = 'awaiting_review'


class RunNotFoundError(LookupError):
    """Raised when a requested run does not exist."""


class ConcurrentUpdateError(RuntimeError):
    """Raised when persisted state changed before an update completed."""


class RunStore:
    """Persist and retrieve runs from a local SQLite database."""

    def __init__(self, database_path: Path) -> None:
        """Create a store for the supplied database path."""

        self.database_path = database_path

    def initialize(self) -> None:
        """Create the database schema if it does not exist."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute('PRAGMA journal_mode = WAL')
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    scenario TEXT NOT NULL,
                    repository_path TEXT NOT NULL,
                    worktree_path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    diff_digest TEXT,
                    iteration INTEGER NOT NULL,
                    remote_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                'UPDATE runs SET state = ? WHERE state = ?',
                (RunState.REVIEWING, LEGACY_REVIEW_STATE),
            )
            connection.execute(
                'UPDATE transitions SET from_state = ? WHERE from_state = ?',
                (RunState.REVIEWING, LEGACY_REVIEW_STATE),
            )
            connection.execute(
                'UPDATE transitions SET to_state = ? WHERE to_state = ?',
                (RunState.REVIEWING, LEGACY_REVIEW_STATE),
            )

    def add(self, run: Run) -> None:
        """Insert a newly created run and its initial transition record."""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, scenario, repository_path, worktree_path, state, base_sha,
                    head_sha, diff_digest, iteration, remote_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._values(run),
            )
            connection.execute(
                'INSERT INTO transitions (run_id, from_state, to_state, occurred_at) '
                'VALUES (?, NULL, ?, ?)',
                (str(run.id), run.state, run.created_at.isoformat()),
            )

    def get(self, run_id: str) -> Run:
        """Return a run by identifier."""

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM runs WHERE id = ?', (str(run_id),)
            ).fetchone()
        if row is None:
            raise RunNotFoundError(str(run_id))
        return self._from_row(row)

    def list_runs(self) -> tuple[Run, ...]:
        """Return runs ordered from newest to oldest."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM runs ORDER BY created_at DESC'
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def update(self, run: Run, expected_state: RunState) -> None:
        """Persist a run when its current stored state matches the expectation."""

        expected_values = (
            (str(expected_state), LEGACY_REVIEW_STATE)
            if expected_state is RunState.REVIEWING
            else (str(expected_state), str(expected_state))
        )
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE runs SET
                    scenario = ?, repository_path = ?, worktree_path = ?, state = ?,
                    base_sha = ?, head_sha = ?, diff_digest = ?, iteration = ?,
                    remote_url = ?, updated_at = ?
                WHERE id = ? AND state IN (?, ?)
                """,
                (
                    run.scenario,
                    str(run.repo_path),
                    str(run.worktree_path),
                    run.state,
                    run.base_sha,
                    run.head_sha,
                    run.diff_digest,
                    run.iteration,
                    run.remote_url,
                    run.updated_at.isoformat(),
                    str(run.id),
                    *expected_values,
                ),
            )
            if cursor.rowcount != 1:
                exists = connection.execute(
                    'SELECT 1 FROM runs WHERE id = ?', (str(run.id),)
                ).fetchone()
                if exists is None:
                    raise RunNotFoundError(str(run.id))
                raise ConcurrentUpdateError(str(run.id))
            connection.execute(
                'INSERT INTO transitions (run_id, from_state, to_state, occurred_at) '
                'VALUES (?, ?, ?, ?)',
                (str(run.id), expected_state, run.state, run.updated_at.isoformat()),
            )

    def _connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection."""

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 5000')
        return connection

    @staticmethod
    def _values(run: Run) -> tuple[object, ...]:
        """Convert a run to database parameter values."""

        return (
            str(run.id),
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
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Run:
        """Convert a database row to a domain model."""

        return Run(
            id=row['id'],
            scenario=ScenarioType(row['scenario']),
            repo_path=Path(row['repository_path']),
            worktree_path=Path(row['worktree_path']),
            state=(
                RunState.REVIEWING
                if row['state'] == LEGACY_REVIEW_STATE
                else RunState(row['state'])
            ),
            base_sha=row['base_sha'],
            head_sha=row['head_sha'],
            diff_digest=row['diff_digest'],
            iteration=row['iteration'],
            remote_url=row['remote_url'],
            created_at=datetime.fromisoformat(row['created_at']),
            updated_at=datetime.fromisoformat(row['updated_at']),
        )
