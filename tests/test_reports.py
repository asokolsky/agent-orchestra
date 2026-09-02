"""Tests for Markdown review rendering."""

from agent_orchestra.models import Finding, Review, Severity, Verdict
from agent_orchestra.reports import render_review


def test_render_review_includes_structured_finding() -> None:
    """Render the verdict, location, and explanation of a finding."""

    review = Review(
        run_id='20260902T150612Z-a7f3c921',
        iteration=1,
        diff_digest='abc123',
        verdict=Verdict.CHANGES_REQUESTED,
        summary='One correctness issue.',
        findings=(
            Finding(
                finding_id='F-001',
                severity=Severity.HIGH,
                title='Unsafe transition',
                explanation='The transition bypasses approval.',
                acceptance_criterion='Require approval before transition.',
                path='workflow.py',
                line=42,
            ),
        ),
        validation=('pytest passed',),
        verification_gaps=('No integration test.',),
    )

    rendered = render_review(review)

    assert 'changes_requested' in rendered
    assert 'F-001: high - Unsafe transition' in rendered
    assert 'workflow.py:42' in rendered
    assert 'The transition bypasses approval.' in rendered
    assert 'Acceptance criterion: Require approval before transition.' in rendered
    assert '- pytest passed' in rendered
    assert '- No integration test.' in rendered
