"""Execute one durable, bounded local review step."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import ValidationError

from agent_orchestra.agents import (
    CommandAgentAdapter,
    DeveloperRequest,
    ReviewerRequest,
)
from agent_orchestra.models import Run, RunState, same_diff_digest, utc_now
from agent_orchestra.schemas import (
    CHANGES_REQUESTED_WITHOUT_FINDINGS,
    DUPLICATE_REVIEW_FINDING_IDS,
    DeveloperHandoffMessageSchema,
    RemediationRequestMessageSchema,
    ReviewRequestMessageSchema,
    ReviewResultMessageSchema,
)
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agent_orchestra.store import RunStore


class WorkerError(RuntimeError):
    """Raised when a queued run cannot complete its review step."""


NOT_OBJECT = 'reviewer response must be a JSON object'
INVALID_ENVELOPE = 'reviewer response has missing or unknown envelope fields'
INVALID_IDENTITY = 'reviewer response has an invalid identity or timestamp'
INVALID_PAYLOAD = 'reviewer response has invalid payload fields'
INVALID_VERDICT = 'reviewer response has invalid verdict'
INVALID_ARTIFACT_PATH = 'reviewer response has invalid artifact_path'
MISSING_ARTIFACT = 'reviewer did not create the requested artifact'
APPROVED_WITH_FINDINGS = 'approved review cannot contain findings'
EMPTY_OBJECTIVE = 'objective must not be empty'
EMPTY_COMMAND = 'reviewer command must not be empty'
NO_CHANGES = 'worktree has no local changes'
NO_REMEDIATION_CHANGE = 'developer handoff did not produce a new diff digest'
DEVELOPER_DISAGREEMENT = 'developer disputed every finding without changing the diff'
ITERATION_LIMIT = 'maximum review iteration count exhausted'
INVALID_DEVELOPER_TIMEOUT = 'developer timeout must be positive'
INVALID_ITERATION_LIMIT = 'maximum review iterations must be positive'
WORKTREE_CHANGED = 'worktree changed during read-only review'
EVIDENCE_INSIDE_WORKTREE = 'run evidence directory must be outside the worktree'
INVALID_DEVELOPER_HANDOFF = 'developer handoff is invalid'
INVALID_FINDING_DISPOSITIONS = (
    'developer handoff must contain exactly one disposition for every finding'
)
INVALID_REMEDIATION_REQUEST = 'remediation request is invalid'
REMEDIATION_PATH_ESCAPE = 'remediation request references evidence outside the run'
REMEDIATION_ACTIONS = 'remediation request must not authorize lifecycle actions'
INVALID_REVIEW_REQUEST = 'review request is invalid'
REVIEW_PATH_ESCAPE = 'review request references evidence outside the run'
DUPLICATE_MESSAGE_ID = 'message ID was already persisted for this run'


def _require_unchanged(actual: str | None, expected: str) -> None:
    """Reject a review when its worktree digest changed during execution."""

    if not same_diff_digest(actual, expected):
        raise WorkerError(WORKTREE_CHANGED)


def _digest(
    digest_worktree: Callable[[Path, str], str | None], worktree: Path, base_sha: str
) -> str | None:
    """Normalize filesystem and Git digest failures as worker errors."""

    try:
        return digest_worktree(worktree, base_sha)
    except (OSError, RuntimeError) as error:
        raise WorkerError(f'cannot compute worktree digest: {error}') from error


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Write one UTF-8 JSON document atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text_atomic(path: Path, content: str) -> None:
    """Write one UTF-8 text artifact atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_object(path: Path) -> dict[str, Any]:
    """Read a JSON object or raise a stable worker error."""

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise WorkerError(f'invalid reviewer response: {error}') from error
    if not isinstance(document, dict):
        raise WorkerError(NOT_OBJECT)
    return document


