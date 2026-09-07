"""Contained evidence paths and atomic per-job integrity indexes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

HASH_CHUNK_SIZE = 1024 * 1024
INTEGRITY_INDEX = '.integrity.json'
INTEGRITY_LOCK = '.integrity.lock'
INTEGRITY_PENDING = '.integrity.pending.json'


class EvidencePathError(ValueError):
    """Raised when evidence cannot be contained beneath its selected job."""


@dataclass(frozen=True, slots=True)
class IntegrityEntry:
    """Immutable metadata for one finalized evidence file."""

    job_id: str
    evidence_type: str
    path: str
    size: int
    sha256: str
    finalized_at: str


def resolve_evidence_path(root: Path, job_id: str, *parts: str) -> Path:
    """Return a job-contained path without following symlinked components."""

    evidence_root = root.expanduser().resolve()
    if not job_id or Path(job_id).name != job_id or job_id in {'.', '..'}:
        message = 'invalid evidence job ID'
        raise EvidencePathError(message)
    relative_parts = (job_id, *parts)
    if any(
        not part
        or Path(part).is_absolute()
        or Path(part).parts != (part,)
        or part in {'.', '..'}
        for part in relative_parts
    ):
        message = 'evidence path escapes the selected job'
        raise EvidencePathError(message)
    candidate = evidence_root.joinpath(*relative_parts)
    current = evidence_root
    for part in candidate.relative_to(evidence_root).parts:
        current /= part
        if current.is_symlink():
            message = (
                f'evidence path contains a symlink and escapes containment: {current}'
            )
            raise EvidencePathError(message)
    if not candidate.resolve().is_relative_to(evidence_root / job_id):
        message = 'evidence path escapes the selected job'
        raise EvidencePathError(message)
    return candidate


def record_finalized_evidence(
    root: Path,
    job_id: str,
    path: Path,
    evidence_type: str,
    *,
    finalized_at: datetime | None = None,
    replace_existing: bool = True,
) -> IntegrityEntry:
    """Hash a finalized regular file and atomically update its job index."""

    job_directory = resolve_evidence_path(root, job_id)
    entry = _entry_for_file(root, job_id, path, evidence_type)
    if finalized_at is not None:
        entry = IntegrityEntry(
            **{
                **asdict(entry),
                'finalized_at': finalized_at.isoformat().replace('+00:00', 'Z'),
            }
        )
    job_directory.mkdir(parents=True, exist_ok=True)
    lock_path = resolve_evidence_path(root, job_id, INTEGRITY_LOCK)
    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    try:
        lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    except OSError as error:
        message = 'cannot open integrity lock'
        raise EvidencePathError(message) from error
    with os.fdopen(lock_descriptor, 'a+', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _recover_pending(root, job_id)
        _update_index(root, job_id, entry, replace_existing=replace_existing)
    return entry


def finalize_evidence_write(
    root: Path,
    job_id: str,
    temporary: Path,
    path: Path,
    evidence_type: str,
    *,
    exclusive: bool = False,
) -> IntegrityEntry:
    """Publish a prepared file through a recoverable index transaction."""

    job_directory = resolve_evidence_path(root, job_id)
    relative = _relative_evidence_path(job_directory, path)
    contained = resolve_evidence_path(root, job_id, *relative.parts)
    try:
        temporary_relative = temporary.relative_to(job_directory)
        resolve_evidence_path(root, job_id, *temporary_relative.parts)
    except (EvidencePathError, ValueError) as error:
        message = 'temporary evidence does not belong to the selected job'
        raise EvidencePathError(message) from error
    job_directory.mkdir(parents=True, exist_ok=True)
    lock_path = resolve_evidence_path(root, job_id, INTEGRITY_LOCK)
    lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(lock_path, lock_flags, 0o600)
    except OSError as error:
        message = 'cannot open integrity lock'
        raise EvidencePathError(message) from error
    with os.fdopen(descriptor, 'a+', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _recover_pending(root, job_id)
        _write_pending(root, job_id, relative, evidence_type)
        if exclusive:
            try:
                os.link(temporary, contained)
            except FileExistsError as error:
                resolve_evidence_path(root, job_id, INTEGRITY_PENDING).unlink()
                message = 'finalized evidence already exists'
                raise EvidencePathError(message) from error
        else:
            temporary.replace(contained)
        entry = _entry_for_file(root, job_id, contained, evidence_type)
        _update_index(root, job_id, entry)
        resolve_evidence_path(root, job_id, INTEGRITY_PENDING).unlink()
    return entry


def recover_evidence_index(root: Path, job_id: str) -> None:
    """Reconcile a finalization interrupted after its evidence rename."""

    job_directory = resolve_evidence_path(root, job_id)
    if not job_directory.exists():
        return
    lock_path = resolve_evidence_path(root, job_id, INTEGRITY_LOCK)
    descriptor = os.open(
        lock_path,
        os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0),
        0o600,
    )
    with os.fdopen(descriptor, 'a+', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _recover_pending(root, job_id)


def _relative_evidence_path(job_directory: Path, path: Path) -> Path:
    """Return a path relative to its explicit owning job."""

    candidate = path if path.is_absolute() else job_directory / path
    try:
        return candidate.relative_to(job_directory)
    except ValueError as error:
        message = 'evidence path escapes the selected job'
        raise EvidencePathError(message) from error


def _entry_for_file(
    root: Path, job_id: str, path: Path, evidence_type: str
) -> IntegrityEntry:
    """Hash one contained regular file into an integrity entry."""

    job_directory = resolve_evidence_path(root, job_id)
    relative = _relative_evidence_path(job_directory, path)
    contained = resolve_evidence_path(root, job_id, *relative.parts)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(contained, flags)
    except OSError as error:
        raise EvidencePathError(
            f'cannot read finalized evidence: {contained}'
        ) from error
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, 'rb') as file:
            while chunk := file.read(HASH_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise EvidencePathError(
            f'cannot read finalized evidence: {contained}'
        ) from error
    return IntegrityEntry(
        job_id=job_id,
        evidence_type=evidence_type,
        path=relative.as_posix(),
        size=size,
        sha256=f'sha256:{digest.hexdigest()}',
        finalized_at=datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
    )


def _update_index(
    root: Path,
    job_id: str,
    entry: IntegrityEntry,
    *,
    replace_existing: bool = True,
) -> None:
    """Replace one entry in the integrity index while its lock is held."""

    index_path = resolve_evidence_path(root, job_id, INTEGRITY_INDEX)
    entries: list[object] = []
    if index_path.exists():
        try:
            descriptor = os.open(index_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, encoding='utf-8') as file:
                document = json.load(file)
            entries = list(document['entries'])
        except (OSError, KeyError, TypeError, ValueError) as error:
            message = 'integrity index is malformed'
            raise EvidencePathError(message) from error
        if (
            document.get('schema_version') != 1
            or document.get('job_id') != job_id
            or not _valid_entries(root, job_id, entries)
        ):
            message = 'integrity index is malformed'
            raise EvidencePathError(message)
    serialized = asdict(entry)
    valid_entries = [item for item in entries if isinstance(item, dict)]
    prior = next(
        (item for item in valid_entries if item.get('path') == entry.path), None
    )
    valid_entries = [item for item in valid_entries if item.get('path') != entry.path]
    if prior is not None and (
        not replace_existing
        or all(
            prior.get(field) == serialized[field]
            for field in ('job_id', 'evidence_type', 'path', 'size', 'sha256')
        )
    ):
        valid_entries.append(prior)
    else:
        valid_entries.append(serialized)
    valid_entries.sort(key=lambda item: str(item['path']))
    document = {'schema_version': 1, 'job_id': job_id, 'entries': valid_entries}
    temporary = index_path.with_name(f'.{index_path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(index_path)
    finally:
        temporary.unlink(missing_ok=True)


def _valid_entries(root: Path, job_id: str, entries: list[object]) -> bool:
    """Return whether existing index entries are strict and correlated."""

    required = set(IntegrityEntry.__dataclass_fields__)
    paths: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            return False
        if set(item) != required or item.get('job_id') != job_id:
            return False
        evidence_type = item.get('evidence_type')
        path = item.get('path')
        size = item.get('size')
        digest = item.get('sha256')
        finalized_at = item.get('finalized_at')
        if (
            not isinstance(evidence_type, str)
            or not evidence_type
            or not isinstance(path, str)
            or not path
            or path in paths
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or not digest.startswith('sha256:')
            or len(digest) != 71
            or not isinstance(finalized_at, str)
            or not finalized_at
        ):
            return False
        try:
            resolve_evidence_path(root, job_id, *Path(path).parts)
        except EvidencePathError:
            return False
        paths.add(path)
    return True


def _write_pending(root: Path, job_id: str, relative: Path, evidence_type: str) -> None:
    """Durably announce one evidence rename before publishing it."""

    path = resolve_evidence_path(root, job_id, INTEGRITY_PENDING)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    document = {'job_id': job_id, 'path': relative.as_posix(), 'type': evidence_type}
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _recover_pending(root: Path, job_id: str) -> None:
    """Complete or discard the transaction left by a stopped writer."""

    pending = resolve_evidence_path(root, job_id, INTEGRITY_PENDING)
    if not pending.exists():
        return
    try:
        document = json.loads(pending.read_text(encoding='utf-8'))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        message = 'pending integrity transaction is malformed'
        raise EvidencePathError(message) from error
    if (
        not isinstance(document, dict)
        or set(document) != {'job_id', 'path', 'type'}
        or document['job_id'] != job_id
        or not isinstance(document['path'], str)
        or not isinstance(document['type'], str)
        or not document['type']
    ):
        message = 'pending integrity transaction is malformed'
        raise EvidencePathError(message)
    relative = Path(document['path'])
    evidence_type = document['type']
    target = resolve_evidence_path(root, job_id, *relative.parts)
    if target.is_file():
        _update_index(
            root,
            job_id,
            _entry_for_file(root, job_id, target, evidence_type),
        )
    pending.unlink()
