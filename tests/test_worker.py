"""Tests for strict local worker protocol validation."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from agent_orchestra.worker import (
    APPROVED_WITH_FINDINGS,
    INVALID_ARTIFACT_PATH,
    INVALID_ENVELOPE,
    INVALID_IDENTITY,
    INVALID_PAYLOAD,
    INVALID_VERDICT,
    MISSING_ARTIFACT,
    WORKTREE_CHANGED,
    WorkerError,
    _digest,
    _require_unchanged,
    _validate_review_response,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def review_documents(artifact_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return one valid correlated request and approved response."""

    request_id = str(uuid4())
    scope = {
        'worktree_path': '/example/worktree',
        'base_sha': 'base',
        'head_sha': 'head',
        'diff_digest': f'sha256:{"a" * 64}',
    }
    request = {
        'message_id': request_id,
        'run_id': '20260902T130000Z-a7f3c921',
        'iteration': 1,
        'scope': scope,
    }
    response = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': request_id,
        'run_id': request['run_id'],
        'sequence': 2,
        'iteration': 1,
        'message_type': 'review_result',
        'sender': 'reviewer',
        'recipient': 'orchestrator',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': scope,
        'payload': {
            'verdict': 'approved',
            'summary': 'approved',
            'findings': [],
            'validation': [],
            'verification_gaps': [],
            'artifact_path': str(artifact_path),
        },
    }
    return request, response


def add_unknown_field(document: dict[str, Any]) -> None:
    """Add an unsupported envelope field."""

    document['unknown'] = True


def break_correlation(document: dict[str, Any]) -> None:
    """Break request-response correlation."""

    document['in_reply_to'] = str(uuid4())


def break_identity(document: dict[str, Any]) -> None:
    """Replace the response identity with invalid text."""

    document['message_id'] = 'invalid'


def break_timestamp(document: dict[str, Any]) -> None:
    """Replace the timestamp with a non-UTC value."""

    document['created_at'] = '2026-09-02T13:00:00+02:00'


def add_payload_field(document: dict[str, Any]) -> None:
    """Add an unsupported payload field."""

    document['payload']['unknown'] = True


def break_findings(document: dict[str, Any]) -> None:
    """Supply an incomplete finding object."""

    document['payload']['verdict'] = 'changes_requested'
    document['payload']['findings'] = [{'title': 'incomplete'}]


def break_verdict(document: dict[str, Any]) -> None:
    """Supply an unsupported verdict."""

    document['payload']['verdict'] = 'maybe'


def break_artifact_path(document: dict[str, Any]) -> None:
    """Point the response at a different artifact."""

    document['payload']['artifact_path'] = '/example/elsewhere.md'


def approve_with_findings(document: dict[str, Any]) -> None:
    """Attach a finding to an approved response."""

    document['payload']['findings'] = [
        {
            'finding_id': 'F-001',
            'severity': 'medium',
            'title': 'finding',
            'path': 'file.py',
            'line': 1,
            'explanation': 'explanation',
            'acceptance_criterion': 'criterion',
        }
    ]


@pytest.mark.parametrize(
    ('mutation', 'expected'),
    [
        (add_unknown_field, INVALID_ENVELOPE),
        (break_correlation, 'reviewer response has invalid in_reply_to'),
        (break_identity, INVALID_IDENTITY),
        (break_timestamp, INVALID_IDENTITY),
        (add_payload_field, INVALID_PAYLOAD),
        (break_findings, INVALID_PAYLOAD),
        (break_verdict, INVALID_VERDICT),
        (break_artifact_path, INVALID_ARTIFACT_PATH),
        (approve_with_findings, APPROVED_WITH_FINDINGS),
    ],
)
def test_rejects_invalid_review_response(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any]], None],
    expected: str,
) -> None:
    """Reject malformed, stale, or internally inconsistent responses."""

    artifact = tmp_path / 'review.md'
    artifact.write_text('# Review\n')
    request, original = review_documents(artifact)
    response = deepcopy(original)
    mutation(response)

    with pytest.raises(WorkerError, match=expected):
        _validate_review_response(response, request=request, artifact_path=artifact)


def test_rejects_missing_review_artifact(tmp_path: Path) -> None:
    """Require the reviewer to create the declared artifact."""

    artifact = tmp_path / 'missing.md'
    request, response = review_documents(artifact)

    with pytest.raises(WorkerError, match=MISSING_ARTIFACT):
        _validate_review_response(response, request=request, artifact_path=artifact)


def test_rejects_changed_worktree_digest() -> None:
    """Enforce the reviewer's read-only worktree boundary."""

    with pytest.raises(WorkerError, match=WORKTREE_CHANGED):
        _require_unchanged('sha256:new', 'sha256:reviewed')


def test_accepts_legacy_digest_spelling() -> None:
    """Accept a bare persisted SHA-256 digest when content is unchanged."""

    bare_digest = 'a' * 64
    _require_unchanged(f'sha256:{bare_digest}', bare_digest)


def test_normalizes_digest_failures(tmp_path: Path) -> None:
    """Keep Git and filesystem failures inside the worker error contract."""

    def fail_digest(worktree: Path, base_sha: str) -> str | None:
        raise RuntimeError(f'{worktree}:{base_sha}')

    with pytest.raises(WorkerError, match='cannot compute worktree digest'):
        _digest(fail_digest, tmp_path, 'HEAD')
