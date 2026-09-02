"""Human-readable report rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_orchestra.models import Review


def render_review(review: Review) -> str:
    """Render a structured review as Markdown."""

    lines = [
        f'# Review: run {review.run_id}',
        '',
        f'- Iteration: {review.iteration}',
        f'- Diff digest: `{review.diff_digest}`',
        f'- Verdict: **{review.verdict}**',
        '',
        '## Summary',
        '',
        review.summary,
        '',
        '## Findings',
        '',
    ]
    if not review.findings:
        lines.append('No findings.')
    else:
        for finding in review.findings:
            location = finding.path or 'general'
            if finding.line is not None:
                location = f'{location}:{finding.line}'
            lines.extend(
                [
                    f'### {finding.finding_id}: {finding.severity} - {finding.title}',
                    '',
                    f'Location: `{location}`',
                    '',
                    finding.explanation,
                    '',
                    f'Acceptance criterion: {finding.acceptance_criterion}',
                    '',
                ]
            )
    for heading, values, empty in (
        ('Validation', review.validation, 'No validation reported.'),
        (
            'Verification gaps',
            review.verification_gaps,
            'No verification gaps reported.',
        ),
    ):
        lines.extend(['', f'## {heading}', ''])
        lines.extend((f'- {value}' for value in values) if values else [empty])
    return '\n'.join(lines).rstrip() + '\n'
