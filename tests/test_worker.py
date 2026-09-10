"""Tests for strict local worker protocol validation."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from agent_orchestra.evidence import (
    WORKTREE_CHANGED,
    WorkerError,
    require_unchanged,
    worktree_digest,
)
from agent_orchestra.invocations import InvocationIdentity
from agent_orchestra.manifests import evidence_path
from agent_orchestra.messages import (
    APPROVED_WITH_FINDINGS,
    DUPLICATE_MESSAGE_ID,
    INVALID_ARTIFACT_PATH,
    INVALID_ENVELOPE,
    INVALID_FINDING_DISPOSITIONS,
    INVALID_IDENTITY,
    INVALID_PAYLOAD,
    INVALID_REMEDIATION_REQUEST,
    INVALID_VERDICT,
    MISSING_ARTIFACT,
    read_message_chain,
    require_unique_message_id,
    validate_developer_handoff,
    validate_remediation_request,
    validate_review_request,
    validate_review_response,
)
from agent_orchestra.reviewer_plan import ReviewerExecution, ReviewerExecutionPlan
from agent_orchestra.schemas import CHANGES_REQUESTED_WITHOUT_FINDINGS
from agent_orchestra.worker import (
    ReviewerSetReviewPlan,
    _execution_record,
    _read_execution_record,
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
    request: dict[str, Any] = {
        'message_id': request_id,
        'run_id': '20260902T130000Z-a7f3c921',
        'sequence': 1,
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


def test_read_execution_record_accepts_reviewer_set_schema(tmp_path: Path) -> None:
    """Parse schema-3 execution metadata at the worker resume boundary."""

    document = {
        'schema_version': 3,
        'run_id': 'job-1',
        'objective': 'Review the frozen diff.',
        'developer': {
            'command': ['/python', '-m', 'developer'],
            'identity': {'vendor': 'vendor', 'model': None, 'runtime': 'runtime'},
            'timeout_seconds': 120,
        },
        'max_review_iterations': 3,
        'created_at': '2026-09-09T15:00:00Z',
        'reviewer_plan': {
            'schema_version': 1,
            'reviewer_set_id': 'default',
            'aggregation_policy': 'all_required',
            'reviewers': [
                {
                    'reviewer_id': reviewer_id,
                    'command': ['/python', '-m', reviewer_id],
                    'identity': {
                        'vendor': 'vendor',
                        'model': None,
                        'runtime': reviewer_id,
                    },
                    'timeout_seconds': 90,
                }
                for reviewer_id in ('security', 'portability')
            ],
        },
    }
    (tmp_path / 'execution.json').write_text(json.dumps(document), encoding='utf-8')

    record = _read_execution_record(tmp_path, 'job-1')

    assert record.schema_version == 3


def test_reviewer_set_plan_builds_schema_3_execution_record() -> None:
    """Persist the complete immutable reviewer plan through the worker boundary."""

    identity = InvocationIdentity(vendor='vendor', model=None, runtime='runtime')
    reviewer_plan = ReviewerExecutionPlan(
        'default',
        tuple(
            ReviewerExecution(
                reviewer_id=reviewer_id,
                command=('/python', '-m', reviewer_id),
                identity=identity,
                timeout_seconds=90,
            )
            for reviewer_id in ('security', 'portability')
        ),
    )
    plan = ReviewerSetReviewPlan(
        objective='Review the frozen diff.',
        reviewer_plan=reviewer_plan,
        developer_command=('/python', '-m', 'developer'),
        developer_timeout_seconds=120,
        max_iterations=3,
        developer_identity=identity,
    )

    record = _execution_record(plan, run_id='job-1', created_at='2026-09-09T15:00:00Z')

    assert record.schema_version == 3
    assert record.reviewer_plan.reviewer_set_id == 'default'
    assert [reviewer.reviewer_id for reviewer in record.reviewer_plan.reviewers] == [
        'security',
        'portability',
    ]


def test_validate_review_response_uses_request_sequence(tmp_path: Path) -> None:
    """Accept a correlated review response after a remediation iteration."""

    artifact_path = tmp_path / 'review.md'
    artifact_path.write_text('approved\n', encoding='utf-8')
    request, response = review_documents(artifact_path)
    request['sequence'] = 5
    request['iteration'] = 2
    response['sequence'] = 6
    response['iteration'] = 2

    assert (
        validate_review_response(response, request=request, artifact_path=artifact_path)
        == 'approved'
    )


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


def request_changes_without_findings(document: dict[str, Any]) -> None:
    """Request changes without identifying an actionable defect."""

    document['payload']['verdict'] = 'changes_requested'


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
        (request_changes_without_findings, CHANGES_REQUESTED_WITHOUT_FINDINGS),
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
        validate_review_response(response, request=request, artifact_path=artifact)


def test_rejects_missing_review_artifact(tmp_path: Path) -> None:
    """Require the reviewer to create the declared artifact."""

    artifact = tmp_path / 'missing.md'
    request, response = review_documents(artifact)

    with pytest.raises(WorkerError, match=MISSING_ARTIFACT):
        validate_review_response(response, request=request, artifact_path=artifact)


def test_rejects_changed_worktree_digest() -> None:
    """Enforce the reviewer's read-only worktree boundary."""

    with pytest.raises(WorkerError, match=WORKTREE_CHANGED):
        require_unchanged('sha256:new', 'sha256:reviewed')


