"""Contained evidence paths and atomic per-job integrity indexes."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator

from agent_orchestra.errors import AgentOrchestraError
from agent_orchestra.manifests import evidence_path
from agent_orchestra.models import same_diff_digest

if TYPE_CHECKING:
    from collections.abc import Callable

HASH_CHUNK_SIZE = 1024 * 1024
INTEGRITY_INDEX = '.integrity.json'
INTEGRITY_LOCK = '.integrity.lock'
INTEGRITY_PENDING = '.integrity.pending.json'
SHARDED_JOB_ID = re.compile(
    r'^(?P<year>\d{4})(?P<month>0[1-9]|1[0-2])'
    r'(?P<day>0[1-9]|[12]\d|3[01])T'
    r'(?:[01]\d|2[0-3])(?:[0-5]\d){2}Z-[0-9a-f]{8}$'
)
EvidenceType = Literal[
    'decision_required',
    'developer_handoff',
    'execution',
    'failure',
    'invocation_record',
    'issue_feedback',
    'issue_review_request',
    'issue_review_result',
    'issue_snapshot',
    'process_stderr',
    'process_stdout',
    'rejected_developer_handoff',
    'rejected_review_artifact',
    'rejected_review_result',
    'remediation_request',
    'review_artifact',
    'review_batch_result',
    'review_request',
    'review_result',
]
EVIDENCE_TYPES: frozenset[str] = frozenset(
    {
        'decision_required',
        'developer_handoff',
        'execution',
        'failure',
        'invocation_record',
        'issue_feedback',
        'issue_review_request',
        'issue_review_result',
        'issue_snapshot',
        'process_stderr',
        'process_stdout',
        'rejected_developer_handoff',
        'rejected_review_artifact',
        'rejected_review_result',
        'remediation_request',
        'review_artifact',
        'review_batch_result',
        'review_request',
        'review_result',
    }
)


class EvidencePathError(AgentOrchestraError):
    """Raised when evidence cannot be contained beneath its selected job."""


@dataclass(frozen=True, slots=True)
class IntegrityEntry:
    """Immutable metadata for one finalized evidence file."""

    job_id: str
    evidence_type: EvidenceType
    path: str
    size: int
    sha256: str
    finalized_at: str


def _job_shard(job_id: str) -> tuple[str, str, str] | None:
    """Return the UTC date shard encoded in a generated job identifier."""

    match = SHARDED_JOB_ID.fullmatch(job_id)
    if match is None:
        return None
    return match.group('year'), match.group('month'), match.group('day')


def evidence_root_for_job(job_directory: Path) -> Path:
    """Return the configured evidence root for an established job directory."""

    directory = job_directory.expanduser().absolute()
    shard = _job_shard(directory.name)
    if shard is not None and directory.parent.parts[-3:] == shard:
        return directory.parents[3]
    return directory.parent


def resolve_evidence_path(root: Path, job_id: str, *parts: str) -> Path:
    """Return a flat or UTC-sharded job path without following symlinks."""

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
    shard = _job_shard(job_id)
    job_directory = (
        evidence_root / job_id
        if shard is None
        else evidence_root.joinpath(*shard, job_id)
    )
    candidate = job_directory.joinpath(*parts)
    current = evidence_root
    for part in candidate.relative_to(evidence_root).parts:
        current /= part
        if current.is_symlink():
            message = (
                f'evidence path contains a symlink and escapes containment: {current}'
            )
            raise EvidencePathError(message)
    if not candidate.resolve().is_relative_to(job_directory.resolve()):
        message = 'evidence path escapes the selected job'
        raise EvidencePathError(message)
    return candidate


class JobEvidence:
    """Own one job's contained evidence paths and its integrity index."""

    def __init__(self, root: Path, job_id: str) -> None:
        """Create the collaborator for one job beneath one evidence root."""

        self.root = root
        self.job_id = job_id

    @classmethod
    def for_directory(cls, job_directory: Path) -> JobEvidence:
        """Create the collaborator owning an already established job directory."""

        return cls(evidence_root_for_job(job_directory), job_directory.name)

    def path(self, *parts: str) -> Path:
        """Return one contained path beneath this job."""

        return resolve_evidence_path(self.root, self.job_id, *parts)

    @property
    def directory(self) -> Path:
        """Return this job's contained evidence directory."""

        return self.path()

    def record_finalized(
        self,
        path: Path,
        evidence_type: EvidenceType,
        *,
        finalized_at: datetime | None = None,
        replace_existing: bool = True,
    ) -> IntegrityEntry:
        """Hash a finalized regular file and atomically update the job index."""

        job_directory = self.directory
        entry = self._entry_for_file(path, evidence_type)
        if finalized_at is not None:
            entry = IntegrityEntry(
                **{
                    **asdict(entry),
                    'finalized_at': finalized_at.isoformat().replace('+00:00', 'Z'),
                }
            )
        job_directory.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._recover_pending()
            self._update_index(
                entry,
                replace_existing=replace_existing,
                native_creation=False,
            )
        return entry

    def finalize_write(
        self,
        temporary: Path,
        path: Path,
        evidence_type: EvidenceType,
        *,
        exclusive: bool = False,
        remove_source_entry: bool = False,
    ) -> IntegrityEntry:
        """Publish a prepared file through a recoverable index transaction."""

        job_directory = self.directory
        relative = _relative_evidence_path(job_directory, path)
        contained = self.path(*relative.parts)
        try:
            temporary_relative = temporary.relative_to(job_directory)
            self.path(*temporary_relative.parts)
        except (EvidencePathError, ValueError) as error:
            message = 'temporary evidence does not belong to the selected job'
            raise EvidencePathError(message) from error
        job_directory.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._recover_pending()
            removed_path = (
                temporary_relative.as_posix() if remove_source_entry else None
            )
            self._write_pending(relative, evidence_type, removed_path)
            if exclusive:
                try:
                    os.link(temporary, contained)
                except FileExistsError as error:
                    self.path(INTEGRITY_PENDING).unlink()
                    message = 'finalized evidence already exists'
                    raise EvidencePathError(message) from error
            else:
                temporary.replace(contained)
            entry = self._entry_for_file(contained, evidence_type)
            self._update_index(
                entry,
                native_creation=True,
                removed_path=removed_path,
            )
            self.path(INTEGRITY_PENDING).unlink()
        return entry

    def relocate_finalized(
        self,
        source: Path,
        destination: Path,
        evidence_type: EvidenceType,
    ) -> IntegrityEntry:
        """Atomically relocate finalized evidence and its indexed identity."""

        return self.finalize_write(
            source,
            destination,
            evidence_type,
            remove_source_entry=True,
        )

    def recover_index(self) -> None:
        """Reconcile a finalization interrupted after its evidence rename."""

        if not self.directory.exists():
            return
        with self._locked(translate_open_errors=False):
            self._recover_pending()

    @contextmanager
    def _locked(self, *, translate_open_errors: bool = True) -> Iterator[None]:
        """Hold this job's exclusive integrity lock for one transaction."""

        # Recovery reports a failed lock open as the original OSError, while the
        # writing operations report it as EvidencePathError. Keeping that
        # difference explicit preserves each caller's established contract.
        lock_path = self.path(INTEGRITY_LOCK)
        lock_flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        if translate_open_errors:
            try:
                descriptor = os.open(lock_path, lock_flags, 0o600)
            except OSError as error:
                message = 'cannot open integrity lock'
                raise EvidencePathError(message) from error
        else:
            descriptor = os.open(lock_path, lock_flags, 0o600)
        with os.fdopen(descriptor, 'a+', encoding='utf-8') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def _entry_for_file(
        self, path: Path, evidence_type: EvidenceType
    ) -> IntegrityEntry:
        """Hash one contained regular file into an integrity entry."""

        relative = _relative_evidence_path(self.directory, path)
        contained = self.path(*relative.parts)
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
            job_id=self.job_id,
            evidence_type=evidence_type,
            path=relative.as_posix(),
            size=size,
            sha256=f'sha256:{digest.hexdigest()}',
            finalized_at=datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        )

    def _update_index(
        self,
        entry: IntegrityEntry,
        *,
        replace_existing: bool = True,
        native_creation: bool,
        removed_path: str | None = None,
    ) -> None:
        """Replace one entry in the integrity index while its lock is held."""

        index_path = self.path(INTEGRITY_INDEX)
        entries: list[object] = []
        backfilled_at: str | None = None
        if index_path.exists():
            try:
                descriptor = os.open(
                    index_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                )
                with os.fdopen(descriptor, encoding='utf-8') as file:
                    document = json.load(file)
                entries = list(document['entries'])
            except (OSError, KeyError, TypeError, ValueError) as error:
                message = 'integrity index is malformed'
                raise EvidencePathError(message) from error
            if (
                set(document)
                != {'schema_version', 'job_id', 'backfilled_at', 'entries'}
                or document.get('schema_version') != 1
                or document.get('job_id') != self.job_id
                or (
                    document.get('backfilled_at') is not None
                    and not isinstance(document.get('backfilled_at'), str)
                )
                or not self._valid_entries(entries)
            ):
                message = 'integrity index is malformed'
                raise EvidencePathError(message)
            backfilled_at = document['backfilled_at']
        elif not native_creation or self._has_other_evidence(entry.path):
            backfilled_at = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
        serialized = asdict(entry)
        valid_entries = [item for item in entries if isinstance(item, dict)]
        if removed_path is not None:
            valid_entries = [
                item for item in valid_entries if item.get('path') != removed_path
            ]
        prior = next(
            (item for item in valid_entries if item.get('path') == entry.path), None
        )
        valid_entries = [
            item for item in valid_entries if item.get('path') != entry.path
        ]
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
        document = {
            'schema_version': 1,
            'job_id': self.job_id,
            'backfilled_at': backfilled_at,
            'entries': valid_entries,
        }
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

    def _valid_entries(self, entries: list[object]) -> bool:
        """Return whether existing index entries are strict and correlated."""

        required = set(IntegrityEntry.__dataclass_fields__)
        paths: set[str] = set()
        for item in entries:
            if not isinstance(item, dict):
                return False
            if set(item) != required or item.get('job_id') != self.job_id:
                return False
            evidence_type = item.get('evidence_type')
            path = item.get('path')
            size = item.get('size')
            digest = item.get('sha256')
            finalized_at = item.get('finalized_at')
            if (
                not isinstance(evidence_type, str)
                or evidence_type not in EVIDENCE_TYPES
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
                self.path(*Path(path).parts)
            except EvidencePathError:
                return False
            paths.add(path)
        return True

    def _write_pending(
        self,
        relative: Path,
        evidence_type: EvidenceType,
        removed_path: str | None,
    ) -> None:
        """Durably announce one evidence rename before publishing it."""

        path = self.path(INTEGRITY_PENDING)
        temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
        document = {
            'job_id': self.job_id,
            'path': relative.as_posix(),
            'type': evidence_type,
            'removed_path': removed_path,
        }
        try:
            with temporary.open('x', encoding='utf-8') as file:
                json.dump(document, file, indent=2)
                file.write('\n')
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def _recover_pending(self) -> None:
        """Complete or discard the transaction left by a stopped writer."""

        pending = self.path(INTEGRITY_PENDING)
        if not pending.exists():
            return
        try:
            document = json.loads(pending.read_text(encoding='utf-8'))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            message = 'pending integrity transaction is malformed'
            raise EvidencePathError(message) from error
        if (
            not isinstance(document, dict)
            or set(document) != {'job_id', 'path', 'type', 'removed_path'}
            or document['job_id'] != self.job_id
            or not isinstance(document['path'], str)
            or not isinstance(document['type'], str)
            or document['type'] not in EVIDENCE_TYPES
            or (
                document['removed_path'] is not None
                and not isinstance(document['removed_path'], str)
            )
        ):
            message = 'pending integrity transaction is malformed'
            raise EvidencePathError(message)
        relative = Path(document['path'])
        evidence_type = cast('EvidenceType', document['type'])
        target = self.path(*relative.parts)
        removed_path = document['removed_path']
        if removed_path is not None:
            removed_relative = Path(removed_path)
            removed_source = self.path(*removed_relative.parts)
            removed_path = removed_relative.as_posix()
            if removed_source.exists():
                pending.unlink()
                return
        if target.is_file():
            self._update_index(
                self._entry_for_file(target, evidence_type),
                native_creation=True,
                removed_path=removed_path,
            )
        pending.unlink()

    def _has_other_evidence(self, current_path: str) -> bool:
        """Return whether an index is first appearing around earlier evidence."""

        job_directory = self.directory
        internal = {INTEGRITY_INDEX, INTEGRITY_LOCK, INTEGRITY_PENDING}
        for directory, names, files in os.walk(job_directory, followlinks=False):
            names[:] = [
                name for name in names if not (Path(directory) / name).is_symlink()
            ]
            for name in files:
                path = Path(directory) / name
                relative = path.relative_to(job_directory).as_posix()
                if (
                    relative != current_path
                    and relative not in internal
                    and not name.endswith('.tmp')
                ):
                    return True
        return False


def _relative_evidence_path(job_directory: Path, path: Path) -> Path:
    """Return a path relative to its explicit owning job."""

    candidate = path if path.is_absolute() else job_directory / path
    try:
        return candidate.relative_to(job_directory)
    except ValueError as error:
        message = 'evidence path escapes the selected job'
        raise EvidencePathError(message) from error


NOT_OBJECT = 'reviewer response must be a JSON object'
WORKTREE_CHANGED = 'worktree changed during read-only review'


# Stable error codes the CLI documents. They live beside the error that carries
# them: a code without its exception is not usable on its own, and every module
# that raises WorkerError needs the same vocabulary.
REVIEWER_BATCH_INCOMPLETE_CODE = 'reviewer_batch_incomplete'
RUN_NOT_RESUMABLE_CODE = 'run_not_resumable'
RESUME_METADATA_UNSUPPORTED_CODE = 'resume_metadata_unsupported'
RESUME_SCOPE_CHANGED_CODE = 'resume_scope_changed'
RESUME_INTERRUPTED_CODE = 'resume_interrupted'
RESUME_EXECUTION_FAILED_CODE = 'resume_execution_failed'
RESUME_ACTIVATION_UNCERTAIN_CODE = 'resume_activation_uncertain'
RESUME_CANCELLED_CODE = 'resume_cancelled'


class WorkerError(AgentOrchestraError):
    """Raised when a queued run cannot complete its review step."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        """Create an error with an optional stable machine-readable code."""

        super().__init__(message)
        self.code = code


def run_evidence_path(run_directory: Path, *parts: str) -> Path:
    """Resolve one contained path beneath an established run directory."""

    try:
        return resolve_evidence_path(
            evidence_root_for_job(run_directory), run_directory.name, *parts
        )
    except EvidencePathError as error:
        raise WorkerError(str(error)) from error


def manifest_evidence_path(
    run_directory: Path, evidence_type: str, ordinal: int
) -> Path:
    """Resolve one manifest-rendered path through the run boundary."""

    return run_evidence_path(
        run_directory, *Path(evidence_path(evidence_type, ordinal=ordinal)).parts
    )


def contained_job_reference(
    run_directory: Path, value: str | Path, error_message: str
) -> Path:
    """Resolve an evidence reference through the selected run boundary."""

    candidate = Path(value)
    try:
        relative = candidate.relative_to(run_directory)
        return run_evidence_path(run_directory, *relative.parts)
    except (ValueError, WorkerError) as error:
        raise WorkerError(error_message) from error


def require_unchanged(actual: str | None, expected: str) -> None:
    """Reject a review when its worktree digest changed during execution."""

    if not same_diff_digest(actual, expected):
        raise WorkerError(WORKTREE_CHANGED)


def worktree_digest(
    digest_worktree: Callable[[Path, str], str | None], worktree: Path, base_sha: str
) -> str | None:
    """Normalize filesystem and Git digest failures as worker errors."""

    try:
        return digest_worktree(worktree, base_sha)
    except (OSError, RuntimeError, AgentOrchestraError) as error:
        raise WorkerError(f'cannot compute worktree digest: {error}') from error


def write_json_atomic(
    path: Path, document: dict[str, Any], evidence_type: EvidenceType
) -> None:
    """Write one UTF-8 JSON document atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        finalize_temporary_path(temporary, path, evidence_type)
    finally:
        temporary.unlink(missing_ok=True)


def write_text_atomic(
    path: Path, content: str, *, evidence_type: EvidenceType | None = None
) -> None:
    """Write one UTF-8 text artifact atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if evidence_type is not None:
            finalize_temporary_path(temporary, path, evidence_type)
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def archive_unaccepted_response(
    path: Path, destination: Path, evidence_type: EvidenceType
) -> None:
    """Preserve a partial response so a retry cannot consume stale output."""

    if path.exists():
        structural = {'messages', 'artifacts', 'logs', 'invocations'}
        job_directory = (
            destination.parent.parent
            if destination.parent.name in structural
            else destination.parent
        )
        JobEvidence.for_directory(job_directory).relocate_finalized(
            path, destination, evidence_type
        )


def record_finalized_path(path: Path, evidence_type: EvidenceType) -> None:
    """Record one finalized worker artifact in its owning job index."""

    structural = {'messages', 'artifacts', 'logs', 'invocations', 'review-batches'}
    job_directory = (
        path.parent.parent if path.parent.name in structural else path.parent
    )
    JobEvidence.for_directory(job_directory).record_finalized(path, evidence_type)


def finalize_temporary_path(
    temporary: Path, path: Path, evidence_type: EvidenceType
) -> None:
    """Publish one worker file through the recoverable evidence protocol."""

    structural = {'messages', 'artifacts', 'logs', 'invocations', 'review-batches'}
    job_directory = (
        path.parent.parent if path.parent.name in structural else path.parent
    )
    JobEvidence.for_directory(job_directory).finalize_write(
        temporary, path, evidence_type
    )


def output_text(value: str | bytes | None) -> str:
    """Normalize captured subprocess output for durable UTF-8 logs."""

    if value is None:
        return ''
    return value.decode(errors='replace') if isinstance(value, bytes) else value


def invocation_stem(sequence: int, role: str, attempt: int) -> str:
    """Return the stable evidence stem for one invocation attempt."""

    retry_suffix = '' if attempt == 1 else f'-attempt-{attempt:04d}'
    return f'{sequence:06d}-{role}{retry_suffix}'


def read_json_object(path: Path) -> dict[str, Any]:
    """Read a JSON object or raise a stable worker error."""

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkerError(f'invalid reviewer response: {error}') from error
    if not isinstance(document, dict):
        raise WorkerError(NOT_OBJECT)
    return document


def reviewer_dispatch_path(run_directory: Path, relative: str) -> Path:
    """Resolve one reviewer-owned relative path beneath the run directory."""

    return run_evidence_path(run_directory, *Path(relative).parts)