def _validate_review_response(
    document: dict[str, Any], *, request: dict[str, Any], artifact_path: Path
) -> str:
    """Validate response correlation and return its verdict."""

    try:
        parsed = ReviewResultMessageSchema.model_validate(document)
    except ValidationError as error:
        details = error.errors()
        if any('cannot contain findings' in str(detail['msg']) for detail in details):
            message = APPROVED_WITH_FINDINGS
        elif any(
            CHANGES_REQUESTED_WITHOUT_FINDINGS in str(detail['msg'])
            for detail in details
        ):
            message = CHANGES_REQUESTED_WITHOUT_FINDINGS
        elif any(
            DUPLICATE_REVIEW_FINDING_IDS in str(detail['msg']) for detail in details
        ):
            message = DUPLICATE_REVIEW_FINDING_IDS
        elif any(
            detail['loc'] and detail['loc'][0] in {'message_id', 'created_at'}
            for detail in details
        ):
            message = INVALID_IDENTITY
        elif any(
            len(detail['loc']) > 1
            and detail['loc'][0] == 'payload'
            and detail['loc'][1] == 'verdict'
            for detail in details
        ):
            message = INVALID_VERDICT
        elif any(detail['loc'] and detail['loc'][0] == 'payload' for detail in details):
            message = INVALID_PAYLOAD
        else:
            message = INVALID_ENVELOPE
        raise WorkerError(message) from error
    expected_values = {
        'schema_version': 1,
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': int(request['sequence']) + 1,
        'iteration': request['iteration'],
        'message_type': 'review_result',
        'sender': 'reviewer',
        'recipient': 'orchestrator',
        'scope': request['scope'],
    }
    for key, expected in expected_values.items():
        if document[key] != expected:
            raise WorkerError(f'reviewer response has invalid {key}')
    payload = parsed.payload
    verdict = payload.verdict
    if payload.artifact_path != str(artifact_path):
        raise WorkerError(INVALID_ARTIFACT_PATH)
    if not artifact_path.is_file():
        raise WorkerError(MISSING_ARTIFACT)
    return str(verdict)


def _validate_developer_handoff(
    document: dict[str, Any], *, request: dict[str, Any], finding_ids: tuple[str, ...]
) -> DeveloperHandoffMessageSchema:
    """Validate a correlated handoff and return its canonical message."""

    try:
        parsed = DeveloperHandoffMessageSchema.model_validate(document)
    except ValidationError as error:
        raise WorkerError(INVALID_DEVELOPER_HANDOFF) from error
    expected = {
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': int(request['sequence']) + 1,
        'iteration': request['iteration'],
        'scope': request['scope'],
    }
    for field, value in expected.items():
        if document[field] != value:
            raise WorkerError(f'developer handoff has invalid {field}')
    dispositions = [item.finding_id for item in parsed.payload.dispositions]
    if len(dispositions) != len(set(dispositions)) or sorted(dispositions) != sorted(
        finding_ids
    ):
        raise WorkerError(INVALID_FINDING_DISPOSITIONS)
    return parsed


def _validate_remediation_request(
    document: dict[str, Any], *, run_directory: Path
) -> None:
    """Validate remediation authority and contained evidence references."""

    try:
        parsed = RemediationRequestMessageSchema.model_validate(document)
    except ValidationError as error:
        raise WorkerError(INVALID_REMEDIATION_REQUEST) from error
    if parsed.payload.allowed_actions:
        raise WorkerError(REMEDIATION_ACTIONS)
    root = run_directory.resolve()
    evidence_paths = (
        Path(parsed.payload.review_result_path).resolve(),
        Path(parsed.payload.review_artifact_path).resolve(),
    )
    if any(not path.is_relative_to(root) for path in evidence_paths):
        raise WorkerError(REMEDIATION_PATH_ESCAPE)
    if any(not path.is_file() for path in evidence_paths):
        raise WorkerError(INVALID_REMEDIATION_REQUEST)


def _validate_review_request(document: dict[str, Any], *, run_directory: Path) -> None:
    """Validate review authority and contained artifact references."""

    try:
        parsed = ReviewRequestMessageSchema.model_validate(document)
    except ValidationError as error:
        raise WorkerError(INVALID_REVIEW_REQUEST) from error
    if parsed.payload.allowed_actions:
        raise WorkerError(INVALID_REVIEW_REQUEST)
    root = run_directory.resolve()
    artifact_path = Path(parsed.payload.artifact_path).resolve()
    if not artifact_path.is_relative_to(root):
        raise WorkerError(REVIEW_PATH_ESCAPE)
    if parsed.payload.prior_review_path is not None:
        prior_path = Path(parsed.payload.prior_review_path).resolve()
        if not prior_path.is_relative_to(root):
            raise WorkerError(REVIEW_PATH_ESCAPE)
        if not prior_path.is_file():
            raise WorkerError(INVALID_REVIEW_REQUEST)


