"""Safe planning and application of persistent job retention."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from agent_orchestra.evidence import (
    EvidencePathError,
    evidence_root_for_job,
    resolve_evidence_path,
)
from agent_orchestra.models import IssueJob, Run, RunState
from agent_orchestra.store import JobStore, UnreadableJob

if TYPE_CHECKING:
    from collections.abc import Iterable

ELIGIBLE_STATES = frozenset(
    {RunState.FAILED, RunState.CANCELLED, RunState.SUPERSEDED, RunState.PUBLISHED}
)
RETENTION_MARKER = '.retention.json'
INTEGRITY_INDEX = '.integrity.json'
REQUIRED_DATABASE_COLUMNS = {
    'runs': {'id', 'state'},
    'transitions': {'job_id', 'to_state', 'occurred_at'},
    'issue_jobs': {'id', 'state'},
    'issue_actions': {'job_id'},
}


class RetentionError(RuntimeError):
    """Raised when a safe prune plan cannot be established or applied."""


@dataclass(frozen=True, slots=True)
class PruneItem:
    """One selected or skipped persistent job."""

    job_id: str
    category: Literal['job', 'orphan']
    state: str | None
    age_days: int | None
    terminal_at: str | None
    evidence_path: str
    bytes: int
    database_records: dict[str, int]
    action: str
    reason: str


@dataclass(frozen=True, slots=True)
class PrunePlan:
    """Deterministic retention selection against one database and evidence root."""

    database: Path
    runs_directory: Path
    older_than_days: int
    delete_database_records: bool
    selected: tuple[PruneItem, ...]
    skipped: tuple[PruneItem, ...]
    orphans: tuple[PruneItem, ...]
    invalid_paths: tuple[str, ...]


def parse_duration(value: str) -> int:
    """Parse a positive whole-day duration such as ``30d``."""

    message = 'retention duration must be a positive whole-day value'
    if not value.endswith('d') or not value[:-1].isdigit():
        raise RetentionError(message)
    days = int(value[:-1])
    if days < 1:
        raise RetentionError(message)
    return days


def _directory_size(path: Path) -> int:
    """Measure regular files without following any symlink."""

    total = 0
    for root, directories, files in os.walk(path, followlinks=False):
        root_path = Path(root)
        for name in [*directories, *files]:
            candidate = root_path / name
            if candidate.is_symlink():
                raise RetentionError(f'evidence contains a symlink: {candidate}')
        for name in files:
            candidate = root_path / name
            if not candidate.is_file():
                raise RetentionError(
                    f'evidence contains a non-regular file: {candidate}'
                )
            total += candidate.stat().st_size
    return total


def _shard_entries(directory: Path, invalid: list[str]) -> list[Path] | None:
    """Return the sorted contents of a shard, or None when it cannot be read."""

    # A directory can stop being readable between the type check and the walk:
    # permissions change, or the entry is replaced by a file. Report the shard
    # and let the remaining evidence be pruned rather than denying the command.
    try:
        return sorted(directory.iterdir())
    except OSError:
        invalid.append(str(directory))
        return None


def _evidence_directories(
    root: Path,
) -> tuple[tuple[tuple[str, Path], ...], tuple[str, ...]]:
    """Discover contained jobs while reporting malformed candidate paths."""

    if not root.exists():
        return (), ()
    if not root.is_dir() or root.is_symlink():
        raise RetentionError(f'runs directory is not a regular directory: {root}')
    found: list[tuple[str, Path]] = []
    invalid: list[str] = []
    # An unreadable root is different from an unreadable shard: nothing at all
    # can be discovered, so refusing is honest rather than obstructive.
    try:
        root_entries = sorted(root.iterdir())
    except OSError as error:
        raise RetentionError(f'runs directory cannot be read: {root}') from error
    for candidate in root_entries:
        if candidate.is_symlink():
            invalid.append(str(candidate))
            continue
        if candidate.name.isdigit() and len(candidate.name) == 4:
            if not candidate.is_dir():
                # An ordinary file or other non-directory entry named like a year
                # is malformed, not a shard. Report it and keep pruning.
                invalid.append(str(candidate))
                continue
            months = _shard_entries(candidate, invalid)
            if months is None:
                continue
            for month in months:
                if month.is_symlink():
                    invalid.append(str(month))
                    continue
                if (
                    not month.is_dir()
                    or len(month.name) != 2
                    or not month.name.isdigit()
                ):
                    invalid.append(str(month))
                    continue
                days = _shard_entries(month, invalid)
                if days is None:
                    continue
                for day in days:
                    if day.is_symlink():
                        invalid.append(str(day))
                        continue
                    if not day.is_dir() or len(day.name) != 2 or not day.name.isdigit():
                        invalid.append(str(day))
                        continue
                    day_jobs = _shard_entries(day, invalid)
                    if day_jobs is None:
                        continue
                    for job in day_jobs:
                        if job.is_symlink() or not job.is_dir():
                            invalid.append(str(job))
                            continue
                        try:
                            if resolve_evidence_path(root, job.name) == job:
                                found.append((job.name, job))
                            else:
                                invalid.append(str(job))
                        except EvidencePathError:
                            invalid.append(str(job))
            continue
        if candidate.is_dir():
            try:
                if resolve_evidence_path(root, candidate.name) == candidate:
                    found.append((candidate.name, candidate))
                else:
                    invalid.append(str(candidate))
            except EvidencePathError:
                invalid.append(str(candidate))
        else:
            invalid.append(str(candidate))
    return tuple(sorted(found)), tuple(sorted(set(invalid)))


def _record_counts(store: JobStore, job: Run | IssueJob) -> dict[str, int]:
    """Return database rows affected by deleting one job."""

    return {
        'jobs': 1,
        'transitions': len(store.list_transitions(str(job.id))),
        'provider_actions': (
            0 if isinstance(job, Run) else len(store.list_issue_actions(job.id))
        ),
    }


def _validate_database_schema(database: Path) -> None:
    """Require every table used to classify and remove persistent jobs."""

    try:
        with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
            for table, required in REQUIRED_DATABASE_COLUMNS.items():
                columns = {
                    str(row[1])
                    for row in connection.execute(f'PRAGMA table_info({table})')
                }
                if not required.issubset(columns):
                    raise RetentionError(
                        f'state database lacks required {table} schema'
                    )
    except sqlite3.Error as error:
        raise RetentionError(f'cannot read complete state database: {error}') from error


def _validate_evidence_identity(path: Path, job_id: str) -> None:
    """Require a valid integrity or retention document bound to the directory."""

    document_path = path / (
        RETENTION_MARKER if (path / RETENTION_MARKER).is_file() else INTEGRITY_INDEX
    )
    try:
        document = json.loads(document_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise RetentionError(f'invalid evidence identity: {path}') from error
    if (
        not isinstance(document, dict)
        or document.get('schema_version') != 1
        or document.get('job_id') != job_id
        or not isinstance(document.get('entries', document.get('evidence')), list)
    ):
        raise RetentionError(f'invalid evidence identity: {path}')


def build_prune_plan(
    store: JobStore,
    database: Path,
    runs_directory: Path,
    *,
    older_than_days: int,
    include_orphans: bool,
    delete_database_records: bool,
    now: datetime | None = None,
) -> PrunePlan:
    """Build a read-only retention plan, failing closed on ambiguous state."""

    if not database.is_file():
        raise RetentionError(f'state database not found: {database}')
    _validate_database_schema(database)
    try:
        records = [*store.list_runs_with_errors(), *store.list_issues_with_errors()]
    except (OSError, sqlite3.Error) as error:
        raise RetentionError(f'cannot read complete state database: {error}') from error
    if any(isinstance(item, UnreadableJob) for item in records):
        message = 'state database contains an unreadable job row'
        raise RetentionError(message)
    jobs = [item for item in records if isinstance(item, (Run, IssueJob))]
    directories, discovered_invalid = _evidence_directories(
        runs_directory.expanduser().resolve()
    )
    current = (now or datetime.now(UTC)).astimezone(UTC)
    cutoff = current - timedelta(days=older_than_days)
    selected: list[PruneItem] = []
    skipped: list[PruneItem] = []
    invalid_paths = list(discovered_invalid)
    for job in jobs:
        job_id = str(job.id)
        try:
            path = resolve_evidence_path(runs_directory, job_id)
        except EvidencePathError:
            # A stored job whose evidence path cannot be resolved is skipped so
            # one unsafe directory cannot deny pruning every other job. The
            # offending filesystem path is reported by the discovery walk above;
            # recording a second, unresolvable path here would be misleading.
            skipped.append(
                PruneItem(
                    job_id,
                    'job',
                    str(job.state),
                    None,
                    None,
                    '',
                    0,
                    {},
                    'skip',
                    'evidence_path_unsafe',
                )
            )
            continue
        transitions = store.list_transitions(job_id)
        completion = next(
            (
                item.occurred_at
                for item in reversed(transitions)
                if item.to_state is job.state and job.state in ELIGIBLE_STATES
            ),
            None,
        )
        reason = 'eligible'
        if job.state not in ELIGIBLE_STATES:
            reason = 'state_not_eligible'
        elif completion is None:
            reason = 'terminal_transition_missing'
        elif completion > cutoff:
            reason = 'retention_period_not_elapsed'
        elif not path.is_dir():
            reason = 'evidence_missing'
        marker_status: str | None = None
        try:
            if path.is_dir():
                _validate_evidence_identity(path, job_id)
                size = _directory_size(path)
                marker_status = _retention_status(path, job_id)
            else:
                size = 0
        except RetentionError as error:
            size = 0
            reason = str(error)
            invalid_paths.append(str(path))
        if reason == 'eligible' and marker_status == 'completed':
            reason = 'eligible' if delete_database_records else 'already_expired'
        action = (
            'delete_database_records'
            if marker_status == 'completed' and delete_database_records
            else (
                'expire_evidence_and_delete_database_records'
                if delete_database_records
                else 'expire_evidence'
            )
        )
        item = PruneItem(
            job_id,
            'job',
            str(job.state),
            (current - completion).days if completion is not None else None,
            completion.isoformat() if completion is not None else None,
            str(path),
            size,
            _record_counts(store, job),
            action,
            reason,
        )
        (selected if reason == 'eligible' else skipped).append(item)
    known = {str(job.id) for job in jobs}
    unmatched_directories = tuple(
        (job_id, path) for job_id, path in directories if job_id not in known
    )
    orphan_items_list: list[PruneItem] = []
    for job_id, path in unmatched_directories:
        try:
            _validate_evidence_identity(path, job_id)
            size = _directory_size(path)
        except RetentionError:
            invalid_paths.append(str(path))
            continue
        orphan_items_list.append(
            PruneItem(
                job_id,
                'orphan',
                None,
                None,
                None,
                str(path),
                size,
                {'jobs': 0, 'transitions': 0, 'provider_actions': 0},
                'delete_orphan' if include_orphans else 'none',
                'explicitly_selected'
                if include_orphans
                else 'orphan_selection_not_enabled',
            )
        )
    orphan_items = tuple(orphan_items_list)
    if (
        include_orphans
        and unmatched_directories
        and (not jobs or len(unmatched_directories) == len(directories))
    ):
        message = 'orphan pruning refused: database has no matched evidence directories'
        raise RetentionError(message)
    return PrunePlan(
        database,
        runs_directory.expanduser().resolve(),
        older_than_days,
        delete_database_records,
        tuple(sorted(selected, key=lambda item: item.job_id)),
        tuple(sorted(skipped, key=lambda item: item.job_id)),
        orphan_items,
        tuple(sorted(set(invalid_paths))),
    )


def _write_expiry_marker(
    item: PruneItem,
    days: int,
    now: datetime,
    *,
    status: Literal['pending', 'completed'],
    database_cleanup_requested: bool,
) -> None:
    """Atomically record pending or completed intentional evidence expiry."""

    job_directory = Path(item.evidence_path)
    index_path = job_directory / '.integrity.json'
    marker_path = job_directory / RETENTION_MARKER
    if marker_path.is_file():
        try:
            prior = json.loads(marker_path.read_text(encoding='utf-8'))
            evidence = prior['evidence']
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise RetentionError(
                f'cannot validate retention marker for {item.job_id}'
            ) from error
    else:
        try:
            index = json.loads(index_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as error:
            raise RetentionError(
                f'cannot validate integrity index for {item.job_id}'
            ) from error
        if not isinstance(index, dict) or index.get('job_id') != item.job_id:
            raise RetentionError(f'integrity index job ID mismatch for {item.job_id}')
        evidence = index.get('entries', [])
    marker = {
        'schema_version': 1,
        'job_id': item.job_id,
        'status': status,
        'database_cleanup_requested': database_cleanup_requested,
        'expired_at': now.astimezone(UTC).isoformat().replace('+00:00', 'Z'),
        'policy': {'older_than_days': days},
        'evidence': evidence,
    }
    temporary = job_directory / f'.retention.{uuid4()}.tmp'
    temporary.write_text(json.dumps(marker, indent=2) + '\n', encoding='utf-8')
    temporary.replace(job_directory / RETENTION_MARKER)


def _clear_directory(item: PruneItem, *, keep_marker: bool) -> None:
    """Delete a validated directory tree without following symlinks."""

    job_directory = Path(item.evidence_path)
    _validate_evidence_identity(job_directory, item.job_id)
    _directory_size(job_directory)
    paths = sorted(
        job_directory.rglob('*'), key=lambda path: len(path.parts), reverse=True
    )
    for path in paths:
        relative = path.relative_to(job_directory)
        contained = resolve_evidence_path(
            evidence_root_for_job(job_directory),
            item.job_id,
            *relative.parts,
        )
        if contained != path:
            raise RetentionError(f'evidence path mismatch: {path}')
        if keep_marker and relative == Path(RETENTION_MARKER):
            continue
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    if not keep_marker:
        job_directory.rmdir()


def _delete_database_records(connection: sqlite3.Connection, item: PruneItem) -> None:
    """Delete one job and related rows in the active transaction."""

    connection.execute('DELETE FROM issue_actions WHERE job_id = ?', (item.job_id,))
    connection.execute('DELETE FROM transitions WHERE job_id = ?', (item.job_id,))
    connection.execute('DELETE FROM issue_jobs WHERE id = ?', (item.job_id,))
    connection.execute('DELETE FROM runs WHERE id = ?', (item.job_id,))


def _revalidate_item(connection: sqlite3.Connection, item: PruneItem) -> None:
    """Confirm a planned item still has the exact identity and terminal state."""

    rows = [
        *connection.execute('SELECT state FROM runs WHERE id = ?', (item.job_id,)),
        *connection.execute(
            'SELECT state FROM issue_jobs WHERE id = ?', (item.job_id,)
        ),
    ]
    if item.category == 'orphan':
        if rows:
            raise RetentionError(f'orphan now belongs to a job: {item.job_id}')
        return
    if len(rows) != 1 or str(rows[0][0]) != item.state:
        raise RetentionError(f'job state changed after preview: {item.job_id}')
    transition = connection.execute(
        'SELECT occurred_at FROM transitions '
        'WHERE job_id = ? AND to_state = ? ORDER BY id DESC LIMIT 1',
        (item.job_id, item.state),
    ).fetchone()
    if transition is None or str(transition[0]) != item.terminal_at:
        raise RetentionError(f'job transition changed after preview: {item.job_id}')


def _retention_status(job_directory: Path, job_id: str) -> str | None:
    """Return a validated retention marker status when one exists."""

    path = job_directory / RETENTION_MARKER
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise RetentionError(
            f'cannot validate retention marker for {job_id}'
        ) from error
    status = document.get('status') if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get('job_id') != job_id
        or status
        not in {
            'pending',
            'completed',
        }
    ):
        raise RetentionError(f'cannot validate retention marker for {job_id}')
    return str(status)


def apply_prune_plan(plan: PrunePlan) -> tuple[dict[str, str], ...]:
    """Apply exactly one previously built plan with independent outcomes."""

    outcomes: list[dict[str, str]] = []
    now = datetime.now(UTC)
    for item in [
        *plan.selected,
        *(i for i in plan.orphans if i.action == 'delete_orphan'),
    ]:
        try:
            with sqlite3.connect(plan.database) as connection:
                connection.execute('PRAGMA foreign_keys = ON')
                connection.execute('BEGIN IMMEDIATE')
                _revalidate_item(connection, item)
                job_directory = Path(item.evidence_path)
                marker_status = (
                    None
                    if item.category == 'orphan'
                    else _retention_status(job_directory, item.job_id)
                )
                if marker_status != 'completed':
                    lock_path = job_directory / '.integrity.lock'
                    descriptor = os.open(
                        lock_path,
                        os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0),
                        0o600,
                    )
                    with os.fdopen(descriptor, 'a+', encoding='utf-8') as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        if item.category == 'orphan':
                            _clear_directory(item, keep_marker=False)
                        else:
                            _write_expiry_marker(
                                item,
                                plan.older_than_days,
                                now,
                                status='pending',
                                database_cleanup_requested=(
                                    plan.delete_database_records
                                ),
                            )
                            _clear_directory(item, keep_marker=True)
                            _write_expiry_marker(
                                item,
                                plan.older_than_days,
                                now,
                                status='completed',
                                database_cleanup_requested=(
                                    plan.delete_database_records
                                ),
                            )
                if plan.delete_database_records:
                    _delete_database_records(connection, item)
            outcomes.append({'job_id': item.job_id, 'status': 'applied'})
        except (OSError, sqlite3.Error, RetentionError, EvidencePathError) as error:
            outcomes.append(
                {'job_id': item.job_id, 'status': 'failed', 'error': str(error)}
            )
    return tuple(outcomes)


def plan_document(
    plan: PrunePlan, *, applied: bool, outcomes: Iterable[dict[str, str]]
) -> dict[str, object]:
    """Render a versioned prune plan and optional outcomes."""

    return {
        'schema_version': 13,
        'database': str(plan.database),
        'runs_directory': str(plan.runs_directory),
        'older_than_days': plan.older_than_days,
        'mode': 'apply' if applied else 'dry_run',
        'database_cleanup': plan.delete_database_records,
        'selected': [asdict(item) for item in plan.selected],
        'skipped': [asdict(item) for item in plan.skipped],
        'orphans': [asdict(item) for item in plan.orphans],
        'orphan_count': len(plan.orphans),
        'invalid_paths': list(plan.invalid_paths),
        'outcomes': list(outcomes),
        'error': None,
    }
