"""Persist and read adapter-neutral agent invocation evidence."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Never
from uuid import uuid4

from agent_orchestra.models import RunState

INVOCATION_DIRECTORY_ESCAPE = 'invocation directory escapes the run directory'
INVOCATION_RECORD_ESCAPE = 'invocation record escapes the run directory'
UNEXPECTED_FIELDS = 'unexpected fields'


class InvocationEvidenceError(RuntimeError):
    """Raised when invocation evidence is unsafe or malformed."""


def _fail(message: str, cause: BaseException | None = None) -> Never:
    """Raise one invocation-evidence error with a stable diagnostic."""

    if cause is not None:
        raise InvocationEvidenceError(message) from cause
    raise InvocationEvidenceError(message)


class AttemptStatus(StrEnum):
    """Durable progress states for one agent process attempt."""

    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'


class AttemptConclusion(StrEnum):
    """Terminal outcomes for one agent process attempt."""

    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    TIMED_OUT = 'timed_out'
    CANCELLED = 'cancelled'
    INTERRUPTED = 'interrupted'


class TaskStatus(StrEnum):
    """Status derived from the latest attempt for one durable request."""

    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'


class RecoveryAction(StrEnum):
    """Deterministic action selected from durable attempt evidence."""

    LAUNCH = 'launch'
    FAIL_ACTIVATION_UNCERTAIN = 'fail_activation_uncertain'
    PERSIST_RESPONSE_AND_VALIDATE = 'persist_response_and_validate'
    VALIDATE_RESPONSE = 'validate_response'
    APPLY_CONCLUSION = 'apply_conclusion'
    NONE = 'none'


type AttemptStatusValue = Literal['pending', 'running', 'completed']
type AttemptConclusionValue = Literal[
    'succeeded', 'failed', 'timed_out', 'cancelled', 'interrupted'
]


ATTEMPT_TRANSITIONS: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.PENDING: frozenset({AttemptStatus.RUNNING, AttemptStatus.COMPLETED}),
    AttemptStatus.RUNNING: frozenset({AttemptStatus.RUNNING, AttemptStatus.COMPLETED}),
    AttemptStatus.COMPLETED: frozenset({AttemptStatus.COMPLETED}),
}


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
    task_id: str
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
    attempt: int
    status: AttemptStatusValue
    conclusion: AttemptConclusionValue | None
    response_received_at: str | None = None
    validation_started_at: str | None = None


def transition_attempt(
    record: InvocationRecord,
    status: AttemptStatus,
    *,
    conclusion: AttemptConclusion | None = None,
    finished_at: str | None = None,
    response_received_at: str | None = None,
    validation_started_at: str | None = None,
) -> InvocationRecord:
    """Return an attempt advanced through one valid durable transition."""

    validate_attempt_record(record)
    current = AttemptStatus(record.status)
    if status not in ATTEMPT_TRANSITIONS.get(current, frozenset()):
        _fail(f'invalid attempt transition from {current} to {status}')
    if status is AttemptStatus.COMPLETED and conclusion is None:
        _fail('completed attempt requires a conclusion')
    if status is not AttemptStatus.COMPLETED and conclusion is not None:
        _fail('non-completed attempt cannot have a conclusion')
    if current is AttemptStatus.PENDING and status is AttemptStatus.COMPLETED:
        if conclusion not in {
            AttemptConclusion.FAILED,
            AttemptConclusion.CANCELLED,
            AttemptConclusion.INTERRUPTED,
        }:
            _fail(f'invalid attempt transition from {current} to {status}')
        if response_received_at is not None or validation_started_at is not None:
            _fail('attempt completed before activation cannot have response milestones')
    for field, existing, milestone_update in (
        (
            'response_received_at',
            record.response_received_at,
            response_received_at,
        ),
        (
            'validation_started_at',
            record.validation_started_at,
            validation_started_at,
        ),
    ):
        if existing is not None and existing != milestone_update:
            _fail(f'{field} is immutable once set')
    updated = replace(
        record,
        status=status.value,
        conclusion=conclusion.value if conclusion is not None else None,
        finished_at=finished_at,
        response_received_at=response_received_at,
        validation_started_at=validation_started_at,
        timed_out=conclusion is AttemptConclusion.TIMED_OUT,
        interrupted=conclusion is AttemptConclusion.INTERRUPTED,
    )
    validate_attempt_record(updated)
    return updated


def _parsed_timestamp(value: str, field: str) -> datetime:
    """Parse one UTC evidence timestamp or raise a stable diagnostic."""

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        _fail(f'invalid {field}', error)
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        _fail(f'invalid {field}')
    return parsed


def _valid_record_types(record: InvocationRecord) -> bool:
    """Return whether lifecycle fields have their exact schema-4 primitive types."""

    return (
        type(record.schema_version) is int
        and isinstance(record.run_id, str)
        and isinstance(record.task_id, str)
        and isinstance(record.invocation_id, str)
        and isinstance(record.role, str)
        and isinstance(record.agent_vendor, str)
        and (record.requested_model is None or isinstance(record.requested_model, str))
        and isinstance(record.effective_models, (list, tuple))
        and all(isinstance(model, str) and model for model in record.effective_models)
        and isinstance(record.effective_model_status, str)
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
        and isinstance(record.status, str)
        and (record.conclusion is None or isinstance(record.conclusion, str))
        and (
            record.response_received_at is None
            or isinstance(record.response_received_at, str)
        )
        and (
            record.validation_started_at is None
            or isinstance(record.validation_started_at, str)
        )
    )


def validate_attempt_record(record: InvocationRecord) -> None:
    """Validate schema-4 lifecycle and milestone consistency."""

    if record.schema_version != 4:
        _fail('unsupported invocation record schema')
    if not record.task_id:
        _fail('schema 4 invocation requires task_id')
    if not _valid_record_types(record):
        _fail('invalid invocation record field type')
    if len(record.effective_models) != len(set(record.effective_models)):
        _fail('effective_models must be unique')
    if record.effective_model_status not in {'reported', 'unavailable'}:
        _fail('invalid effective_model_status')
    if (record.effective_model_status == 'reported') != bool(record.effective_models):
        _fail('effective_model_status contradicts effective_models')
    if record.role not in {'developer', 'reviewer'}:
        _fail('invalid attempt role')
    if record.iteration < 1 or record.attempt < 1:
        _fail('iteration and attempt must be positive')
    if record.status not in {'pending', 'running', 'completed'}:
        _fail('invalid attempt status')
    if record.conclusion not in {
        None,
        'succeeded',
        'failed',
        'timed_out',
        'cancelled',
        'interrupted',
    }:
        _fail('invalid attempt conclusion')
    terminal = record.status == 'completed'
    if terminal != (record.conclusion is not None):
        _fail('invalid attempt status and conclusion')
    expected_task_suffix = rf':\d{{6}}-{re.escape(record.role)}'
    if (
        re.fullmatch(re.escape(record.run_id) + expected_task_suffix, record.task_id)
        is None
    ):
        _fail('task_id does not match run and role')
    expected_invocation_id = f'{record.task_id}:attempt-{record.attempt:04d}'
    if record.invocation_id != expected_invocation_id:
        _fail('invocation_id does not match task and attempt')
    if record.status == 'pending' and (
        record.finished_at is not None
        or record.exit_code is not None
        or record.response_received_at is not None
        or record.validation_started_at is not None
    ):
        _fail('pending attempt has lifecycle milestones')
    if terminal and record.finished_at is None:
        _fail('completed attempt requires finished_at')
    started = _parsed_timestamp(record.started_at, 'started_at')
    finished = (
        _parsed_timestamp(record.finished_at, 'finished_at')
        if record.finished_at is not None
        else None
    )
    response = (
        _parsed_timestamp(record.response_received_at, 'response_received_at')
        if record.response_received_at is not None
        else None
    )
    validation = (
        _parsed_timestamp(record.validation_started_at, 'validation_started_at')
        if record.validation_started_at is not None
        else None
    )
    for name, milestone in (
        ('finished_at', finished),
        ('response_received_at', response),
        ('validation_started_at', validation),
    ):
        if milestone is not None and milestone < started:
            _fail(f'{name} precedes started_at')
    if finished is not None:
        for name, milestone in (
            ('response_received_at', response),
            ('validation_started_at', validation),
        ):
            if milestone is not None and milestone < finished:
                _fail(f'{name} precedes finished_at')
    if response is not None and validation is not None and validation < response:
        _fail('validation_started_at precedes response_received_at')
    if response is not None and finished is None:
        _fail('response_received_at requires finished_at')
    if validation is not None and response is None:
        _fail('validation_started_at requires response_received_at')
    if record.conclusion == 'succeeded' and (response is None or validation is None):
        _fail('succeeded attempt requires response validation')
    if record.conclusion == 'succeeded' and record.exit_code not in {None, 0}:
        _fail('succeeded attempt cannot have nonzero exit_code')
    if (
        record.conclusion in {'timed_out', 'cancelled', 'interrupted'}
        and validation is not None
    ):
        _fail(f'{record.conclusion} attempt cannot start validation')
    if record.timed_out != (record.conclusion == 'timed_out'):
        _fail('timed_out contradicts attempt conclusion')
    if record.interrupted != (record.conclusion == 'interrupted'):
        _fail('interrupted contradicts attempt conclusion')


def derive_task_status(records: tuple[InvocationRecord, ...]) -> TaskStatus:
    """Derive one task's status from its latest schema-4 attempt evidence."""

    if not records:
        return TaskStatus.PENDING
    task_ids = {record.task_id for record in records}
    if len(task_ids) != 1:
        _fail('task status requires one schema 4 task')
    attempts = [record.attempt for record in records]
    if len(attempts) != len(set(attempts)):
        _fail('task attempts must be unique')
    latest = max(records, key=lambda record: record.attempt)
    if latest.status == 'pending':
        return TaskStatus.PENDING
    if latest.status == 'running':
        return TaskStatus.RUNNING
    if latest.status == 'completed':
        return TaskStatus.COMPLETED
    _fail('task status requires schema 4 lifecycle evidence')


