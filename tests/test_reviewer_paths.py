"""Tests for reviewer-qualified identities and evidence paths."""

from __future__ import annotations

from dataclasses import astuple

import pytest

from agent_orchestra.reviewer_paths import (
    ReviewerIdentityError,
    reviewer_evidence_paths,
    reviewer_invocation_id,
    reviewer_task_id,
)


def test_reviewer_id_qualifies_every_identity_and_path_family() -> None:
    """Keep two reviewers from sharing any durable or mutable identifier."""

    first = reviewer_evidence_paths(
        sequence=1, iteration=2, reviewer_id='codex', attempt=1
    )
    second = reviewer_evidence_paths(
        sequence=1, iteration=2, reviewer_id='claude', attempt=1
    )

    assert reviewer_task_id('job', 1, 'codex') == 'job:000001-reviewer-codex'
    assert reviewer_invocation_id('job', 1, 'codex', 1) == (
        'job:000001-reviewer-codex:attempt-0001'
    )
    assert set(astuple(first)).isdisjoint(astuple(second))
    assert first.temporary_result == (
        '.000001-reviewer-codex.attempt-0001.review-result.json'
    )


def test_reviewer_attempts_have_distinct_streams_and_temporary_results() -> None:
    """Ensure retries cannot overwrite prior attempt-local evidence."""

    first = reviewer_evidence_paths(
        sequence=3, iteration=1, reviewer_id='security', attempt=1
    )
    retry = reviewer_evidence_paths(
        sequence=3, iteration=1, reviewer_id='security', attempt=2
    )

    assert first.request == retry.request
    assert first.result == retry.result
    assert first.artifact == retry.artifact
    assert first.stdout != retry.stdout
    assert first.stderr != retry.stderr
    assert first.runtime_metadata != retry.runtime_metadata
    assert first.temporary_result != retry.temporary_result


def test_reviewer_and_attempt_segments_cannot_collide() -> None:
    """Keep an attempt suffix inside an ID distinct from a real retry segment."""

    adversarial = reviewer_evidence_paths(
        sequence=1, iteration=1, reviewer_id='codex-attempt-0002', attempt=1
    )
    retry = reviewer_evidence_paths(
        sequence=1, iteration=1, reviewer_id='codex', attempt=2
    )

    assert set(astuple(adversarial)).isdisjoint(astuple(retry))
    assert adversarial.stdout.endswith(
        'reviewer-codex-attempt-0002.attempt-0001.stdout.log'
    )
    assert retry.stdout.endswith('reviewer-codex.attempt-0002.stdout.log')


@pytest.mark.parametrize('reviewer_id', ['', 'Upper', '../escape', 'has space'])
def test_reviewer_id_rejects_unsafe_values(reviewer_id: str) -> None:
    """Reject reviewer IDs that cannot safely occupy paths and task IDs."""

    with pytest.raises(ReviewerIdentityError, match='invalid reviewer ID'):
        reviewer_task_id('job', 1, reviewer_id)
