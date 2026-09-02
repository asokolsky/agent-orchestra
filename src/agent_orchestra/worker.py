"""Execute one durable, bounded local review step."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from agent_orchestra.models import (
    REVIEW_FINDING_FIELDS,
    Run,
    RunState,
    same_diff_digest,
    utc_now,
)
from agent_orchestra.workflow import transition

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

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
WORKTREE_CHANGED = 'worktree changed during read-only review'
EVIDENCE_INSIDE_WORKTREE = 'run evidence directory must be outside the worktree'


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

    expected_keys = {
        'schema_version',
        'message_id',
        'in_reply_to',
        'run_id',
        'sequence',
        'iteration',
        'message_type',
        'sender',
        'recipient',
        'created_at',
        'scope',
        'payload',
    }
    if set(document) != expected_keys:
        raise WorkerError(INVALID_ENVELOPE)
    expected_values = {
        'schema_version': 1,
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': 2,
        'iteration': request['iteration'],
        'message_type': 'review_result',
        'sender': 'reviewer',
        'recipient': 'orchestrator',
        'scope': request['scope'],
    }
    for key, expected in expected_values.items():
        if document[key] != expected:
            raise WorkerError(f'reviewer response has invalid {key}')
    try:
        UUID(str(document['message_id']))
        created_at = datetime.fromisoformat(str(document['created_at']))
    except ValueError as error:
        raise WorkerError(INVALID_IDENTITY) from error
    if created_at.tzinfo is None or created_at.utcoffset() != UTC.utcoffset(created_at):
        raise WorkerError(INVALID_IDENTITY)

    payload = document['payload']
    payload_keys = {
        'verdict',
        'summary',
        'findings',
        'validation',
        'verification_gaps',
        'artifact_path',
    }
    if not isinstance(payload, dict) or set(payload) != payload_keys:
        raise WorkerError(INVALID_PAYLOAD)
    if not isinstance(payload['summary'], str):
        raise WorkerError(INVALID_PAYLOAD)
    for key in ('findings', 'validation', 'verification_gaps'):
        if not isinstance(payload[key], list):
            raise WorkerError(INVALID_PAYLOAD)
    if any(
        not isinstance(finding, dict) or set(finding) != REVIEW_FINDING_FIELDS
        for finding in payload['findings']
    ):
        raise WorkerError(INVALID_PAYLOAD)
    verdict = payload['verdict']
    if verdict not in {'approved', 'changes_requested', 'blocked'}:
        raise WorkerError(INVALID_VERDICT)
    if payload['artifact_path'] != str(artifact_path):
        raise WorkerError(INVALID_ARTIFACT_PATH)
    if not artifact_path.is_file():
        raise WorkerError(MISSING_ARTIFACT)
    if verdict == 'approved' and payload['findings']:
        raise WorkerError(APPROVED_WITH_FINDINGS)
    return str(verdict)


def run_queued_review(
    *,
    store: RunStore,
    run: Run,
    objective: str,
    reviewer_command: Sequence[str],
    runs_directory: Path,
    timeout_seconds: int,
    digest_worktree: Callable[[Path, str], str | None],
) -> Run:
    """Consume one queued local run through its first review decision."""

    if run.state is not RunState.QUEUED:
        raise WorkerError(f'run must be queued, found {run.state}')
    if not objective.strip():
        raise WorkerError(EMPTY_OBJECTIVE)
    if not reviewer_command:
        raise WorkerError(EMPTY_COMMAND)
    if timeout_seconds <= 0:
        message = 'timeout must be positive'
        raise WorkerError(message)
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
    reviewing = transition(prepared, RunState.AWAITING_REVIEW)
    store.update(reviewing, expected_state=RunState.PREPARING)

    messages = run_directory / 'messages'
    artifacts = run_directory / 'artifacts'
    logs = run_directory / 'logs'
    artifact_path = artifacts / f'review-{reviewing.iteration:04d}.md'
    request_path = messages / '000001-review-request.json'
    response_path = run_directory / '.review-result.json'
    request = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': None,
        'run_id': str(run.id),
        'sequence': 1,
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
            'prior_review_path': None,
        },
    }
    _write_json_atomic(request_path, request)
    artifacts.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [*reviewer_command, str(request_path), str(response_path)],
            cwd=run.worktree_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        failed = transition(reviewing, RunState.FAILED)
        store.update(failed, expected_state=RunState.AWAITING_REVIEW)
        raise WorkerError(
            f'reviewer timed out after {timeout_seconds} seconds'
        ) from error
    except OSError as error:
        failed = transition(reviewing, RunState.FAILED)
        store.update(failed, expected_state=RunState.AWAITING_REVIEW)
        raise WorkerError(f'cannot execute reviewer: {error}') from error
    (logs / 'reviewer.stdout.log').write_text(completed.stdout, encoding='utf-8')
    (logs / 'reviewer.stderr.log').write_text(completed.stderr, encoding='utf-8')
    if completed.returncode != 0:
        failed = transition(reviewing, RunState.FAILED)
        store.update(failed, expected_state=RunState.AWAITING_REVIEW)
        raise WorkerError(f'reviewer exited with code {completed.returncode}')

    response_valid = False
    try:
        response = _read_object(response_path)
        verdict = _validate_review_response(
            response, request=request, artifact_path=artifact_path
        )
        _require_unchanged(
            _digest(digest_worktree, run.worktree_path, run.base_sha), current_digest
        )
        response_valid = True
    except WorkerError:
        failed = transition(reviewing, RunState.FAILED)
        store.update(failed, expected_state=RunState.AWAITING_REVIEW)
        raise
    finally:
        if response_path.exists():
            destination = (
                messages / '000002-review-result.json'
                if response_valid
                else logs / 'rejected-review-result.json'
            )
            response_path.replace(destination)

    if verdict == 'blocked':
        return reviewing
    decided = transition(
        reviewing,
        RunState.APPROVED if verdict == 'approved' else RunState.CHANGES_REQUESTED,
    )
    store.update(decided, expected_state=RunState.AWAITING_REVIEW)
    if verdict == 'changes_requested':
        return decided
    awaiting_authorization = transition(decided, RunState.AWAITING_COMMIT_AUTHORIZATION)
    store.update(awaiting_authorization, expected_state=RunState.APPROVED)
    return awaiting_authorization