def recovery_action(
    record: InvocationRecord | None,
    *,
    response_artifact_present: bool = False,
    workflow_state: RunState,
) -> RecoveryAction:
    """Select recovery behavior without risking duplicate process activation."""

    if record is None:
        return RecoveryAction.LAUNCH
    validate_attempt_record(record)
    if record.status == 'pending':
        action = RecoveryAction.FAIL_ACTIVATION_UNCERTAIN
    elif record.status == 'completed':
        active_state = (
            RunState.REVIEWING if record.role == 'reviewer' else RunState.DEVELOPING
        )
        action = (
            RecoveryAction.NONE
            if workflow_state is not active_state
            else RecoveryAction.APPLY_CONCLUSION
        )
    elif (
        record.validation_started_at is not None
        or record.response_received_at is not None
    ):
        action = RecoveryAction.VALIDATE_RESPONSE
    elif response_artifact_present:
        action = RecoveryAction.PERSIST_RESPONSE_AND_VALIDATE
    else:
        action = RecoveryAction.FAIL_ACTIVATION_UNCERTAIN
    return action


def timestamp() -> str:
    """Return a canonical UTC timestamp."""

    return datetime.now(UTC).isoformat().replace('+00:00', 'Z')


def write_record(path: Path, record: InvocationRecord) -> None:
    """Write an invocation record atomically."""

    validate_attempt_record(record)
    document = asdict(record)
    new_record = not path.exists()
    if new_record and record.status != 'pending':
        _fail('new attempt must start pending')
    if not new_record and path.is_file():
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            _fail(f'invalid existing invocation record {path.name}', error)
        if not isinstance(existing, dict):
            _fail(f'invalid existing invocation record {path.name}')
        immutable_fields = ('run_id', 'task_id', 'invocation_id', 'role', 'attempt')
        if any(existing.get(field) != document[field] for field in immutable_fields):
            _fail('attempt identity is immutable')
        existing_status_value = existing.get('status')
        if not isinstance(existing_status_value, str):
            _fail(f'invalid existing invocation record {path.name}')
        try:
            existing_status = AttemptStatus(existing_status_value)
            new_status = AttemptStatus(record.status)
        except ValueError as error:
            _fail(f'invalid existing invocation record {path.name}', error)
        if (
            existing_status is AttemptStatus.PENDING
            and new_status is AttemptStatus.PENDING
        ):
            _fail('attempt record already exists')
        if new_status not in ATTEMPT_TRANSITIONS.get(existing_status, frozenset()):
            _fail(f'invalid attempt transition from {existing_status} to {new_status}')
        normalized_existing = {
            **existing,
            'effective_models': tuple(existing.get('effective_models', ())),
        }
        normalized_document = {
            **document,
            'effective_models': tuple(document['effective_models']),
        }
        if (
            existing_status is AttemptStatus.COMPLETED
            and normalized_existing != normalized_document
        ):
            _fail('completed attempt is immutable')
        for field in ('response_received_at', 'validation_started_at'):
            existing_milestone = existing.get(field)
            if existing_milestone is not None and existing_milestone != document[field]:
                _fail(f'{field} is immutable once set')
        if (
            existing_status is AttemptStatus.PENDING
            and new_status is AttemptStatus.COMPLETED
        ):
            if record.conclusion not in {'failed', 'cancelled', 'interrupted'}:
                _fail('invalid attempt transition from pending to completed')
            if (
                record.response_received_at is not None
                or record.validation_started_at is not None
            ):
                _fail(
                    'attempt completed before activation cannot have response milestones'
                )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        if new_record:
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                _fail('attempt record already exists', error)
        else:
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
        _fail(f'{description} escapes the run directory')
    return resolved


