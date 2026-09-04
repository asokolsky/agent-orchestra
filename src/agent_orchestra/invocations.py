"""Persist and read adapter-neutral agent invocation evidence."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

INVOCATION_DIRECTORY_ESCAPE = 'invocation directory escapes the run directory'
INVOCATION_RECORD_ESCAPE = 'invocation record escapes the run directory'
LOG_DIRECTORY_ESCAPE = 'log directory escapes the run directory'
UNEXPECTED_FIELDS = 'unexpected fields'


class InvocationEvidenceError(RuntimeError):
    """Raised when invocation evidence is unsafe or malformed."""


@dataclass(frozen=True, slots=True, kw_only=True)
class InvocationIdentity:
    """Describe the selected agent independently from its adapter runtime."""

    vendor: str
    model: str | None
    runtime: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InvocationRecord:
    """Describe one bounded external process and its separate output streams."""

    schema_version: int
    run_id: str
    invocation_id: str
    role: Literal['developer', 'reviewer']
    agent_vendor: str
    requested_model: str | None
    effective_models: tuple[str, ...]
    effective_model_status: Literal['reported', 'unavailable']
    runtime: str
    iteration: int
    started_at: str
    finished_at: str | None
    exit_code: int | None
    timed_out: bool
    interrupted: bool
    stdout_path: str
    stderr_path: str
    attempt: int = 1


def timestamp() -> str:
    """Return a canonical UTC timestamp."""

    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def write_record(path: Path, record: InvocationRecord) -> None:
    """Write an invocation record atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(asdict(record), file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_file(root: Path, value: str, *, description: str) -> Path:
    """Resolve a declared evidence file without allowing an escape."""

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or candidate.is_symlink():
        raise InvocationEvidenceError(f'{description} escapes the run directory')
    return resolved


def read_records(run_directory: Path, run_id: str) -> tuple[InvocationRecord, ...]:
    """Read validated invocation records in deterministic order."""

    root = run_directory.resolve()
    manifests = root / 'invocations'
    if manifests.is_symlink():
        raise InvocationEvidenceError(INVOCATION_DIRECTORY_ESCAPE)
    if not manifests.is_dir():
        return ()
    records: list[InvocationRecord] = []
    for path in sorted(manifests.glob('*.json')):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise InvocationEvidenceError(INVOCATION_RECORD_ESCAPE)
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise InvocationEvidenceError(
                f'invalid invocation record {path.name}: {error}'
            ) from error
        if not isinstance(document, dict):
            raise InvocationEvidenceError(
                f'invalid invocation record {path.name}: {UNEXPECTED_FIELDS}'
            )
        version = document.get('schema_version')
        if version in {1, 2}:
            legacy_required = {
                'schema_version',
                'run_id',
                'invocation_id',
                'role',
                'agent_vendor',
                'agent_model',
                'runtime',
                'iteration',
                'started_at',
                'finished_at',
                'exit_code',
                'timed_out',
                'interrupted',
                'stdout_path',
                'stderr_path',
            }
            legacy_allowed = legacy_required | {'attempt'}
            if (
                not legacy_required.issubset(document)
                or not set(document).issubset(legacy_allowed)
                or (version == 2 and 'attempt' not in document)
            ):
                raise InvocationEvidenceError(
                    f'invalid invocation record {path.name}: {UNEXPECTED_FIELDS}'
                )
            requested_model = document.pop('agent_model')
            document = {
                **document,
                'requested_model': requested_model,
                'effective_models': (),
                'effective_model_status': 'unavailable',
            }
        else:
            required = set(InvocationRecord.__dataclass_fields__)
            if set(document) != required:
                raise InvocationEvidenceError(
                    f'invalid invocation record {path.name}: {UNEXPECTED_FIELDS}'
                )
        try:
            record = InvocationRecord(**document)
        except TypeError as error:
            raise InvocationEvidenceError(
                f'invalid invocation record {path.name}: {error}'
            ) from error
        valid_types = (
            type(record.schema_version) is int
            and isinstance(record.run_id, str)
            and isinstance(record.invocation_id, str)
            and isinstance(record.role, str)
            and isinstance(record.agent_vendor, str)
            and (
                record.requested_model is None
                or isinstance(record.requested_model, str)
            )
            and isinstance(record.effective_models, (list, tuple))
            and all(
                isinstance(model, str) and model for model in record.effective_models
            )
            and len(record.effective_models) == len(set(record.effective_models))
            and record.effective_model_status in {'reported', 'unavailable'}
            and isinstance(record.runtime, str)
            and type(record.iteration) is int
            and isinstance(record.started_at, str)
            and (record.finished_at is None or isinstance(record.finished_at, str))
            and (record.exit_code is None or type(record.exit_code) is int)
            and type(record.timed_out) is bool
            and type(record.interrupted) is bool
            and isinstance(record.stdout_path, str)
            and isinstance(record.stderr_path, str)
            and type(record.attempt) is int
        )
        if not valid_types:
            raise InvocationEvidenceError(f'invalid invocation record {path.name}')
        if record.schema_version not in {1, 2, 3} or record.run_id != run_id:
            raise InvocationEvidenceError(
                f'invocation record {path.name} does not match run {run_id}'
            )
        if (
            record.role not in {'developer', 'reviewer'}
            or record.iteration < 1
            or record.attempt < 1
            or (record.schema_version == 1 and record.attempt != 1)
            or (
                record.effective_model_status == 'reported'
                and not record.effective_models
            )
            or (
                record.effective_model_status == 'unavailable'
                and bool(record.effective_models)
            )
        ):
            raise InvocationEvidenceError(f'invalid invocation record {path.name}')
        stdout_path = _safe_file(root, record.stdout_path, description='stdout log')
        stderr_path = _safe_file(root, record.stderr_path, description='stderr log')
        records.append(
            replace(
                record,
                effective_models=tuple(record.effective_models),
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
            )
        )
    return tuple(records)


LEGACY_LOG = re.compile(
    r'^(?P<sequence>\d+)-(?P<role>developer|reviewer)\.(?P<stream>stdout|stderr)\.log$'
)


def legacy_log_groups(run_directory: Path) -> tuple[tuple[str, str, Path, Path], ...]:
    """Group safe legacy stdout and stderr logs by sequence and role."""

    root = run_directory.resolve()
    logs = root / 'logs'
    if logs.is_symlink():
        raise InvocationEvidenceError(LOG_DIRECTORY_ESCAPE)
    if not logs.is_dir():
        return ()
    groups: dict[tuple[str, str], dict[str, Path]] = {}
    for path in sorted(logs.glob('*.log')):
        match = LEGACY_LOG.fullmatch(path.name)
        if match is None:
            continue
        resolved = path.resolve()
        if path.is_symlink() or not resolved.is_relative_to(root):
            raise InvocationEvidenceError(f'log {path.name} escapes the run directory')
        key = (match['sequence'], match['role'])
        groups.setdefault(key, {})[match['stream']] = resolved
    return tuple(
        (
            sequence,
            role,
            streams.get('stdout', logs / f'{sequence}-{role}.stdout.log'),
            streams.get('stderr', logs / f'{sequence}-{role}.stderr.log'),
        )
        for (sequence, role), streams in sorted(groups.items())
    )
