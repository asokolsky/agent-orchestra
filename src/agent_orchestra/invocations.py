"""Persist and read adapter-neutral agent invocation evidence."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Never, cast
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.evidence import (
    RESUME_ACTIVATION_UNCERTAIN_CODE,
    EvidencePathError,
    EvidenceType,
    JobEvidence,
    WorkerError,
    evidence_root_for_job,
    invocation_stem,
    output_text,
    record_finalized_path,
    resolve_evidence_path,
    run_evidence_path,
    write_text_atomic,
)
from agent_orchestra.models import RunState
from agent_orchestra.persisted_enum import PersistedEnum
from agent_orchestra.reviewer_paths import (
    ReviewerIdentityError,
    reviewer_invocation_id,
    reviewer_invocation_stem,
    reviewer_task_id,
    validate_reviewer_id,
)

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


class AttemptStatus(PersistedEnum):
    """Durable progress states for one agent process attempt."""

    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETED = 'completed'


class AttemptConclusion(PersistedEnum):
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


class EffectiveModelStatus(PersistedEnum):
    """Whether a runtime reported machine-readable effective model identities."""

    REPORTED = 'reported'
    UNAVAILABLE = 'unavailable'


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
    role: RuntimeRole
    agent_vendor: str
    requested_model: str | None
    effective_models: tuple[str, ...]
    effective_model_status: EffectiveModelStatus
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
    status: AttemptStatus
    conclusion: AttemptConclusion | None
    response_received_at: str | None = None
    validation_started_at: str | None = None
    reviewer_id: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptIdentity:
    """
    Which agent ran, for which round of which job, and on which try.

    These values travel together through every function that records attempt
    evidence, and they alone determine the durable task ID, invocation ID,
    evidence stem, and record schema. Keeping them as one value type puts those
    four derivations beside the fields they derive from, instead of repeating
    the branch at each caller.
    """

    run_id: str
    role: RuntimeRole
    agent: InvocationIdentity
    iteration: int
    sequence: int
    attempt: int = 1
    reviewer_id: str | None = None
    invocation_id: str | None = None

    def __post_init__(self) -> None:
        """Reject a reviewer identifier on a role that cannot own one."""

        if self.reviewer_id is not None and self.role is not RuntimeRole.REVIEWER:
            message = 'only reviewer invocations can have a reviewer ID'
            raise WorkerError(message)

    @property
    def schema_version(self) -> int:
        """Return the record schema this attempt's identity requires."""

        return 5 if self.reviewer_id is not None else 4

    @property
    def task_id(self) -> str:
        """Return the durable task this attempt belongs to."""

        if self.reviewer_id is None:
            return f'{self.run_id}:{self.sequence:06d}-{self.role}'
        return self._reviewer_qualified(
            reviewer_task_id, self.run_id, self.sequence, self.reviewer_id
        )

    @property
    def durable_invocation_id(self) -> str:
        """Return the supplied invocation ID or the one this identity implies."""

        if self.invocation_id is not None:
            return self.invocation_id
        if self.reviewer_id is None:
            return f'{self.task_id}:attempt-{self.attempt:04d}'
        return self._reviewer_qualified(
            reviewer_invocation_id,
            self.run_id,
            self.sequence,
            self.reviewer_id,
            self.attempt,
        )

    @property
    def evidence_stem(self) -> str:
        """Return the filename stem shared by this attempt's evidence."""

        if self.reviewer_id is None:
            return invocation_stem(self.sequence, self.role, self.attempt)
        return self._reviewer_qualified(
            reviewer_invocation_stem, self.sequence, self.reviewer_id, self.attempt
        )

    @staticmethod
    def _reviewer_qualified(builder: Callable[..., str], *arguments: object) -> str:
        """Build one reviewer-qualified identifier inside this error boundary."""

        try:
            return str(builder(*arguments))
        except ReviewerIdentityError as error:
            raise WorkerError(str(error)) from error


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessOutcome:
    """What one agent process produced and how it ended."""

    started_at: str
    stdout: str | bytes | None
    stderr: str | bytes | None
    exit_code: int | None
    timed_out: bool = False
    interrupted: bool = False
    finished: bool = True
    finished_at: str | None = None
    effective_models: tuple[str, ...] = ()
    effective_model_status: EffectiveModelStatus = EffectiveModelStatus.UNAVAILABLE

    @property
    def derived_conclusion(self) -> AttemptConclusion:
        """Return the terminal outcome this process result implies."""

        if self.timed_out:
            return AttemptConclusion.TIMED_OUT
        if self.interrupted:
            return AttemptConclusion.INTERRUPTED
        if self.exit_code == 0:
            return AttemptConclusion.SUCCEEDED
        return AttemptConclusion.FAILED