def read_records(run_directory: Path, run_id: str) -> tuple[InvocationRecord, ...]:
    """Read validated invocation records in deterministic order."""

    root = run_directory.resolve()
    manifests = root / 'invocations'
    if manifests.is_symlink():
        _fail(INVOCATION_DIRECTORY_ESCAPE)
    if not manifests.is_dir():
        return ()
    records: list[InvocationRecord] = []
    seen_attempts: set[tuple[str, int]] = set()
    for path in sorted(manifests.glob('*.json')):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            _fail(INVOCATION_RECORD_ESCAPE)
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            _fail(f'invalid invocation record {path.name}: {error}', error)
        if not isinstance(document, dict):
            _fail(f'invalid invocation record {path.name}: {UNEXPECTED_FIELDS}')
        if document.get('schema_version') != 4:
            _fail(f'unsupported invocation record schema in {path.name}')
        required = set(InvocationRecord.__dataclass_fields__)
        if set(document) != required:
            _fail(f'invalid invocation record {path.name}: {UNEXPECTED_FIELDS}')
        try:
            record = InvocationRecord(**document)
        except TypeError as error:
            _fail(f'invalid invocation record {path.name}: {error}', error)
        if not _valid_record_types(record):
            _fail(f'invalid invocation record {path.name}')
        validate_attempt_record(record)
        if record.schema_version != 4 or record.run_id != run_id:
            _fail(f'invocation record {path.name} does not match run {run_id}')
        attempt_key = (record.task_id, record.attempt)
        if attempt_key in seen_attempts:
            _fail(f'duplicate task attempt in {path.name}')
        seen_attempts.add(attempt_key)
        if (
            record.role not in {'developer', 'reviewer'}
            or record.iteration < 1
            or record.attempt < 1
            or not record.task_id
            or (record.status == 'completed') != (record.conclusion is not None)
            or (
                record.effective_model_status == 'reported'
                and not record.effective_models
            )
            or (
                record.effective_model_status == 'unavailable'
                and bool(record.effective_models)
            )
        ):
            _fail(f'invalid invocation record {path.name}')
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
