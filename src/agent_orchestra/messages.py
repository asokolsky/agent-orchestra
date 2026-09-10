"""
Read and validate the canonical messages that carry one workflow.

A workflow's durable state is its ordered message chain on disk. This module
reads that chain and validates each document against its schema, correlation,
and containment rules. It decides whether a document is acceptable; it does not
decide what the workflow does next, which belongs with the worker.

The schemas themselves are pure and live in `schemas.py`. These functions add
what a schema cannot check on its own: that a message correlates with the run
and iteration that produced it, that its evidence paths stay inside the run
directory, and that message identifiers are not reused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from agent_orchestra.evidence import (
    WorkerError,
    contained_job_reference,
    read_json_object,
)
from agent_orchestra.manifests import canonical_message_evidence
from agent_orchestra.models import same_diff_digest
from agent_orchestra.reviewer_paths import ReviewerIdentityError, validate_reviewer_id

if TYPE_CHECKING:
    from agent_orchestra.review_batch import ReviewerDispatchResult

from agent_orchestra.schemas import (
    CHANGES_REQUESTED_WITHOUT_FINDINGS,
    DUPLICATE_REVIEW_FINDING_IDS,
    DeveloperHandoffMessageSchema,
    RemediationRequestMessageSchema,
    ReviewRequestMessageSchema,
    ReviewResultMessageSchema,
)

NO_CHANGES = 'worktree has no local changes'
APPROVED_WITH_FINDINGS = 'approved review cannot contain findings'
DUPLICATE_MESSAGE_ID = 'message ID was already persisted for this run'
INCOMPLETE_REVIEWER_MESSAGE_BATCH = 'reviewer message sequence is not complete'
INVALID_ARTIFACT_PATH = 'reviewer response has invalid artifact_path'
INVALID_DEVELOPER_HANDOFF = 'developer handoff is invalid'
INVALID_ENVELOPE = 'reviewer response has missing or unknown envelope fields'
INVALID_FINDING_DISPOSITIONS = (
    'developer handoff must contain exactly one disposition for every finding'
)
INVALID_IDENTITY = 'reviewer response has an invalid identity or timestamp'
INVALID_PAYLOAD = 'reviewer response has invalid payload fields'
INVALID_REMEDIATION_REQUEST = 'remediation request is invalid'
INVALID_REVIEW_REQUEST = 'review request is invalid'
INVALID_VERDICT = 'reviewer response has invalid verdict'
MISSING_ARTIFACT = 'reviewer did not create the requested artifact'
MIXED_REVIEWER_MESSAGE_PATHS = 'canonical messages mix reviewer batch and legacy paths'
NO_REMEDIATION_CHANGE = 'developer handoff did not produce a new diff digest'
REMEDIATION_ACTIONS = 'remediation request must not authorize lifecycle actions'
REMEDIATION_PATH_ESCAPE = 'remediation request references evidence outside the run'
REVIEW_PATH_ESCAPE = 'review request references evidence outside the run'
SMALL_REVIEWER_MESSAGE_BATCH = 'reviewer message batch requires at least two reviewers'


def require_unique_batch_message_ids(
    results: tuple[ReviewerDispatchResult, ...],
) -> None:
    """Reject a reviewer batch that reused a canonical response identity."""

    message_ids = [result.message_id for result in results if result.message_id]
    if len(message_ids) != len(set(message_ids)):
        raise WorkerError(DUPLICATE_MESSAGE_ID)


def validate_review_response(
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


def validate_developer_handoff(
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


def validate_remediation_request(
    document: dict[str, Any], *, run_directory: Path
) -> None:
    """Validate remediation authority and contained evidence references."""

    try:
        parsed = RemediationRequestMessageSchema.model_validate(document)
    except ValidationError as error:
        raise WorkerError(INVALID_REMEDIATION_REQUEST) from error
    if parsed.payload.allowed_actions:
        raise WorkerError(REMEDIATION_ACTIONS)
    evidence_paths = (
        contained_job_reference(
            run_directory, parsed.payload.review_result_path, REMEDIATION_PATH_ESCAPE
        ),
        contained_job_reference(
            run_directory, parsed.payload.review_artifact_path, REMEDIATION_PATH_ESCAPE
        ),
    )
    if any(not path.is_file() for path in evidence_paths):
        raise WorkerError(INVALID_REMEDIATION_REQUEST)


def validate_review_request(document: dict[str, Any], *, run_directory: Path) -> None:
    """Validate review authority and contained artifact references."""

    try:
        parsed = ReviewRequestMessageSchema.model_validate(document)
    except ValidationError as error:
        raise WorkerError(INVALID_REVIEW_REQUEST) from error
    if parsed.payload.allowed_actions:
        raise WorkerError(INVALID_REVIEW_REQUEST)
    contained_job_reference(
        run_directory, parsed.payload.artifact_path, REVIEW_PATH_ESCAPE
    )
    if parsed.payload.prior_review_path is not None:
        prior_path = contained_job_reference(
            run_directory, parsed.payload.prior_review_path, REVIEW_PATH_ESCAPE
        )
        if not prior_path.is_file():
            raise WorkerError(INVALID_REVIEW_REQUEST)


def require_unique_message_id(document: dict[str, Any], run_directory: Path) -> None:
    """Reject a response identifier already present in durable messages."""

    candidate = document.get('message_id')
    for path in run_directory.rglob('*.json'):
        relative = path.relative_to(run_directory).as_posix()
        if canonical_message_evidence(relative) is None:
            continue
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except OSError, json.JSONDecodeError:
            continue
        if isinstance(existing, dict) and existing.get('message_id') == candidate:
            raise WorkerError(DUPLICATE_MESSAGE_ID)


def classify_remediation_progress(
    status: str, new_digest: str | None, current_digest: str
) -> tuple[bool, str]:
    """Return whether a valid handoff is recoverable and its measured digest."""

    if new_digest is None:
        raise WorkerError(NO_CHANGES)
    if status in {'blocked', 'failed'}:
        return True, new_digest
    if same_diff_digest(new_digest, current_digest):
        raise WorkerError(NO_REMEDIATION_CHANGE)
    return False, new_digest


def validate_resumed_progress(
    status: str,
    new_digest: str | None,
    current_digest: str,
    *,
    allow_unchanged_ready: bool,
    is_disagreement: bool,
) -> tuple[bool, str]:
    """Validate a retried handoff and return its recovery classification."""

    if new_digest is None:
        raise WorkerError(NO_CHANGES)
    if status in {'blocked', 'failed'}:
        return True, new_digest
    if (
        not allow_unchanged_ready
        and not is_disagreement
        and same_diff_digest(new_digest, current_digest)
    ):
        raise WorkerError(NO_REMEDIATION_CHANGE)
    return False, new_digest


def is_developer_disagreement(message: DeveloperHandoffMessageSchema) -> bool:
    """Return whether every finding was rejected or blocked without an edit."""

    dispositions = message.payload.dispositions
    return bool(dispositions) and all(
        item.disposition in {'rejected', 'blocked'} for item in dispositions
    )


def read_message_chain(
    run_directory: Path, run_id: str
) -> list[tuple[Path, dict[str, Any]]]:
    """Read and correlate every canonical message for recovery."""

    documents: list[tuple[Path, dict[str, Any]]] = []
    identities: dict[str, tuple[Path, dict[str, Any]]] = {}
    schemas: dict[str, type[BaseModel]] = {
        'review_request': ReviewRequestMessageSchema,
        'review_result': ReviewResultMessageSchema,
        'remediation_request': RemediationRequestMessageSchema,
        'developer_handoff': DeveloperHandoffMessageSchema,
    }
    candidates: list[tuple[int, str, Path]] = []
    message_directory = run_directory / 'messages'
    unsafe_namespace = 'resume message namespace is unsafe'
    if message_directory.is_symlink():
        raise WorkerError(unsafe_namespace)
    if message_directory.exists() and not message_directory.is_dir():
        raise WorkerError(unsafe_namespace)
    if message_directory.is_dir():
        try:
            with os.scandir(message_directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            message = 'resume message namespace is unreadable'
            raise WorkerError(message) from error
        for entry in entries:
            path = Path(entry.path)
            relative = path.relative_to(run_directory).as_posix()
            try:
                unsafe_entry = entry.is_symlink() or not entry.is_file(
                    follow_symlinks=False
                )
            except OSError as error:
                message = f'unreadable canonical message path: {relative}'
                raise WorkerError(message) from error
            if unsafe_entry:
                raise WorkerError(f'unsafe canonical message path: {relative}')
            identity = canonical_message_evidence(relative)
            if identity is None:
                raise WorkerError(f'unknown canonical message path: {relative}')
            path_message_type, sequence = identity
            candidates.append((sequence, path_message_type, path))
    reviewer_ids = {
        reviewer_id
        for sequence, message_type, path in candidates
        if (
            reviewer_id := reviewer_id_from_message_path(
                path, sequence=sequence, message_type=message_type
            )
        )
        is not None
    }
    if reviewer_ids:
        return read_reviewer_message_batch(
            candidates,
            reviewer_ids=reviewer_ids,
            run_directory=run_directory,
            run_id=run_id,
            schemas=schemas,
        )
    for expected_sequence, (sequence, expected_type, path) in enumerate(
        sorted(candidates), start=1
    ):
        contained_job_reference(
            run_directory, path, 'resume message path escapes the run directory'
        )
        if sequence != expected_sequence:
            message = 'resume message sequence is not contiguous'
            raise WorkerError(message)
        document = read_json_object(path)
        message_type = document.get('message_type')
        if not isinstance(message_type, str) or message_type != expected_type:
            raise WorkerError(f'invalid canonical message: {path.name}')
        schema = schemas.get(message_type)
        if schema is None:
            raise WorkerError(f'unsupported recoverable message type: {message_type}')
        try:
            schema.model_validate(document)
        except ValidationError as error:
            raise WorkerError(f'invalid canonical message: {path.name}') from error
        if document['run_id'] != run_id or document['sequence'] != sequence:
            raise WorkerError(f'message does not match recoverable run: {path.name}')
        parent_id = document['in_reply_to']
        parent_entry = identities.get(parent_id) if parent_id is not None else None
        parent = parent_entry[1] if parent_entry is not None else None
        previous = documents[-1][1] if documents else None
        if document['message_type'] == 'review_request':
            if previous is None:
                if (
                    parent_id is not None
                    or document['payload']['prior_review_path'] is not None
                ):
                    raise WorkerError(f'invalid message correlation: {path.name}')
            elif (
                parent_id is not None or previous['message_type'] != 'developer_handoff'
            ):
                raise WorkerError(f'invalid message correlation: {path.name}')
            else:
                remediation_entry = identities.get(previous['in_reply_to'])
                remediation = (
                    remediation_entry[1] if remediation_entry is not None else None
                )
                review_entry = (
                    identities.get(remediation['in_reply_to'])
                    if remediation is not None
                    else None
                )
                if (
                    remediation is None
                    or remediation['message_type'] != 'remediation_request'
                    or review_entry is None
                    or review_entry[1]['message_type'] != 'review_result'
                    or document['payload']['prior_review_path'] != str(review_entry[0])
                ):
                    raise WorkerError(f'invalid message correlation: {path.name}')
        elif document['message_type'] == 'review_result':
            if (
                parent is None
                or previous is not parent
                or parent['message_type'] != 'review_request'
                or document['sequence'] != parent['sequence'] + 1
            ):
                raise WorkerError(f'invalid message correlation: {path.name}')
            artifact_path = Path(document['payload']['artifact_path'])
            if (
                document['payload']['artifact_path']
                != parent['payload']['artifact_path']
                or not artifact_path.is_file()
            ):
                raise WorkerError(f'invalid message correlation: {path.name}')
            contained_job_reference(
                run_directory,
                artifact_path,
                f'invalid message correlation: {path.name}',
            )
        elif document['message_type'] == 'remediation_request':
            prior_remediation = (
                identities.get(previous['in_reply_to'])
                if previous is not None
                else None
            )
            recovery_request = (
                previous is not None
                and previous['message_type'] == 'developer_handoff'
                and previous['payload']['status'] in {'blocked', 'failed'}
                and prior_remediation is not None
                and prior_remediation[1]['message_type'] == 'remediation_request'
                and prior_remediation[1]['in_reply_to'] == document['in_reply_to']
            )
            if (
                parent is None
                or parent_entry is None
                or parent['message_type'] != 'review_result'
                or parent['payload']['verdict'] != 'changes_requested'
                or document['payload']['review_result_path'] != str(parent_entry[0])
                or document['payload']['review_artifact_path']
                != parent['payload']['artifact_path']
                or (
                    not recovery_request
                    and (
                        previous is not parent
                        or document['sequence'] != parent['sequence'] + 1
                    )
                )
                or (
                    recovery_request
                    and previous is not None
                    and document['sequence'] != previous['sequence'] + 1
                )
            ):
                raise WorkerError(f'invalid message correlation: {path.name}')
        elif (
            parent is None
            or previous is not parent
            or parent['message_type'] != 'remediation_request'
            or document['sequence'] != parent['sequence'] + 1
        ):
            raise WorkerError(f'invalid message correlation: {path.name}')
        if parent is not None and (
            parent['run_id'] != document['run_id']
            or parent['scope'] != document['scope']
            or parent['iteration'] != document['iteration']
        ):
            raise WorkerError(f'invalid message correlation: {path.name}')
        message_id = document['message_id']
        if message_id in identities:
            raise WorkerError(DUPLICATE_MESSAGE_ID)
        identities[message_id] = (path, document)
        documents.append((path, document))
    if not documents:
        message = 'resume message chain is empty'
        raise WorkerError(message)
    return documents


def reviewer_id_from_message_path(
    path: Path, *, sequence: int, message_type: str
) -> str | None:
    """Return a reviewer qualifier from one canonical message path."""

    prefix = f'{sequence:06d}-'
    suffix = f'-{message_type.replace("_", "-")}.json'
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        raise WorkerError(f'invalid canonical message path: {name}')
    reviewer_id = name[len(prefix) : -len(suffix)]
    if not reviewer_id:
        return None
    try:
        return validate_reviewer_id(reviewer_id)
    except ReviewerIdentityError as error:
        raise WorkerError(f'invalid canonical message path: {name}') from error


def read_reviewer_message_batch(
    candidates: list[tuple[int, str, Path]],
    *,
    reviewer_ids: set[str],
    run_directory: Path,
    run_id: str,
    schemas: dict[str, type[BaseModel]],
) -> list[tuple[Path, dict[str, Any]]]:
    """Validate one initial reviewer batch as parallel correlated chains."""

    grouped: dict[str, list[tuple[int, str, Path]]] = {
        reviewer_id: [] for reviewer_id in reviewer_ids
    }
    shared: list[tuple[int, str, Path]] = []
    for sequence, message_type, path in candidates:
        reviewer_id = reviewer_id_from_message_path(
            path, sequence=sequence, message_type=message_type
        )
        if reviewer_id is None:
            shared.append((sequence, message_type, path))
        else:
            grouped[reviewer_id].append((sequence, message_type, path))
    if len(grouped) < 2:
        raise WorkerError(SMALL_REVIEWER_MESSAGE_BATCH)

    documents: list[tuple[Path, dict[str, Any]]] = []
    identities: set[str] = set()
    expected_rounds: list[tuple[int, int, int, dict[str, Any]]] | None = None
    for reviewer_id in sorted(grouped):
        chain = sorted(grouped[reviewer_id])
        if len(chain) < 2 or len(chain) % 2:
            raise WorkerError(INCOMPLETE_REVIEWER_MESSAGE_BATCH)
        reviewer_documents: list[tuple[Path, dict[str, Any]]] = []
        for sequence, message_type, path in chain:
            contained_job_reference(
                run_directory, path, 'resume message path escapes the run directory'
            )
            document = read_json_object(path)
            schema = schemas[message_type]
            try:
                schema.model_validate(document)
            except ValidationError as error:
                raise WorkerError(f'invalid canonical message: {path.name}') from error
            if (
                document['message_type'] != message_type
                or document['run_id'] != run_id
                or document['sequence'] != sequence
            ):
                raise WorkerError(
                    f'message does not match recoverable run: {path.name}'
                )
            message_id = document['message_id']
            if message_id in identities:
                raise WorkerError(DUPLICATE_MESSAGE_ID)
            identities.add(message_id)
            reviewer_documents.append((path, document))

        rounds: list[tuple[int, int, int, dict[str, Any]]] = []
        prior_result_path: Path | None = None
        for index in range(0, len(reviewer_documents), 2):
            _request_path, request = reviewer_documents[index]
            result_path, result = reviewer_documents[index + 1]
            request_sequence = int(request['sequence'])
            result_sequence = int(result['sequence'])
            if (
                request['message_type'] != 'review_request'
                or result['message_type'] != 'review_result'
                or result_sequence != request_sequence + 1
                or request['in_reply_to'] is not None
                or request['payload']['prior_review_path']
                != (str(prior_result_path) if prior_result_path is not None else None)
                or result['in_reply_to'] != request['message_id']
                or result['scope'] != request['scope']
                or result['iteration'] != request['iteration']
                or result['payload']['artifact_path']
                != request['payload']['artifact_path']
            ):
                raise WorkerError(f'invalid message correlation: {result_path.name}')
            artifact_path = Path(result['payload']['artifact_path'])
            if not artifact_path.is_file():
                raise WorkerError(f'invalid message correlation: {result_path.name}')
            contained_job_reference(
                run_directory,
                artifact_path,
                f'invalid message correlation: {result_path.name}',
            )
            rounds.append(
                (
                    request_sequence,
                    result_sequence,
                    request['iteration'],
                    request['scope'],
                )
            )
            prior_result_path = result_path
        if expected_rounds is None:
            expected_rounds = rounds
        elif rounds != expected_rounds:
            raise WorkerError(f'invalid message correlation: {chain[0][2].name}')
        documents.extend(reviewer_documents)
    assert expected_rounds is not None
    shared_documents: list[tuple[Path, dict[str, Any]]] = []
    for sequence, message_type, path in sorted(shared):
        if message_type not in {'remediation_request', 'developer_handoff'}:
            raise WorkerError(MIXED_REVIEWER_MESSAGE_PATHS)
        contained_job_reference(
            run_directory, path, 'resume message path escapes the run directory'
        )
        document = read_json_object(path)
        try:
            schemas[message_type].model_validate(document)
        except ValidationError as error:
            raise WorkerError(f'invalid canonical message: {path.name}') from error
        if document['run_id'] != run_id or document['sequence'] != sequence:
            raise WorkerError(f'message does not match recoverable run: {path.name}')
        message_id = document['message_id']
        if message_id in identities:
            raise WorkerError(DUPLICATE_MESSAGE_ID)
        identities.add(message_id)
        shared_documents.append((path, document))
    if len(shared_documents) != 2 * (len(expected_rounds) - 1):
        raise WorkerError(INCOMPLETE_REVIEWER_MESSAGE_BATCH)
    for index in range(len(expected_rounds) - 1):
        prior_request_sequence, prior_result_sequence, iteration, scope = (
            expected_rounds[index]
        )
        next_request_sequence, _, next_iteration, _ = expected_rounds[index + 1]
        _remediation_path, remediation = shared_documents[index * 2]
        handoff_path, handoff = shared_documents[index * 2 + 1]
        if (
            prior_request_sequence + 1 != prior_result_sequence
            or remediation['message_type'] != 'remediation_request'
            or remediation['sequence'] != prior_result_sequence + 1
            or remediation['iteration'] != iteration
            or remediation['scope'] != scope
            or handoff['message_type'] != 'developer_handoff'
            or handoff['sequence'] != remediation['sequence'] + 1
            or handoff['iteration'] != iteration
            or handoff['scope'] != scope
            or handoff['in_reply_to'] != remediation['message_id']
            or next_request_sequence != handoff['sequence'] + 1
            or next_iteration != iteration + 1
        ):
            raise WorkerError(f'invalid message correlation: {handoff_path.name}')
    documents.extend(shared_documents)
    return sorted(documents, key=lambda item: item[0].name)