@dataclass(frozen=True, slots=True, kw_only=True)
class AttemptLifecycle:
    """One attempt's durable status, outcome, and response milestones."""

    status: AttemptStatus | None = None
    conclusion: AttemptConclusion | None = None
    response_received_at: str | None = None
    validation_started_at: str | None = None

    def resolved(self, outcome: ProcessOutcome) -> ResolvedLifecycle:
        """Fill the status and conclusion a finished process implies."""

        status = self.status or (
            AttemptStatus.COMPLETED if outcome.finished else AttemptStatus.PENDING
        )
        conclusion = self.conclusion
        if status is AttemptStatus.COMPLETED and conclusion is None:
            conclusion = outcome.derived_conclusion
        return ResolvedLifecycle(
            status=status,
            conclusion=conclusion,
            response_received_at=self.response_received_at,
            validation_started_at=self.validation_started_at,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedLifecycle:
    """One attempt's lifecycle after the implied status and outcome are filled."""

    status: AttemptStatus
    conclusion: AttemptConclusion | None
    response_received_at: str | None
    validation_started_at: str | None


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
        status=status,
        conclusion=conclusion,
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
    """Return whether lifecycle fields have their exact primitive types."""

    return (
        type(record.schema_version) is int
        and isinstance(record.run_id, str)
        and isinstance(record.task_id, str)
        and isinstance(record.invocation_id, str)
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
        and (record.reviewer_id is None or isinstance(record.reviewer_id, str))
    )


def validate_attempt_record(record: InvocationRecord) -> None:
    """Validate lifecycle, identity, and milestone consistency."""

    if record.schema_version not in {4, 5}:
        _fail('unsupported invocation record schema')
    if record.schema_version == 4 and record.reviewer_id is not None:
        _fail('schema 4 invocation cannot contain reviewer_id')
    if record.schema_version == 5:
        if record.role == 'reviewer' and record.reviewer_id is None:
            _fail('schema 5 reviewer invocation requires reviewer_id')
        if record.role != 'reviewer' and record.reviewer_id is not None:
            _fail('only reviewer invocations can contain reviewer_id')
    if not record.task_id:
        _fail('schema 4 invocation requires task_id')
    if not _valid_record_types(record):
        _fail('invalid invocation record field type')
    if not isinstance(record.role, RuntimeRole):
        _fail('invalid attempt role')
    if not isinstance(record.effective_model_status, EffectiveModelStatus):
        _fail('invalid effective_model_status')
    if not isinstance(record.status, AttemptStatus):
        _fail('invalid attempt status')
    if record.conclusion is not None and not isinstance(
        record.conclusion, AttemptConclusion
    ):
        _fail('invalid attempt conclusion')
    if len(record.effective_models) != len(set(record.effective_models)):
        _fail('effective_models must be unique')
    if (record.effective_model_status == 'reported') != bool(record.effective_models):
        _fail('effective_model_status contradicts effective_models')
    if record.iteration < 1 or record.attempt < 1:
        _fail('iteration and attempt must be positive')
    terminal = record.status == 'completed'
    if terminal != (record.conclusion is not None):
        _fail('invalid attempt status and conclusion')
    if record.reviewer_id is not None:
        try:
            reviewer_id = validate_reviewer_id(record.reviewer_id)
        except ValueError as error:
            _fail(str(error), error)
        expected_task_suffix = rf':\d{{6}}-reviewer-{re.escape(reviewer_id)}'
    else:
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
    """Derive one task's status from its latest validated attempt evidence."""

    if not records:
        return TaskStatus.PENDING
    task_ids = {record.task_id for record in records}
    if len(task_ids) != 1:
        _fail('task status requires one task')
    attempts = [record.attempt for record in records]
    if len(attempts) != len(set(attempts)):
        _fail('task attempts must be unique')
    latest = max(records, key=lambda record: record.attempt)
    # Identity, not equality: a bare string compares equal to a StrEnum member,
    # so `==` would let an unvalidated record derive a status that
    # validate_attempt_record rejects.
    if latest.status is AttemptStatus.PENDING:
        return TaskStatus.PENDING
    if latest.status is AttemptStatus.RUNNING:
        return TaskStatus.RUNNING
    if latest.status is AttemptStatus.COMPLETED:
        return TaskStatus.COMPLETED
    _fail('task status requires valid lifecycle evidence')


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


def _job_relative_stream(value: str, job_directory: Path) -> str:
    """Return one stream path relative to its job, or the value unchanged."""

    candidate = Path(value)
    if not candidate.is_absolute():
        return value
    try:
        return candidate.resolve().relative_to(job_directory.resolve()).as_posix()
    except OSError, ValueError:
        return value


def _job_relative_streams(
    document: dict[str, Any], job_directory: Path
) -> dict[str, Any]:
    """Return one record document with job-relative stream paths."""

    return {
        **document,
        'stdout_path': _job_relative_stream(document['stdout_path'], job_directory),
        'stderr_path': _job_relative_stream(document['stderr_path'], job_directory),
    }


def _validated_document(
    path: Path, record: InvocationRecord
) -> tuple[dict[str, object], bool]:
    """Validate one attempt against any persisted record and render it."""

    validate_attempt_record(record)
    document = _job_relative_streams(asdict(record), path.parent.parent)
    if record.schema_version == 4:
        document.pop('reviewer_id')
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
        immutable_fields = (
            'run_id',
            'task_id',
            'invocation_id',
            'role',
            'attempt',
            'reviewer_id',
        )
        if any(
            existing.get(field) != document.get(field) for field in immutable_fields
        ):
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
            **_job_relative_streams(existing, path.parent.parent),
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
    return document, new_record


@contextmanager
def _prepared_record(path: Path, document: dict[str, object]) -> Iterator[Path]:
    """Write one attempt document to a sibling temporary awaiting publication."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        yield temporary
    finally:
        temporary.unlink(missing_ok=True)


def _write_record_unindexed(path: Path, record: InvocationRecord) -> None:
    """
    Write one attempt record atomically without indexing it as evidence.

    Private on purpose. Publishing an attempt is
    ``InvocationEvidenceStore.write``, which validates containment and updates
    the integrity index, and there is no supported way to persist a record
    without that. This primitive exists for tests that exercise the record
    protocol -- validation, transitions, and immutability -- against a bare
    directory that is not an evidence root.
    """

    document, new_record = _validated_document(path, record)
    with _prepared_record(path, document) as temporary:
        if new_record:
            try:
                os.link(temporary, path)
            except FileExistsError as error:
                _fail('attempt record already exists', error)
        else:
            temporary.replace(path)


def _safe_file(root: Path, value: str, *, description: str) -> Path:
    """Resolve a declared evidence file without allowing an escape."""

    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        relative = candidate.relative_to(root)
        resolved = resolve_evidence_path(
            evidence_root_for_job(root), root.name, *relative.parts
        )
    except EvidencePathError, ValueError:
        _fail(f'{description} escapes the run directory')
    return resolved.resolve()


class InvocationEvidenceStore:
    """Own one job's invocation records and their contained evidence."""

    def __init__(self, job_directory: Path) -> None:
        """Create the store for one established job evidence directory."""

        self.job_directory = job_directory

    def write(self, path: Path, record: InvocationRecord) -> None:
        """Publish one attempt record as contained, integrity-indexed evidence."""

        document, new_record = _validated_document(path, record)
        with _prepared_record(path, document) as temporary:
            try:
                JobEvidence.for_directory(self.job_directory).finalize_write(
                    temporary, path, 'invocation_record', exclusive=new_record
                )
            except EvidencePathError as error:
                if new_record and str(error) == 'finalized evidence already exists':
                    _fail('attempt record already exists', error)
                _fail(str(error), error)

    def read_all(self, run_id: str) -> tuple[InvocationRecord, ...]:
        """Read validated invocation records in deterministic order."""

        root = self.job_directory.resolve()
        try:
            evidence_root = evidence_root_for_job(root)
            manifests = resolve_evidence_path(evidence_root, root.name, 'invocations')
        except EvidencePathError:
            _fail(INVOCATION_DIRECTORY_ESCAPE)
        if not manifests.is_dir():
            return ()
        records: list[InvocationRecord] = []
        seen_attempts: set[tuple[str, int]] = set()
        for candidate_path in sorted(manifests.glob('*.json')):
            try:
                path = resolve_evidence_path(
                    evidence_root, root.name, 'invocations', candidate_path.name
                )
            except EvidencePathError:
                _fail(INVOCATION_RECORD_ESCAPE)
            try:
                document = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                _fail(f'invalid invocation record {path.name}: {error}', error)
            if not isinstance(document, dict):
                _fail(f'invalid invocation record {path.name}: {UNEXPECTED_FIELDS}')
            schema_version = document.get('schema_version')
            if schema_version not in {4, 5}:
                _fail(f'unsupported invocation record schema in {path.name}')
            required = set(InvocationRecord.__dataclass_fields__)
            if schema_version == 4:
                required.remove('reviewer_id')
            if set(document) != required:
                _fail(f'invalid invocation record {path.name}: {UNEXPECTED_FIELDS}')
            if schema_version == 4:
                document['reviewer_id'] = None

            def fail_record(message: str, name: str = path.name) -> Never:
                """Report one unreadable persisted field for this record."""

                _fail(f'invalid invocation record {name}: {message}')

            document['role'] = RuntimeRole.decode(
                document.get('role'), fail=fail_record
            )
            document['status'] = AttemptStatus.decode(
                document.get('status'), fail=fail_record
            )
            document['effective_model_status'] = EffectiveModelStatus.decode(
                document.get('effective_model_status'), fail=fail_record
            )
            if document.get('conclusion') is not None:
                document['conclusion'] = AttemptConclusion.decode(
                    document['conclusion'], fail=fail_record
                )
            try:
                record = InvocationRecord(**document)
            except TypeError as error:
                _fail(f'invalid invocation record {path.name}: {error}', error)
            if not _valid_record_types(record):
                _fail(f'invalid invocation record {path.name}')
            validate_attempt_record(record)
            if record.run_id != run_id:
                _fail(f'invocation record {path.name} does not match run {run_id}')
            attempt_key = (record.task_id, record.attempt)
            if attempt_key in seen_attempts:
                _fail(f'duplicate task attempt in {path.name}')
            seen_attempts.add(attempt_key)
            if (
                record.iteration < 1
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

    def recover_completed(self, run_id: str) -> None:
        """Add only missing index entries from fully validated completed attempts."""

        records = self.read_all(run_id)
        for record in records:
            if record.status != 'completed':
                continue
            task_stem = record.task_id.rsplit(':', 1)[-1]
            if record.role == 'issue_reviewer':
                stem = f'{record.iteration:06d}-issue-reviewer-attempt-{record.attempt:04d}'
            elif record.reviewer_id is not None:
                stem = f'{task_stem}.attempt-{record.attempt:04d}'
            else:
                stem = (
                    task_stem
                    if record.attempt == 1
                    else f'{task_stem}-attempt-{record.attempt:04d}'
                )
            manifest = resolve_evidence_path(
                evidence_root_for_job(self.job_directory),
                run_id,
                'invocations',
                f'{stem}.json',
            )
            expected_streams = (
                (
                    resolve_evidence_path(
                        evidence_root_for_job(self.job_directory),
                        run_id,
                        'logs',
                        f'{stem}.stdout.log',
                    ),
                    Path(record.stdout_path),
                    'process_stdout',
                ),
                (
                    resolve_evidence_path(
                        evidence_root_for_job(self.job_directory),
                        run_id,
                        'logs',
                        f'{stem}.stderr.log',
                    ),
                    Path(record.stderr_path),
                    'process_stderr',
                ),
            )
            if not manifest.is_file() or any(
                declared != expected for expected, declared, _kind in expected_streams
            ):
                _fail(f'invocation record does not match evidence filenames: {stem}')
            JobEvidence(
                evidence_root_for_job(self.job_directory), run_id
            ).record_finalized(manifest, 'invocation_record', replace_existing=False)
            for expected, _declared, kind in expected_streams:
                if not expected.is_file():
                    _fail(f'completed invocation stream is missing: {expected.name}')
                JobEvidence(
                    evidence_root_for_job(self.job_directory), run_id
                ).record_finalized(
                    expected, cast('EvidenceType', kind), replace_existing=False
                )


def prepare_run_evidence_directory(runs_directory: Path, run_id: str) -> Path:
    """Resolve one run directory while preserving the worker error contract."""

    try:
        path = resolve_evidence_path(runs_directory, run_id)
        path.mkdir(parents=True, exist_ok=True)
        JobEvidence(runs_directory, run_id).recover_index()
        InvocationEvidenceStore(path).recover_completed(run_id)
        return path
    except EvidencePathError as error:
        raise WorkerError(str(error)) from error


def persist_attempt_record(path: Path, record: InvocationRecord) -> None:
    """Normalize unsafe or conflicting attempt writes as worker failures."""

    try:
        job_directory = path.parent.parent
        InvocationEvidenceStore(job_directory).write(path, record)
        if record.status == 'completed':
            record_finalized_path(Path(record.stdout_path), 'process_stdout')
            record_finalized_path(Path(record.stderr_path), 'process_stderr')
    except InvocationEvidenceError as error:
        code = (
            RESUME_ACTIVATION_UNCERTAIN_CODE
            if str(error) == 'attempt record already exists'
            else None
        )
        raise WorkerError(f'invalid invocation evidence: {error}', code=code) from error


def record_invocation(
    attempt: AttemptIdentity,
    outcome: ProcessOutcome,
    *,
    run_directory: Path,
    lifecycle: AttemptLifecycle | None = None,
) -> str:
    """Persist separate streams and their adapter-neutral invocation record."""

    resolved = (lifecycle or AttemptLifecycle()).resolved(outcome)
    stem = attempt.evidence_stem
    logs = run_evidence_path(run_directory, 'logs')
    stdout_path = logs / f'{stem}.stdout.log'
    stderr_path = logs / f'{stem}.stderr.log'
    if outcome.stdout is not None or not stdout_path.exists():
        write_text_atomic(stdout_path, output_text(outcome.stdout))
    if outcome.stderr is not None or not stderr_path.exists():
        write_text_atomic(stderr_path, output_text(outcome.stderr))
    persist_attempt_record(
        run_evidence_path(run_directory, 'invocations') / f'{stem}.json',
        InvocationRecord(
            schema_version=attempt.schema_version,
            run_id=attempt.run_id,
            task_id=attempt.task_id,
            invocation_id=attempt.durable_invocation_id,
            role=attempt.role,
            agent_vendor=attempt.agent.vendor,
            requested_model=attempt.agent.model,
            effective_models=outcome.effective_models,
            effective_model_status=outcome.effective_model_status,
            runtime=attempt.agent.runtime,
            iteration=attempt.iteration,
            started_at=outcome.started_at,
            finished_at=(
                (outcome.finished_at or timestamp()) if outcome.finished else None
            ),
            exit_code=outcome.exit_code,
            timed_out=outcome.timed_out,
            interrupted=outcome.interrupted,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            attempt=attempt.attempt,
            status=resolved.status,
            conclusion=resolved.conclusion,
            response_received_at=resolved.response_received_at,
            validation_started_at=resolved.validation_started_at,
            reviewer_id=attempt.reviewer_id,
        ),
    )
    return attempt.durable_invocation_id


def latest_task_attempt(
    run_directory: Path, sequence: int, role: str
) -> InvocationRecord | None:
    """Return the latest validated attempt for one durable task."""

    try:
        records = InvocationEvidenceStore(run_directory).read_all(run_directory.name)
    except InvocationEvidenceError as error:
        message = 'invalid invocation evidence'
        raise WorkerError(message) from error
    task_id = f'{run_directory.name}:{sequence:06d}-{role}'
    attempts = [record for record in records if record.task_id == task_id]
    return max(attempts, key=lambda record: record.attempt) if attempts else None


def attempt_activation_was_persisted(
    run_directory: Path,
    sequence: int,
    role: Literal[RuntimeRole.DEVELOPER, RuntimeRole.REVIEWER],
    attempt: int,
) -> bool:
    """Return whether activation is durable enough to finalize interruption."""

    latest = latest_task_attempt(run_directory, sequence, role)
    return (
        latest is not None
        and latest.attempt == attempt
        and latest.status == AttemptStatus.RUNNING.value
    )


def next_attempt(
    run_directory: Path, sequence: int, role: str, workflow_state: RunState
) -> int:
    """Return the next non-overwriting invocation attempt number."""

    latest = latest_task_attempt(run_directory, sequence, role)
    action = recovery_action(
        latest,
        workflow_state=workflow_state,
    )
    if action is RecoveryAction.LAUNCH:
        return 1
    if action is not RecoveryAction.NONE or latest is None:
        message = 'cannot retry task with uncertain active attempt'
        raise WorkerError(
            message,
            code=RESUME_ACTIVATION_UNCERTAIN_CODE,
        )
    return latest.attempt + 1


def attempt_record_path(
    run_directory: Path, sequence: int, role: str, attempt: int
) -> Path:
    """Return the durable record path for one task attempt."""

    return run_evidence_path(
        run_directory,
        'invocations',
        f'{invocation_stem(sequence, role, attempt)}.json',
    )
