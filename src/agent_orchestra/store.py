"""SQLite persistence for orchestration runs and issue-review jobs."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from agent_orchestra.models import (
    TERMINAL_STATES,
    IssueJob,
    JobTransition,
    ProviderAction,
    Run,
    RunState,
    ScenarioType,
    utc_now,
)

if TYPE_CHECKING:
    from collections.abc import Callable

LEGACY_REVIEW_STATE = 'awaiting_review'


class PersistedEnumError(ValueError):
    """Describe one enum value that this installation cannot interpret."""

    def __init__(self, job_id: str, field: str, value: str) -> None:
        """Create a stable persisted-value diagnostic."""

        self.job_id = job_id
        self.field = field
        self.value = value
        self.code = f'unknown_job_{field}'
        super().__init__(f'unrecognized persisted {field} for job {job_id}: {value}')


@dataclass(frozen=True, slots=True)
class UnreadableJob:
    """Retain a list entry whose persisted job row cannot be decoded."""

    job_id: str
    created_at: str
    error: PersistedEnumError


def _decode_enum[EnumT: StrEnum](
    enum_type: type[EnumT],
    value: str,
    *,
    job_id: str,
    field: str,
    normalize_legacy_state: bool = False,
) -> EnumT:
    """Decode one persisted enum value with a stable domain error."""

    normalized = (
        str(RunState.REVIEWING)
        if normalize_legacy_state and value == LEGACY_REVIEW_STATE
        else value
    )
    try:
        return enum_type(normalized)
    except ValueError:
        raise PersistedEnumError(job_id, field, value) from None


def _decode_transition_enum[EnumT: StrEnum](
    enum_type: type[EnumT],
    value: str,
    *,
    job_id: str,
    column: str,
    unrecognized: list[str],
) -> EnumT | str:
    """Decode a transition value while retaining unknown raw text."""

    try:
        return _decode_enum(
            enum_type,
            value,
            job_id=job_id,
            field='scenario' if column == 'scenario' else 'state',
            normalize_legacy_state=column != 'scenario',
        )
    except PersistedEnumError:
        unrecognized.append(column)
        return value


class RunNotFoundError(LookupError):
    """Raised when a requested run does not exist."""


class ConcurrentUpdateError(RuntimeError):
    """Raised when persisted state changed before an update completed."""


class JobStore:
    """Persist and retrieve source-code runs and issue jobs from SQLite."""

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
                    supersedes_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    scenario TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    scope_digest TEXT,
                    reason TEXT,
                    occurred_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS issue_jobs (
                    id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    host TEXT NOT NULL,
                    remote_url TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    project TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT NOT NULL,
                    source_updated_at TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS issue_actions (
                    job_id TEXT NOT NULL REFERENCES issue_jobs(id),
                    iteration INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    remote_url TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, iteration, action)
                );
                """
            )
            self._migrate_transitions(connection)
            transition_columns = {
                row['name']
                for row in connection.execute('PRAGMA table_info(transitions)')
            }
            if 'reason' not in transition_columns:
                connection.execute('ALTER TABLE transitions ADD COLUMN reason TEXT')
            connection.execute(
                'CREATE INDEX IF NOT EXISTS transitions_job_id_id '
                'ON transitions(job_id, id)'
            )
            columns = {
                row['name']
                for row in connection.execute('PRAGMA table_info(runs)').fetchall()
            }
            if 'supersedes_run_id' not in columns:
                connection.execute('ALTER TABLE runs ADD COLUMN supersedes_run_id TEXT')
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

    def add_issue(self, job: IssueJob) -> None:
        """Insert one newly captured issue-review job."""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO issue_jobs (
                    id, state, provider, host, remote_url, namespace, project,
                    issue_number, title, author, source_updated_at, source_digest,
                    iteration, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.id,
                    job.state,
                    job.provider,
                    job.host,
                    job.remote_url,
                    job.namespace,
                    job.project,
                    job.issue_number,
                    job.title,
                    job.author,
                    job.source_updated_at,
                    job.source_digest,
                    job.iteration,
                    job.created_at.isoformat(),
                    job.updated_at.isoformat(),
                ),
            )
            self._add_transition(
                connection,
                job_id=job.id,
                scenario=ScenarioType.ISSUE_REVIEW,
                from_state=None,
                to_state=job.state,
                scope_digest=job.source_digest,
                occurred_at=job.created_at,
            )

    def get_issue(self, job_id: str) -> IssueJob:
        """Return one issue-review job by identifier."""

        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    'SELECT * FROM issue_jobs WHERE id = ?', (job_id,)
                ).fetchone()
        except sqlite3.OperationalError as error:
            if 'no such table: issue_jobs' not in str(error):
                raise
            raise RunNotFoundError(job_id) from error
        if row is None:
            raise RunNotFoundError(job_id)
        return self._issue_from_row(row)

    def list_issues(self) -> tuple[IssueJob, ...]:
        """Return issue-review jobs ordered newest first."""

        try:
            with closing(self._connect()) as connection, connection:
                rows = connection.execute(
                    'SELECT * FROM issue_jobs ORDER BY created_at DESC'
                ).fetchall()
        except sqlite3.OperationalError as error:
            if 'no such table: issue_jobs' not in str(error):
                raise
            return ()
        return tuple(self._issue_from_row(row) for row in rows)

    def list_issues_with_errors(self) -> tuple[IssueJob | UnreadableJob, ...]:
        """Return issue jobs while retaining rows with unknown enum values."""

        try:
            with closing(self._connect()) as connection, connection:
                rows = connection.execute(
                    'SELECT * FROM issue_jobs ORDER BY created_at DESC'
                ).fetchall()
        except sqlite3.OperationalError as error:
            if 'no such table: issue_jobs' not in str(error):
                raise
            return ()
        return tuple(self._decode_job_row(row, self._issue_from_row) for row in rows)

    def update_issue(self, job: IssueJob, expected_state: RunState) -> None:
        """Persist an issue-review job using compare-and-set semantics."""

        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE issue_jobs SET state = ?, source_updated_at = ?,
                    source_digest = ?, title = ?, author = ?, iteration = ?,
                    updated_at = ?
                WHERE id = ? AND state = ?
                """,
                (
                    job.state,
                    job.source_updated_at,
                    job.source_digest,
                    job.title,
                    job.author,
                    job.iteration,
                    job.updated_at.isoformat(),
                    job.id,
                    expected_state,
                ),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdateError(job.id)
            self._add_transition(
                connection,
                job_id=job.id,
                scenario=ScenarioType.ISSUE_REVIEW,
                from_state=expected_state,
                to_state=job.state,
                scope_digest=job.source_digest,
                occurred_at=job.updated_at,
            )

    def get_issue_action(
        self, job_id: str, iteration: int, action: str
    ) -> ProviderAction | None:
        """Return a previously completed provider action, when present."""

        try:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    """SELECT * FROM issue_actions
                    WHERE job_id = ? AND iteration = ? AND action = ?""",
                    (job_id, iteration, action),
                ).fetchone()
        except sqlite3.OperationalError as error:
            if 'no such table: issue_actions' not in str(error):
                raise
            raise RunNotFoundError(job_id) from error
        if row is None:
            return None
        return ProviderAction(
            job_id=row['job_id'],
            iteration=row['iteration'],
            action=row['action'],
            provider_id=row['provider_id'],
            remote_url=row['remote_url'],
            created_at=datetime.fromisoformat(row['created_at']),
        )

    def add_issue_action(self, action: ProviderAction) -> None:
        """Persist one idempotent provider-action identity."""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT OR IGNORE INTO issue_actions (
                    job_id, iteration, action, provider_id, remote_url, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    action.job_id,
                    action.iteration,
                    action.action,
                    action.provider_id,
                    action.remote_url,
                    action.created_at.isoformat(),
                ),
            )

    def list_issue_actions(self, job_id: str) -> tuple[ProviderAction, ...]:
        """Return provider actions for one job in creation order."""

        try:
            with closing(self._connect()) as connection, connection:
                rows = connection.execute(
                    'SELECT * FROM issue_actions WHERE job_id = ? ORDER BY created_at',
                    (job_id,),
                ).fetchall()
        except sqlite3.OperationalError as error:
            if 'no such table: issue_actions' not in str(error):
                raise
            return ()
        return tuple(
            ProviderAction(
                job_id=row['job_id'],
                iteration=row['iteration'],
                action=row['action'],
                provider_id=row['provider_id'],
                remote_url=row['remote_url'],
                created_at=datetime.fromisoformat(row['created_at']),
            )
            for row in rows
        )

    def add(self, run: Run) -> None:
        """Insert a newly created run and its initial transition record."""

        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO runs (
                    id, scenario, repository_path, worktree_path, state, base_sha,
                    head_sha, diff_digest, iteration, remote_url, supersedes_run_id,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._values(run),
            )
            self._add_transition(
                connection,
                job_id=str(run.id),
                scenario=run.scenario,
                from_state=None,
                to_state=run.state,
                scope_digest=run.diff_digest,
                occurred_at=run.created_at,
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

    def list_runs_with_errors(self) -> tuple[Run | UnreadableJob, ...]:
        """Return runs while retaining rows with unknown enum values."""

        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                'SELECT * FROM runs ORDER BY created_at DESC'
            ).fetchall()
        return tuple(self._decode_job_row(row, self._from_row) for row in rows)

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
                    remote_url = ?, supersedes_run_id = ?, updated_at = ?
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
                    run.supersedes_run_id,
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
            self._add_transition(
                connection,
                job_id=str(run.id),
                scenario=run.scenario,
                from_state=expected_state,
                to_state=run.state,
                scope_digest=run.diff_digest,
                occurred_at=run.updated_at,
            )

    def cancel(self, run_id: str, reason: str) -> Run:
        """Atomically cancel one non-terminal source-code job with a reason."""

        if not reason.strip():
            message = 'cancellation reason must not be empty'
            raise ValueError(message)
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                'SELECT * FROM runs WHERE id = ?', (run_id,)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(run_id)
            run = self._from_row(row)
            if run.state in TERMINAL_STATES:
                raise ValueError(f'job is already terminal: {run.state}')
            cancelled = replace(run, state=RunState.CANCELLED, updated_at=utc_now())
            cursor = connection.execute(
                'UPDATE runs SET state = ?, updated_at = ? WHERE id = ? AND state = ?',
                (cancelled.state, cancelled.updated_at.isoformat(), run_id, run.state),
            )
            if cursor.rowcount != 1:
                raise ConcurrentUpdateError(run_id)
            self._add_transition(
                connection,
                job_id=run_id,
                scenario=run.scenario,
                from_state=run.state,
                to_state=RunState.CANCELLED,
                scope_digest=run.diff_digest,
                occurred_at=cancelled.updated_at,
                reason=reason.strip(),
            )
        return cancelled

    def list_transitions(self, job_id: str) -> tuple[JobTransition, ...]:
        """Return one job's state transitions in persistent order."""

        if not self.database_path.is_file():
            return ()
        with closing(self._connect_read_only()) as connection:
            columns = {
                row['name']
                for row in connection.execute(
                    'PRAGMA table_info(transitions)'
                ).fetchall()
            }
            if not columns:
                return ()
            if 'run_id' in columns:
                rows = connection.execute(
                    """SELECT transitions.run_id AS job_id,
                        COALESCE(runs.scenario, ?) AS scenario,
                        transitions.from_state, transitions.to_state,
                        NULL AS scope_digest, transitions.occurred_at
                    FROM transitions
                    LEFT JOIN runs ON runs.id = transitions.run_id
                    WHERE transitions.run_id = ?
                    ORDER BY transitions.id""",
                    (ScenarioType.LOCAL_CHANGES, job_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    'SELECT * FROM transitions WHERE job_id = ? ORDER BY id',
                    (job_id,),
                ).fetchall()
        transitions: list[JobTransition] = []
        for row in rows:
            unrecognized: list[str] = []
            try:
                reason = row['reason']
            except IndexError:
                reason = None
            scenario = _decode_transition_enum(
                ScenarioType,
                row['scenario'],
                job_id=row['job_id'],
                column='scenario',
                unrecognized=unrecognized,
            )
            from_state = (
                _decode_transition_enum(
                    RunState,
                    row['from_state'],
                    job_id=row['job_id'],
                    column='from_state',
                    unrecognized=unrecognized,
                )
                if row['from_state'] is not None
                else None
            )
            to_state = _decode_transition_enum(
                RunState,
                row['to_state'],
                job_id=row['job_id'],
                column='to_state',
                unrecognized=unrecognized,
            )
            transitions.append(
                JobTransition(
                    job_id=row['job_id'],
                    scenario=scenario,
                    from_state=from_state,
                    to_state=to_state,
                    scope_digest=row['scope_digest'],
                    occurred_at=datetime.fromisoformat(row['occurred_at']),
                    reason=reason,
                    unrecognized_fields=tuple(unrecognized),
                )
            )
        return tuple(transitions)

    def interrupted_origin(self, run_id: str) -> RunState:
        """Return the active state from which a run was interrupted."""

        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """
                SELECT from_state FROM transitions
                WHERE job_id = ? AND to_state = ?
                ORDER BY id DESC LIMIT 1
                """,
                (str(run_id), RunState.INTERRUPTED),
            ).fetchone()
        if row is None or row['from_state'] is None:
            raise RunNotFoundError(f'interruption transition for {run_id}')
        return _decode_enum(
            RunState,
            row['from_state'],
            job_id=run_id,
            field='state',
            normalize_legacy_state=True,
        )

    @staticmethod
    def _add_transition(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        scenario: ScenarioType,
        from_state: RunState | None,
        to_state: RunState,
        scope_digest: str | None,
        occurred_at: datetime,
        reason: str | None = None,
    ) -> None:
        """Insert one transition with its immutable scope correlation."""

        connection.execute(
            """INSERT INTO transitions (
                job_id, scenario, from_state, to_state, scope_digest, reason, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                scenario,
                from_state,
                to_state,
                scope_digest,
                reason,
                occurred_at.isoformat(),
            ),
        )

    @staticmethod
    def _migrate_transitions(connection: sqlite3.Connection) -> None:
        """Migrate run-only transitions atomically and resume interrupted work."""

        tables = {
            row['name']
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        columns = {
            row['name']
            for row in connection.execute('PRAGMA table_info(transitions)').fetchall()
        }
        has_backup = 'run_transitions' in tables
        if not has_backup and 'run_id' not in columns:
            return
        connection.execute('SAVEPOINT migrate_transitions')
        try:
            if not has_backup:
                connection.execute('ALTER TABLE transitions RENAME TO run_transitions')
                connection.execute(
                    """CREATE TABLE transitions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        scenario TEXT NOT NULL,
                        from_state TEXT,
                        to_state TEXT NOT NULL,
                        scope_digest TEXT,
                        occurred_at TEXT NOT NULL
                    )"""
                )
            connection.execute(
                """INSERT OR IGNORE INTO transitions (
                id, job_id, scenario, from_state, to_state, scope_digest,
                occurred_at
                )
                SELECT transitions.id, transitions.run_id,
                    COALESCE(runs.scenario, ?), transitions.from_state,
                    transitions.to_state, NULL, transitions.occurred_at
                FROM run_transitions AS transitions
                LEFT JOIN runs ON runs.id = transitions.run_id
                ORDER BY transitions.id""",
                (ScenarioType.LOCAL_CHANGES,),
            )
            connection.execute('DROP TABLE run_transitions')
            connection.execute('RELEASE SAVEPOINT migrate_transitions')
        except Exception:
            connection.execute('ROLLBACK TO SAVEPOINT migrate_transitions')
            connection.execute('RELEASE SAVEPOINT migrate_transitions')
            raise

    def _connect(self) -> sqlite3.Connection:
        """Open a configured SQLite connection."""

        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        connection.execute('PRAGMA busy_timeout = 5000')
        return connection

    def _connect_read_only(self) -> sqlite3.Connection:
        """Open the existing database without permitting writes or creation."""

        database_uri = f'{self.database_path.resolve().as_uri()}?mode=ro'
        connection = sqlite3.connect(database_uri, uri=True)
        connection.row_factory = sqlite3.Row
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
            run.supersedes_run_id,
            run.created_at.isoformat(),
            run.updated_at.isoformat(),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> Run:
        """Convert a database row to a domain model."""

        columns = row.keys()
        return Run(
            id=row['id'],
            scenario=_decode_enum(
                ScenarioType,
                row['scenario'],
                job_id=row['id'],
                field='scenario',
            ),
            repo_path=Path(row['repository_path']),
            worktree_path=Path(row['worktree_path']),
            state=_decode_enum(
                RunState,
                row['state'],
                job_id=row['id'],
                field='state',
                normalize_legacy_state=True,
            ),
            base_sha=row['base_sha'],
            head_sha=row['head_sha'],
            diff_digest=row['diff_digest'],
            iteration=row['iteration'],
            remote_url=row['remote_url'],
            supersedes_run_id=(
                row['supersedes_run_id'] if 'supersedes_run_id' in columns else None
            ),
            created_at=datetime.fromisoformat(row['created_at']),
            updated_at=datetime.fromisoformat(row['updated_at']),
        )

    @staticmethod
    def _issue_from_row(row: sqlite3.Row) -> IssueJob:
        """Convert an issue-job row to its domain model."""

        return IssueJob(
            id=row['id'],
            state=_decode_enum(
                RunState,
                row['state'],
                job_id=row['id'],
                field='state',
            ),
            provider=row['provider'],
            host=row['host'],
            remote_url=row['remote_url'],
            namespace=row['namespace'],
            project=row['project'],
            issue_number=row['issue_number'],
            title=row['title'],
            author=row['author'],
            source_updated_at=row['source_updated_at'],
            source_digest=row['source_digest'],
            iteration=row['iteration'],
            created_at=datetime.fromisoformat(row['created_at']),
            updated_at=datetime.fromisoformat(row['updated_at']),
        )

    @staticmethod
    def _decode_job_row[JobT: Run | IssueJob](
        row: sqlite3.Row,
        mapper: Callable[[sqlite3.Row], JobT],
    ) -> JobT | UnreadableJob:
        """Decode a list row or retain its structured enum error."""

        try:
            return mapper(row)
        except PersistedEnumError as error:
            return UnreadableJob(row['id'], row['created_at'], error)
