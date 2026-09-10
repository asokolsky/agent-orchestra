"""Human-readable report rendering."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_orchestra.models import Review
    from agent_orchestra.schemas import ReviewerBatchResultV3Schema


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


def render_reviewer_batch(batch: ReviewerBatchResultV3Schema) -> str:
    """Render a canonical aggregate reviewer decision as Markdown."""

    lines = [
        f'# Review batch: run {batch.run_id}',
        '',
        f'- Iteration: {batch.iteration}',
        f'- Reviewer set: `{batch.reviewer_set_id}`',
        f'- Diff digest: `{batch.diff_digest}`',
        f'- Verdict: **{batch.verdict}**',
        '',
        '## Reviewer outcomes',
        '',
        *(f'- `{item.reviewer_id}`: {item.outcome}' for item in batch.reviewers),
        '',
        '## Findings',
        '',
    ]
    if not batch.findings:
        lines.append('No findings.')
    else:
        for finding in batch.findings:
            location = finding.path or 'general'
            if finding.line is not None:
                location = f'{location}:{finding.line}'
            lines.extend(
                [
                    f'### {finding.finding_id}: {finding.severity} - {finding.title}',
                    '',
                    f'Reviewer: `{finding.reviewer_id}`',
                    '',
                    f'Location: `{location}`',
                    '',
                    finding.explanation,
                    '',
                    f'Acceptance criterion: {finding.acceptance_criterion}',
                    '',
                ]
            )
    return '\n'.join(lines).rstrip() + '\n'
