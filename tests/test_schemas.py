"""Tests for strict vendor-neutral workflow schemas."""

from __future__ import annotations

from typing import Any

import pytest

from agent_orchestra.schemas import (
    APPROVED_WITH_FINDINGS,
    CHANGES_REQUESTED_WITHOUT_FINDINGS,
    DEVELOPER_RESULT_SCHEMA,
    DUPLICATE_REVIEW_FINDING_IDS,
    INVALID_REVIEW_FIELDS,
    INVALID_REVIEW_FINDINGS,
    REVIEW_RESULT_SCHEMA,
    ReviewerExecutionPlanSchema,
    SchemaValidationError,
    validate_developer_result,
    validate_review_result,
)


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


def test_reviewer_execution_plan_schema_rejects_duplicate_members() -> None:
    """Prevent two persisted reviewers from sharing one evidence namespace."""

    document = reviewer_execution_plan()
    document['reviewers'][1]['reviewer_id'] = 'security'
    with pytest.raises(ValueError, match='duplicate reviewer IDs'):
        ReviewerExecutionPlanSchema.model_validate(document)


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

    with pytest.raises(ValueError, match='invalid reviewer ID'):
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