def _require_unique_message_id(document: dict[str, Any], messages: Path) -> None:
    """Reject a response identifier already present in durable messages."""

    candidate = document.get('message_id')
    for path in messages.glob('*.json'):
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except OSError, json.JSONDecodeError:
            continue
        if isinstance(existing, dict) and existing.get('message_id') == candidate:
            raise WorkerError(DUPLICATE_MESSAGE_ID)


def _require_remediation_progress(
    status: str, new_digest: str | None, current_digest: str
) -> str:
    """Require a ready handoff and return its materially changed digest."""

    if status != 'ready_for_review':
        raise WorkerError(f'developer returned {status}')
    if new_digest is None or same_diff_digest(new_digest, current_digest):
        raise WorkerError(NO_REMEDIATION_CHANGE)
    return new_digest


def _is_developer_disagreement(message: DeveloperHandoffMessageSchema) -> bool:
    """Return whether every finding was rejected or blocked without an edit."""

    dispositions = message.payload.dispositions
    return bool(dispositions) and all(
        item.disposition in {'rejected', 'blocked'} for item in dispositions
    )


def _run_queued_review(
    *,
    store: RunStore,
    run: Run,
    objective: str,
    reviewer_command: Sequence[str],
    developer_command: Sequence[str],
    runs_directory: Path,
    timeout_seconds: int,
    developer_timeout_seconds: int | None = None,
    max_iterations: int = 3,
    digest_worktree: Callable[[Path, str], str | None],
) -> Run:
    """Consume one queued local run through a bounded review-remediation loop."""

    if run.state is not RunState.QUEUED:
        raise WorkerError(f'run must be queued, found {run.state}')
    if not objective.strip():
        raise WorkerError(EMPTY_OBJECTIVE)
    if not reviewer_command:
        raise WorkerError(EMPTY_COMMAND)
    if timeout_seconds <= 0:
        message = 'timeout must be positive'
        raise WorkerError(message)
    if developer_timeout_seconds is None:
        developer_timeout_seconds = timeout_seconds
    if developer_timeout_seconds <= 0:
        raise WorkerError(INVALID_DEVELOPER_TIMEOUT)
    if max_iterations <= 0:
        raise WorkerError(INVALID_ITERATION_LIMIT)
    if not run.worktree_path.is_dir():
        raise WorkerError(f'worktree not found: {run.worktree_path}')

    run_directory = runs_directory.expanduser().resolve() / str(run.id)
    if run_directory.is_relative_to(run.worktree_path.resolve()):
        raise WorkerError(EVIDENCE_INSIDE_WORKTREE)

    current_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
    if current_digest is None:
        raise WorkerError(NO_CHANGES)
    prepared = replace(
        transition(run, RunState.PREPARING),
        diff_digest=current_digest,
        updated_at=utc_now(),
    )
    store.update(prepared, expected_state=RunState.QUEUED)
    messages = run_directory / 'messages'
    artifacts = run_directory / 'artifacts'
    logs = run_directory / 'logs'
    _write_json_atomic(
        run_directory / 'execution.json',
        {
            'schema_version': 1,
            'reviewer_command': list(reviewer_command),
            'developer_command': list(developer_command),
            'reviewer_timeout_seconds': timeout_seconds,
            'developer_timeout_seconds': developer_timeout_seconds,
            'max_review_iterations': max_iterations,
        },
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    reviewing = transition(prepared, RunState.REVIEWING)
    store.update(reviewing, expected_state=RunState.PREPARING)
    sequence = 1
    prior_review_path: Path | None = None
    reviewer_adapter = CommandAgentAdapter(tuple(reviewer_command))
    developer_adapter = CommandAgentAdapter(tuple(developer_command))

    while True:
        artifact_path = artifacts / f'review-{reviewing.iteration:04d}.md'
        request_path = messages / f'{sequence:06d}-review-request.json'
        response_path = run_directory / '.review-result.json'
        request: dict[str, Any] = {
            'schema_version': 1,
            'message_id': str(uuid4()),
            'in_reply_to': None,
            'run_id': str(run.id),
            'sequence': sequence,
            'iteration': reviewing.iteration,
            'message_type': 'review_request',
            'sender': 'orchestrator',
            'recipient': 'reviewer',
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
            'scope': {
                'worktree_path': str(run.worktree_path),
                'base_sha': run.base_sha,
                'head_sha': run.head_sha,
                'diff_digest': current_digest,
            },
            'payload': {
                'objective': objective,
                'allowed_actions': [],
                'timeout_seconds': timeout_seconds,
                'artifact_path': str(artifact_path),
                'prior_review_path': (
                    str(prior_review_path) if prior_review_path is not None else None
                ),
            },
        }
        _validate_review_request(request, run_directory=run_directory)
        _write_json_atomic(request_path, request)
        try:
            completed = reviewer_adapter.execute(
                ReviewerRequest(
                    objective=objective,
                    worktree_path=run.worktree_path,
                    iteration=reviewing.iteration,
                    allowed_actions=(),
                    timeout_seconds=timeout_seconds,
                    base_sha=run.base_sha,
                    head_sha=run.head_sha,
                    diff_digest=current_digest,
                    artifact_path=artifact_path,
                    request_path=request_path,
                    response_path=response_path,
                )
            )
        except subprocess.TimeoutExpired as error:
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise WorkerError(
                f'reviewer timed out after {timeout_seconds} seconds'
            ) from error
        except OSError as error:
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise WorkerError(f'cannot execute reviewer: {error}') from error
        log_stem = f'{sequence:06d}-reviewer'
        _write_text_atomic(logs / f'{log_stem}.stdout.log', completed.stdout)
        _write_text_atomic(logs / f'{log_stem}.stderr.log', completed.stderr)
        if not completed.succeeded:
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise WorkerError(f'reviewer exited with code {completed.exit_code}')

        response_valid = False
        review_result_path = messages / f'{sequence + 1:06d}-review-result.json'
        try:
            response = _read_object(response_path)
            _require_unique_message_id(response, messages)
            verdict = _validate_review_response(
                response, request=request, artifact_path=artifact_path
            )
            _require_unchanged(
                _digest(digest_worktree, run.worktree_path, run.base_sha),
                current_digest,
            )
            response_valid = True
        except WorkerError:
            failed = transition(reviewing, RunState.FAILED)
            store.update(failed, expected_state=RunState.REVIEWING)
            raise
        finally:
            if response_path.exists():
                destination = (
                    review_result_path
                    if response_valid
                    else logs / f'{sequence + 1:06d}-rejected-review-result.json'
                )
                response_path.replace(destination)

        if verdict == 'blocked':
            return reviewing
        decided = transition(
            reviewing,
            RunState.APPROVED if verdict == 'approved' else RunState.CHANGES_REQUESTED,
        )
        store.update(decided, expected_state=RunState.REVIEWING)
        if verdict == 'approved':
            awaiting = transition(decided, RunState.AWAITING_COMMIT_AUTHORIZATION)
            store.update(awaiting, expected_state=RunState.APPROVED)
            return awaiting
        if reviewing.iteration >= max_iterations:
            failed = transition(decided, RunState.FAILED)
            store.update(failed, expected_state=RunState.CHANGES_REQUESTED)
            raise WorkerError(ITERATION_LIMIT)
        if not developer_command:
            return decided

        developing = transition(decided, RunState.DEVELOPING)
        store.update(developing, expected_state=RunState.CHANGES_REQUESTED)
        sequence += 2
        remediation_path = messages / f'{sequence:06d}-remediation-request.json'
        handoff_temporary = run_directory / '.developer-handoff.json'
        remediation: dict[str, Any] = {
            'schema_version': 1,
            'message_id': str(uuid4()),
            'in_reply_to': response['message_id'],
            'run_id': str(run.id),
            'sequence': sequence,
            'iteration': reviewing.iteration,
            'message_type': 'remediation_request',
            'sender': 'orchestrator',
            'recipient': 'developer',
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
            'scope': request['scope'],
            'payload': {
                'objective': objective,
                'allowed_actions': [],
                'timeout_seconds': developer_timeout_seconds,
                'review_result_path': str(review_result_path),
                'review_artifact_path': str(artifact_path),
            },
        }
        _validate_remediation_request(remediation, run_directory=run_directory)
        _write_json_atomic(remediation_path, remediation)
        try:
            completed = developer_adapter.execute(
                DeveloperRequest(
                    objective=objective,
                    worktree_path=run.worktree_path,
                    iteration=reviewing.iteration,
                    allowed_actions=(),
                    timeout_seconds=developer_timeout_seconds,
                    request_path=remediation_path,
                    response_path=handoff_temporary,
                )
            )
        except subprocess.TimeoutExpired as error:
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise WorkerError(
                f'developer timed out after {developer_timeout_seconds} seconds'
            ) from error
        except OSError as error:
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise WorkerError(f'cannot execute developer: {error}') from error
        log_stem = f'{sequence:06d}-developer'
        _write_text_atomic(logs / f'{log_stem}.stdout.log', completed.stdout)
        _write_text_atomic(logs / f'{log_stem}.stderr.log', completed.stderr)
        if not completed.succeeded:
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise WorkerError(f'developer exited with code {completed.exit_code}')
        handoff_valid = False
        handoff_path = messages / f'{sequence + 1:06d}-developer-handoff.json'
        finding_ids = tuple(
            finding['finding_id'] for finding in response['payload']['findings']
        )
        try:
            handoff = _read_object(handoff_temporary)
            _require_unique_message_id(handoff, messages)
            parsed_handoff = _validate_developer_handoff(
                handoff, request=remediation, finding_ids=finding_ids
            )
            handoff_digest = _digest(digest_worktree, run.worktree_path, run.base_sha)
            if (
                parsed_handoff.payload.status == 'ready_for_review'
                and handoff_digest is not None
                and same_diff_digest(handoff_digest, current_digest)
                and _is_developer_disagreement(parsed_handoff)
            ):
                handoff_valid = True
                disagreement = transition(developing, RunState.CHANGES_REQUESTED)
                store.update(disagreement, expected_state=RunState.DEVELOPING)
                _write_json_atomic(
                    run_directory / 'decision-required.json',
                    {
                        'schema_version': 1,
                        'run_id': str(run.id),
                        'state': str(disagreement.state),
                        'reason': {
                            'code': 'developer_disagreement',
                            'message': DEVELOPER_DISAGREEMENT,
                        },
                        'developer_handoff_path': str(handoff_path),
                        'created_at': datetime.now(UTC)
                        .isoformat()
                        .replace('+00:00', 'Z'),
                    },
                )
                return disagreement
            new_digest = _require_remediation_progress(
                parsed_handoff.payload.status, handoff_digest, current_digest
            )
            handoff_valid = True
        except WorkerError:
            failed = transition(developing, RunState.FAILED)
            store.update(failed, expected_state=RunState.DEVELOPING)
            raise
        finally:
            if handoff_temporary.exists():
                destination = (
                    handoff_path
                    if handoff_valid
                    else logs / f'{sequence + 1:06d}-rejected-developer-handoff.json'
                )
                handoff_temporary.replace(destination)
        current_digest = new_digest
        prior_review_path = review_result_path
        reviewing = replace(
            transition(developing, RunState.REVIEWING),
            diff_digest=current_digest,
            updated_at=utc_now(),
        )
        store.update(reviewing, expected_state=RunState.DEVELOPING)
        sequence += 2


def run_queued_review(
    *,
    store: RunStore,
    run: Run,
    objective: str,
    reviewer_command: Sequence[str],
    developer_command: Sequence[str],
    runs_directory: Path,
    timeout_seconds: int,
    developer_timeout_seconds: int | None = None,
    max_iterations: int = 3,
    digest_worktree: Callable[[Path, str], str | None],
) -> Run:
    """Run the bounded loop and persist every worker failure as durable evidence."""

    run_directory = runs_directory.expanduser().resolve() / str(run.id)
    try:
        return _run_queued_review(
            store=store,
            run=run,
            objective=objective,
            reviewer_command=reviewer_command,
            developer_command=developer_command,
            runs_directory=runs_directory,
            timeout_seconds=timeout_seconds,
            developer_timeout_seconds=developer_timeout_seconds,
            max_iterations=max_iterations,
            digest_worktree=digest_worktree,
        )
    except WorkerError as error:
        if not run_directory.is_relative_to(run.worktree_path.resolve()):
            try:
                durable_run = store.get(str(run.id))
                _write_json_atomic(
                    run_directory / 'failure.json',
                    {
                        'schema_version': 1,
                        'run_id': str(run.id),
                        'state': str(durable_run.state),
                        'error': {
                            'code': 'worker_error',
                            'message': str(error),
                        },
                        'created_at': datetime.now(UTC)
                        .isoformat()
                        .replace('+00:00', 'Z'),
                    },
                )
            except OSError:
                pass
        raise