def test_accepts_legacy_digest_spelling() -> None:
    """Accept a bare persisted SHA-256 digest when content is unchanged."""

    bare_digest = 'a' * 64
    require_unchanged(f'sha256:{bare_digest}', bare_digest)


def test_normalizes_digest_failures(tmp_path: Path) -> None:
    """Keep Git and filesystem failures inside the worker error contract."""

    def fail_digest(worktree: Path, base_sha: str) -> str | None:
        raise RuntimeError(f'{worktree}:{base_sha}')

    with pytest.raises(WorkerError, match='cannot compute worktree digest'):
        worktree_digest(fail_digest, tmp_path, 'HEAD')


def developer_documents() -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a correlated remediation request and developer handoff."""

    scope = {
        'worktree_path': '/example/worktree',
        'base_sha': 'base',
        'head_sha': 'head',
        'diff_digest': f'sha256:{"a" * 64}',
    }
    request = {
        'message_id': str(uuid4()),
        'run_id': '20260903T120000Z-a7f3c921',
        'sequence': 3,
        'iteration': 1,
        'scope': scope,
    }
    response = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': 4,
        'iteration': 1,
        'message_type': 'developer_handoff',
        'sender': 'developer',
        'recipient': 'orchestrator',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': scope,
        'payload': {
            'status': 'ready_for_review',
            'summary': 'Addressed both findings.',
            'files_changed': ['src/example.py'],
            'validation': [{'command': 'mise tests', 'outcome': 'passed'}],
            'dispositions': [
                {
                    'finding_id': finding_id,
                    'disposition': 'addressed',
                    'rationale': 'Fixed.',
                }
                for finding_id in ('F-001', 'F-002')
            ],
            'remaining_risks': [],
        },
    }
    return request, response


def test_validates_exact_finding_dispositions() -> None:
    """Accept exactly one disposition for every stable finding ID."""

    request, response = developer_documents()

    parsed = validate_developer_handoff(
        response, request=request, finding_ids=('F-001', 'F-002')
    )

    assert parsed.payload.status == 'ready_for_review'


def test_rejects_missing_duplicate_and_unknown_dispositions() -> None:
    """Reject any disposition set that is not an exact finding-ID match."""

    request, response = developer_documents()
    response['payload']['dispositions'][1]['finding_id'] = 'F-001'

    with pytest.raises(WorkerError, match=INVALID_FINDING_DISPOSITIONS):
        validate_developer_handoff(
            response, request=request, finding_ids=('F-001', 'F-002')
        )


def test_validates_contained_remediation_evidence(tmp_path: Path) -> None:
    """Require remediation to reference complete evidence inside its run."""

    result = tmp_path / 'messages/review.json'
    artifact = tmp_path / 'artifacts/review.md'
    result.parent.mkdir()
    artifact.parent.mkdir()
    result.write_text('{}')
    artifact.write_text('# Review\n')
    request: dict[str, Any] = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': str(uuid4()),
        'run_id': '20260903T120000Z-a7f3c921',
        'sequence': 3,
        'iteration': 1,
        'message_type': 'remediation_request',
        'sender': 'orchestrator',
        'recipient': 'developer',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': {
            'worktree_path': '/example/worktree',
            'base_sha': 'base',
            'head_sha': 'head',
            'diff_digest': f'sha256:{"a" * 64}',
        },
        'payload': {
            'objective': 'Address findings.',
            'allowed_actions': [],
            'timeout_seconds': 1800,
            'review_result_path': str(result),
            'review_artifact_path': str(artifact),
        },
    }

    validate_remediation_request(request, run_directory=tmp_path)

    request['payload']['review_artifact_path'] = str(tmp_path.parent / 'outside.md')
    with pytest.raises(WorkerError, match='outside the run'):
        validate_remediation_request(request, run_directory=tmp_path)


def test_rejects_missing_remediation_evidence(tmp_path: Path) -> None:
    """Fail closed when complete accepted review evidence is unavailable."""

    request, _ = developer_documents()
    request.update(
        {
            'schema_version': 1,
            'in_reply_to': str(uuid4()),
            'message_type': 'remediation_request',
            'sender': 'orchestrator',
            'recipient': 'developer',
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
            'payload': {
                'objective': 'Address findings.',
                'allowed_actions': [],
                'timeout_seconds': 1800,
                'review_result_path': str(tmp_path / 'missing.json'),
                'review_artifact_path': str(tmp_path / 'missing.md'),
            },
        }
    )

    with pytest.raises(WorkerError, match=INVALID_REMEDIATION_REQUEST):
        validate_remediation_request(request, run_directory=tmp_path)


def test_rejects_review_evidence_path_escape(tmp_path: Path) -> None:
    """Keep review artifacts and prior evidence inside the run directory."""

    request, _ = review_documents(tmp_path.parent / 'outside.md')
    request.update(
        {
            'schema_version': 1,
            'in_reply_to': None,
            'sequence': 1,
            'message_type': 'review_request',
            'sender': 'orchestrator',
            'recipient': 'reviewer',
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
            'payload': {
                'objective': 'Review.',
                'allowed_actions': [],
                'timeout_seconds': 30,
                'artifact_path': str(tmp_path.parent / 'outside.md'),
                'prior_review_path': None,
            },
        }
    )

    with pytest.raises(WorkerError, match='outside the run'):
        validate_review_request(request, run_directory=tmp_path)


def test_rejects_duplicate_persisted_message_id(tmp_path: Path) -> None:
    """Reject a response identity already used by durable evidence."""

    message_id = str(uuid4())
    duplicate = tmp_path / evidence_path('review_request', ordinal=1)
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(json.dumps({'message_id': message_id}))

    with pytest.raises(WorkerError, match=DUPLICATE_MESSAGE_ID):
        require_unique_message_id({'message_id': message_id}, tmp_path)


def test_manifest_message_paths_are_written_and_recovered(tmp_path: Path) -> None:
    """Discover, order, correlate, and recover every manifest message type."""

    artifact = tmp_path / 'artifacts/review.md'
    artifact.parent.mkdir()
    artifact.write_text('# Review\n', encoding='utf-8')
    request, result = review_documents(artifact)
    request.update(
        {
            'schema_version': 1,
            'in_reply_to': None,
            'message_type': 'review_request',
            'sender': 'orchestrator',
            'recipient': 'reviewer',
            'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
            'payload': {
                'objective': 'Review.',
                'allowed_actions': [],
                'timeout_seconds': 30,
                'artifact_path': str(artifact),
                'prior_review_path': None,
            },
        }
    )
    finding = {
        'finding_id': 'F-001',
        'severity': 'medium',
        'title': 'Finding',
        'path': 'example.py',
        'line': 1,
        'explanation': 'Explanation.',
        'acceptance_criterion': 'Fix it.',
    }
    result['payload']['verdict'] = 'changes_requested'
    result['payload']['findings'] = [finding]
    result_path = tmp_path / evidence_path('review_result', ordinal=2)
    remediation = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': result['message_id'],
        'run_id': request['run_id'],
        'sequence': 3,
        'iteration': 1,
        'message_type': 'remediation_request',
        'sender': 'orchestrator',
        'recipient': 'developer',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': request['scope'],
        'payload': {
            'objective': 'Fix it.',
            'allowed_actions': [],
            'timeout_seconds': 30,
            'review_result_path': str(result_path),
            'review_artifact_path': str(artifact),
        },
    }
    _, handoff = developer_documents()
    handoff.update(
        {
            'in_reply_to': remediation['message_id'],
            'run_id': request['run_id'],
            'scope': request['scope'],
        }
    )
    handoff['payload']['dispositions'] = [
        {
            'finding_id': 'F-001',
            'disposition': 'addressed',
            'rationale': 'Fixed.',
        }
    ]
    documents = [request, result, remediation, handoff]
    message_types = [
        'review_request',
        'review_result',
        'remediation_request',
        'developer_handoff',
    ]
    for sequence, (message_type, document) in enumerate(
        zip(message_types, documents, strict=True), start=1
    ):
        path = tmp_path / evidence_path(message_type, ordinal=sequence)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding='utf-8')

    recovered = read_message_chain(tmp_path, str(request['run_id']))

    assert [document['message_type'] for _, document in recovered] == message_types


def test_read_message_chain_rejects_unknown_message(tmp_path: Path) -> None:
    """Fail closed when an undeclared JSON file occupies the message namespace."""

    messages = tmp_path / 'messages'
    messages.mkdir()
    (messages / '000002-future.json').write_text('{}', encoding='utf-8')

    with pytest.raises(WorkerError, match='unknown canonical message path'):
        read_message_chain(tmp_path, str(uuid4()))


def test_read_message_chain_rejects_symlinked_message_directory(
    tmp_path: Path,
) -> None:
    """Fail closed without traversing a symlinked canonical message namespace."""

    outside = tmp_path / 'outside'
    outside.mkdir()
    run_directory = tmp_path / 'run'
    run_directory.mkdir()
    (run_directory / 'messages').symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkerError, match='message namespace is unsafe'):
        read_message_chain(run_directory, str(uuid4()))
