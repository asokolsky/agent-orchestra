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
    SchemaValidationError,
    validate_developer_result,
    validate_review_result,
)


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
