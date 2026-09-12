"""Stable reviewer-qualified identities and evidence paths."""

from __future__ import annotations

import re
from dataclasses import dataclass

from agent_orchestra.errors import AgentOrchestraError
from agent_orchestra.manifests import evidence_path

REVIEWER_ID_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]*$')


class ReviewerIdentityError(AgentOrchestraError):
    """Raised when a reviewer identifier cannot safely qualify evidence."""


def validate_reviewer_id(reviewer_id: str) -> str:
    """Return one safe stable reviewer identifier or raise."""

    if REVIEWER_ID_PATTERN.fullmatch(reviewer_id) is None:
        message = f'invalid reviewer ID: {reviewer_id!r}'
        raise ReviewerIdentityError(message)
    return reviewer_id


def reviewer_task_id(run_id: str, sequence: int, reviewer_id: str) -> str:
    """Return the durable task ID for one reviewer assignment."""

    validate_reviewer_id(reviewer_id)
    if sequence < 1:
        message = 'reviewer sequence must be positive'
        raise ReviewerIdentityError(message)
    return f'{run_id}:{sequence:06d}-reviewer-{reviewer_id}'


def reviewer_invocation_id(
    run_id: str, sequence: int, reviewer_id: str, attempt: int
) -> str:
    """Return the durable invocation ID for one reviewer attempt."""

    if attempt < 1:
        message = 'reviewer attempt must be positive'
        raise ReviewerIdentityError(message)
    task_id = reviewer_task_id(run_id, sequence, reviewer_id)
    return f'{task_id}:attempt-{attempt:04d}'


def reviewer_invocation_stem(sequence: int, reviewer_id: str, attempt: int) -> str:
    """Return the shared filename stem for one reviewer attempt."""

    validate_reviewer_id(reviewer_id)
    if sequence < 1:
        message = 'reviewer sequence must be positive'
        raise ReviewerIdentityError(message)
    if attempt < 1:
        message = 'reviewer attempt must be positive'
        raise ReviewerIdentityError(message)
    return f'{sequence:06d}-reviewer-{reviewer_id}.attempt-{attempt:04d}'


@dataclass(frozen=True, slots=True)
class ReviewerEvidencePaths:
    """Contained relative paths owned by one reviewer attempt."""

    request: str
    result: str
    artifact: str
    stdout: str
    stderr: str
    runtime_metadata: str
    temporary_result: str


def reviewer_evidence_paths(
    *, sequence: int, iteration: int, reviewer_id: str, attempt: int
) -> ReviewerEvidencePaths:
    """Build every reviewer-qualified mutable and canonical evidence path."""

    validate_reviewer_id(reviewer_id)
    if sequence < 1 or iteration < 1 or attempt < 1:
        message = 'reviewer sequence, iteration, and attempt must be positive'
        raise ReviewerIdentityError(message)
    invocation_stem = reviewer_invocation_stem(sequence, reviewer_id, attempt)
    return ReviewerEvidencePaths(
        request=evidence_path(
            'review_request', ordinal=sequence, reviewer_id=reviewer_id
        ),
        result=evidence_path(
            'review_result', ordinal=sequence + 1, reviewer_id=reviewer_id
        ),
        artifact=f'artifacts/review-{iteration:04d}-{reviewer_id}.md',
        stdout=f'logs/{invocation_stem}.stdout.log',
        stderr=f'logs/{invocation_stem}.stderr.log',
        runtime_metadata=f'.{invocation_stem}.runtime.json',
        temporary_result=f'.{invocation_stem}.review-result.json',
    )
