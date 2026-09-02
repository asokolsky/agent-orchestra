"""Domain models for orchestration runs and reviews."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def utc_now() -> datetime:
    """Return a timezone-aware current timestamp."""

    return datetime.now(UTC)


def create_run_id(created_at: datetime) -> str:
    """Create a repo-independent identifier from UTC time and random entropy."""

    timestamp = created_at.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')
    return f'{timestamp}-{secrets.token_hex(4)}'


class ScenarioType(StrEnum):
    """Supported orchestration entry points."""

    LOCAL_CHANGES = 'local_changes'
    PULL_REQUEST = 'pull_request'


class RunState(StrEnum):
    """Persistent states in the orchestration lifecycle."""

    QUEUED = 'queued'
    PREPARING = 'preparing'
    DEVELOPING = 'developing'
    AWAITING_REVIEW = 'awaiting_review'
    CHANGES_REQUESTED = 'changes_requested'
    APPROVED = 'approved'
    AWAITING_COMMIT_AUTHORIZATION = 'awaiting_commit_authorization'
    COMMITTED = 'committed'
    AWAITING_PUBLISH_AUTHORIZATION = 'awaiting_publish_authorization'
    PUBLISHED = 'published'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    INTERRUPTED = 'interrupted'
    SUPERSEDED = 'superseded'


class Verdict(StrEnum):
    """Possible outcomes of a review iteration."""

    APPROVED = 'approved'
    CHANGES_REQUESTED = 'changes_requested'
    BLOCKED = 'blocked'


class Severity(StrEnum):
    """Severity assigned to an individual review finding."""

    CRITICAL = 'critical'
    HIGH = 'high'
    MEDIUM = 'medium'
    LOW = 'low'


@dataclass(frozen=True, slots=True)
class Finding:
    """A structured reviewer finding."""

    severity: Severity
    title: str
    explanation: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class Review:
    """One review of an immutable diff digest."""

    run_id: str
    iteration: int
    diff_digest: str
    verdict: Verdict
    summary: str
    findings: tuple[Finding, ...] = ()
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class Run:
    """Persistent orchestration run state."""

    id: str
    scenario: ScenarioType
    repository_path: Path
    worktree_path: Path
    state: RunState
    base_sha: str
    head_sha: str
    diff_digest: str | None = None
    iteration: int = 0
    remote_url: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @classmethod
    def create_local(
        cls,
        repository_path: Path,
        worktree_path: Path,
        base_sha: str,
        head_sha: str,
        diff_digest: str,
    ) -> Run:
        """Create a queued run for local changes."""

        created_at = utc_now()
        return cls(
            id=create_run_id(created_at),
            scenario=ScenarioType.LOCAL_CHANGES,
            repository_path=repository_path.resolve(),
            worktree_path=worktree_path.resolve(),
            state=RunState.QUEUED,
            base_sha=base_sha,
            head_sha=head_sha,
            diff_digest=diff_digest,
            created_at=created_at,
            updated_at=created_at,
        )
