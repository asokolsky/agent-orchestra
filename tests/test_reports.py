"""Tests for Markdown review rendering."""

from uuid import uuid4

from agent_orchestra.models import Finding, Review, Severity, Verdict
from agent_orchestra.reports import render_review


def test_render_review_includes_structured_finding() -> None:
    """Render the verdict, location, and explanation of a finding."""

    review = Review(
        run_id=uuid4(),
        iteration=1,
        diff_digest='abc123',
        verdict=Verdict.CHANGES_REQUESTED,
        summary='One correctness issue.',
        findings=(
            Finding(
                severity=Severity.HIGH,
                title='Unsafe transition',
                explanation='The transition bypasses approval.',
                path='workflow.py',
                line=42,
            ),
        ),
    )

    rendered = render_review(review)

    assert 'changes_requested' in rendered
    assert 'workflow.py:42' in rendered
    assert 'The transition bypasses approval.' in rendered
