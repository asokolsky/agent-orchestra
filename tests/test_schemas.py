"""Tests for strict vendor-neutral workflow schemas."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from agent_orchestra.schemas import (
    APPROVED_WITH_FINDINGS,
    CHANGES_REQUESTED_WITHOUT_FINDINGS,
    DEVELOPER_RESULT_SCHEMA,
    DUPLICATE_REVIEW_FINDING_IDS,
    EXECUTION_RECORD_ADAPTER,
    INVALID_REVIEW_FIELDS,
    INVALID_REVIEW_FINDINGS,
    REVIEW_RESULT_SCHEMA,
    ReviewerBatchResultSchema,
    ReviewerBatchResultV2Schema,
    ReviewerBatchResultV3Schema,
    ReviewerExecutionPlanSchema,
    ReviewerSetExecutionRecordSchema,
    SchemaValidationError,
    validate_developer_result,
    validate_review_result,
)


def _reviewer_batch_result() -> dict[str, Any]:
    """Return one valid canonical aggregate reviewer decision."""

    return {
        'schema_version': 1,
        'run_id': 'run-1',
        'iteration': 1,
        'reviewer_set_id': 'default',
        'aggregation_policy': 'all_required',
        'diff_digest': 'sha256:' + 'a' * 64,
        'verdict': 'approved',
        'reviewers': [
            {
                'reviewer_id': reviewer_id,
                'outcome': 'approved',
                'result_path': f'messages/000002-{reviewer_id}-review-result.json',
            }
            for reviewer_id in ('security', 'portability')
        ],
        'changes_requested_by': [],
        'blocked_by': [],
        'incomplete_reviewers': [],
    }


def test_reviewer_batch_finding_ids_are_reviewer_namespaced() -> None:
    """Require aggregate findings to retain source and reviewer identity."""

    document = _reviewer_batch_result()
    document['schema_version'] = 2
    document['verdict'] = 'changes_requested'
    document['reviewers'][0]['outcome'] = 'changes_requested'
    document['changes_requested_by'] = ['security']
    document['findings'] = [
        {
            'finding_id': 'security:finding-1',
            'source_finding_id': 'finding-1',
            'reviewer_id': 'security',
            'severity': 'high',
            'title': 'Finding',
            'path': 'src/example.py',
            'line': 1,
            'explanation': 'The behavior is incorrect.',
            'acceptance_criterion': 'Correct the behavior.',
        }
    ]

    ReviewerBatchResultV2Schema.model_validate(document)

    document['findings'][0]['finding_id'] = 'finding-1'
    with pytest.raises(ValueError, match='uncorrelated finding'):
        ReviewerBatchResultV2Schema.model_validate(document)


def test_reviewer_batch_v3_is_addressable_for_remediation() -> None:
    """Require aggregate machine and human evidence correlation fields."""

    document = _reviewer_batch_result()
    document.update(
        schema_version=3,
        message_id=str(uuid4()),
        artifact_path='/run/artifacts/review-batch-0001.md',
        findings=[],
    )

    ReviewerBatchResultV3Schema.model_validate(document)

    document['message_id'] = 'not-a-uuid'
    with pytest.raises(ValueError, match='UUID'):
        ReviewerBatchResultV3Schema.model_validate(document)


@pytest.mark.parametrize(
    ('outcome', 'result_path'),
    [('approved', None), ('incomplete', 'messages/000002-security-review-result.json')],
)
def test_reviewer_batch_member_requires_result_for_completed_outcome(
    outcome: str, result_path: str | None
) -> None:
    """Reject aggregate members whose outcome contradicts result presence."""

    document = _reviewer_batch_result()
    document['reviewers'][0]['outcome'] = outcome
    document['reviewers'][0]['result_path'] = result_path
    if outcome == 'incomplete':
        document['verdict'] = 'blocked'
        document['incomplete_reviewers'] = ['security']
    with pytest.raises(ValueError, match='result path is inconsistent'):
        ReviewerBatchResultSchema.model_validate(document)


def reviewer_execution_plan() -> dict[str, Any]:
    """Return one valid canonical reviewer execution plan."""

    reviewer = {
        'command': ['/python', '-m', 'reviewer'],
        'identity': {'vendor': 'vendor', 'model': None, 'runtime': 'runtime'},
        'timeout_seconds': 90,
    }
    return {
        'schema_version': 1,
        'reviewer_set_id': 'default',
        'aggregation_policy': 'all_required',
        'reviewers': [
            {'reviewer_id': 'security', **reviewer},
            {'reviewer_id': 'portability', **reviewer},
        ],
    }


def test_reviewer_execution_plan_schema_is_strict_and_ordered() -> None:
    """Accept an ordered plan while rejecting unknown persisted fields."""

    document = reviewer_execution_plan()
    record = ReviewerExecutionPlanSchema.model_validate(document)
    assert [reviewer.reviewer_id for reviewer in record.reviewers] == [
        'security',
        'portability',
    ]
    document['policy_version'] = 1
    with pytest.raises(ValueError, match='Extra inputs are not permitted'):
        ReviewerExecutionPlanSchema.model_validate(document)

    schema = ReviewerExecutionPlanSchema.model_json_schema()
    assert schema['properties']['reviewer_set_id']['pattern'] == (
        '^[a-z0-9][a-z0-9_-]*$'
    )
    assert (
        schema['$defs']['ReviewerExecutionSchema']['properties']['reviewer_id'][
            'pattern'
        ]
        == '^[a-z0-9][a-z0-9_-]*$'
    )


def test_reviewer_execution_plan_schema_rejects_duplicate_members() -> None:
    """Prevent two persisted reviewers from sharing one evidence namespace."""

    document = reviewer_execution_plan()
    document['reviewers'][1]['reviewer_id'] = 'security'
    with pytest.raises(ValueError, match='duplicate reviewer IDs'):
        ReviewerExecutionPlanSchema.model_validate(document)


def test_reviewer_set_execution_record_embeds_resumable_plan() -> None:
    """Persist the complete selected reviewer plan in schema-3 resume metadata."""

    document = {
        'schema_version': 3,
        'run_id': 'job-1',
        'objective': 'Review the frozen diff.',
        'developer': {
            'command': ['/python', '-m', 'developer'],
            'identity': {
                'vendor': 'vendor',
                'model': None,
                'runtime': 'runtime',
            },
            'timeout_seconds': 120,
        },
        'max_review_iterations': 3,
        'created_at': '2026-09-09T15:00:00Z',
        'reviewer_plan': reviewer_execution_plan(),
    }

    record = ReviewerSetExecutionRecordSchema.model_validate(document)

    assert record.schema_version == 3
    assert record.reviewer_plan.reviewer_set_id == 'default'
    assert [member.reviewer_id for member in record.reviewer_plan.reviewers] == [
        'security',
        'portability',
    ]


def test_reviewer_set_execution_record_rejects_schema_2_shape() -> None:
    """Keep single-reviewer and reviewer-set execution records unambiguous."""

    document = {
        'schema_version': 3,
        'run_id': 'job-1',
        'objective': 'Review the frozen diff.',
        'reviewer': {
            'command': ['/python', '-m', 'reviewer'],
            'identity': {
                'vendor': 'vendor',
                'model': None,
                'runtime': 'runtime',
            },
            'timeout_seconds': 90,
        },
        'developer': {
            'command': ['/python', '-m', 'developer'],
            'identity': {
                'vendor': 'vendor',
                'model': None,
                'runtime': 'runtime',
            },
            'timeout_seconds': 120,
        },
        'max_review_iterations': 3,
        'created_at': '2026-09-09T15:00:00Z',
    }

    with pytest.raises(ValueError, match='reviewer_plan'):
        ReviewerSetExecutionRecordSchema.model_validate(document)


@pytest.mark.parametrize('schema_version', [2, 3])
def test_execution_record_adapter_selects_versioned_shape(schema_version: int) -> None:
    """Decode legacy and reviewer-set records through one strict boundary."""

    document: dict[str, Any] = {
        'schema_version': schema_version,
        'run_id': 'job-1',
        'objective': 'Review the frozen diff.',
        'developer': {
            'command': ['/python', '-m', 'developer'],
            'identity': {'vendor': 'vendor', 'model': None, 'runtime': 'runtime'},
            'timeout_seconds': 120,
        },
        'max_review_iterations': 3,
        'created_at': '2026-09-09T15:00:00Z',
    }
    if schema_version == 2:
        document['reviewer'] = {
            'command': ['/python', '-m', 'reviewer'],
            'identity': {'vendor': 'vendor', 'model': None, 'runtime': 'runtime'},
            'timeout_seconds': 90,
        }
    else:
        document['reviewer_plan'] = reviewer_execution_plan()

    record = EXECUTION_RECORD_ADAPTER.validate_python(document)

    assert record.schema_version == schema_version


@pytest.mark.parametrize(
    ('field', 'value'),
    [('reviewer_set_id', 'Unsafe ID'), ('reviewer_id', 'also bad!')],
)
def test_reviewer_execution_plan_schema_rejects_partial_id_matches(
    field: str, value: str
) -> None:
    """Apply canonical full-match semantics to every persisted identifier."""

    document = reviewer_execution_plan()
    if field == 'reviewer_set_id':
        document[field] = value
    else:
        document['reviewers'][0][field] = value

    with pytest.raises(ValueError, match='String should match pattern'):
        ReviewerExecutionPlanSchema.model_validate(document)


def review_result() -> dict[str, Any]:
    """Return one valid canonical review result."""

    return {
        'verdict': 'changes_requested',
        'summary': 'One issue needs remediation.',
        'findings': [
            {
                'finding_id': 'F-001',
                'severity': 'medium',
                'title': 'Incomplete validation',
                'path': 'src/example.py',
                'line': 10,
                'explanation': 'The input is not validated.',
                'acceptance_criterion': 'Reject invalid input.',
            }
        ],
        'validation': [],
        'verification_gaps': [],
    }


def test_review_schema_is_strict_and_deterministic() -> None:
    """Generate a closed JSON Schema with stable required-field order."""

    finding = REVIEW_RESULT_SCHEMA['$defs']['ReviewFindingSchema']

    assert REVIEW_RESULT_SCHEMA['additionalProperties'] is False
    assert finding['additionalProperties'] is False
    assert finding['required'] == sorted(finding['properties'])


def test_validate_review_result_accepts_canonical_result() -> None:
    """Accept a complete runtime-independent result."""

    validate_review_result(review_result())


def test_validate_review_result_rejects_unknown_fields() -> None:
    """Fail closed when a runtime adds vendor-specific result state."""

    result = review_result()
    result['model'] = 'vendor-model'

    with pytest.raises(SchemaValidationError, match=INVALID_REVIEW_FIELDS):
        validate_review_result(result)


def test_validate_review_result_rejects_unknown_finding_fields() -> None:
    """Apply strict unknown-field rejection to nested findings too."""

    result = review_result()
    result['findings'][0]['vendor_reference'] = 'internal-value'

    with pytest.raises(SchemaValidationError, match=INVALID_REVIEW_FINDINGS):
        validate_review_result(result)


def test_validate_review_result_rejects_coercion() -> None:
    """Reject values that a non-strict model would silently coerce."""

    result = review_result()
    result['findings'][0]['line'] = '10'

    with pytest.raises(SchemaValidationError, match=INVALID_REVIEW_FINDINGS):
        validate_review_result(result)


def test_validate_review_result_rejects_approved_findings() -> None:
    """Reject approval when actionable findings remain."""

    result = review_result()
    result['verdict'] = 'approved'

    with pytest.raises(SchemaValidationError, match=APPROVED_WITH_FINDINGS):
        validate_review_result(result)


def test_validate_review_result_rejects_changes_requested_without_findings() -> None:
    """Require every changes-requested verdict to identify an actionable defect."""

    result = review_result()
    result['findings'] = []

    with pytest.raises(SchemaValidationError, match=CHANGES_REQUESTED_WITHOUT_FINDINGS):
        validate_review_result(result)


def test_validate_review_result_rejects_duplicate_finding_ids() -> None:
    """Reject a review that no valid developer handoff could disposition."""

    result = review_result()
    result['findings'].append(dict(result['findings'][0]))

    with pytest.raises(SchemaValidationError, match=DUPLICATE_REVIEW_FINDING_IDS):
        validate_review_result(result)


def test_developer_result_schema_validates_strict_dispositions() -> None:
    """Validate developer handoffs without runtime-specific fields."""

    result = {
        'status': 'ready_for_review',
        'summary': 'Addressed the finding.',
        'files_changed': ['src/example.py'],
        'validation': [{'command': 'mise tests', 'outcome': 'passed'}],
        'dispositions': [
            {
                'finding_id': 'F-001',
                'disposition': 'addressed',
                'rationale': 'Added strict validation.',
            }
        ],
        'remaining_risks': [],
    }

    parsed = validate_developer_result(result)

    assert DEVELOPER_RESULT_SCHEMA['additionalProperties'] is False
    assert parsed.dispositions[0].finding_id == 'F-001'


def test_developer_result_rejects_vendor_fields() -> None:
    """Prevent runtime metadata from entering canonical developer state."""

    result = {
        'status': 'blocked',
        'summary': 'Needs a decision.',
        'files_changed': [],
        'validation': [],
        'dispositions': [],
        'remaining_risks': [],
        'session_id': 'vendor-session',
    }

    with pytest.raises(SchemaValidationError, match='canonical schema'):
        validate_developer_result(result)
