"""Domain models for orchestration runs and reviews."""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

BARE_SHA256 = re.compile(r'^[0-9a-f]{64}$')
REVIEW_FINDING_FIELDS = frozenset(
    {
        'finding_id',
        'severity',
        'title',
        'path',
        'line',
        'explanation',
        'acceptance_criterion',
    }
)


def utc_now() -> datetime:
    """Return a timezone-aware current timestamp."""

    return datetime.now(UTC)


def create_run_id(created_at: datetime) -> str:
    """Create a repo-independent identifier from UTC time and random entropy."""

    timestamp = created_at.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')
    return f'{timestamp}-{secrets.token_hex(4)}'


def normalize_diff_digest(diff_digest: str | None) -> str | None:
    """Return the canonical spelling of a current or legacy diff digest."""

    if diff_digest is not None and BARE_SHA256.fullmatch(diff_digest):
        return f'sha256:{diff_digest}'
    return diff_digest


def same_diff_digest(left: str | None, right: str | None) -> bool:
    """Compare diff digests while accepting the legacy bare SHA-256 form."""

    return normalize_diff_digest(left) == normalize_diff_digest(right)


class ScenarioType(StrEnum):
    """Supported orchestration entry points."""

    LOCAL_CHANGES = 'local_changes'
    PULL_REQUEST = 'pull_request'
    ISSUE_REVIEW = 'issue_review'


class RunState(StrEnum):
    """Persistent states in the orchestration lifecycle."""

    QUEUED = 'queued'
    PREPARING = 'preparing'
    DEVELOPING = 'developing'
    REVIEWING = 'reviewing'
    CHANGES_REQUESTED = 'changes_requested'
    APPROVED = 'approved'
    AWAITING_COMMIT_AUTHORIZATION = 'awaiting_commit_authorization'
    COMMITTED = 'committed'
    AWAITING_PUBLISH_AUTHORIZATION = 'awaiting_publish_authorization'
    PUBLISHED = 'published'
    VALIDATION_REQUIRED = 'validation_required'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    INTERRUPTED = 'interrupted'
    SUPERSEDED = 'superseded'


HUMAN_ACTION_STATES = frozenset(
    {
        RunState.CHANGES_REQUESTED,
        RunState.AWAITING_COMMIT_AUTHORIZATION,
        RunState.AWAITING_PUBLISH_AUTHORIZATION,
        RunState.VALIDATION_REQUIRED,
        RunState.INTERRUPTED,
    }
)


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

    finding_id: str
    severity: Severity
    title: str
    explanation: str
    acceptance_criterion: str
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
    validation: tuple[str, ...] = ()
    verification_gaps: tuple[str, ...] = ()
    created_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class Run:
    """Persistent orchestration run state."""

    id: str
    scenario: ScenarioType
    repo_path: Path
    worktree_path: Path
    state: RunState
    base_sha: str
    head_sha: str
    diff_digest: str | None = None
    iteration: int = 0
    remote_url: str | None = None
    supersedes_run_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @classmethod
    def create_local(
        cls,
        repo_path: Path,
        worktree_path: Path,
        base_sha: str,
        head_sha: str,
        diff_digest: str,
        *,
        supersedes_run_id: str | None = None,
    ) -> Run:
        """Create a queued run for local changes."""

        created_at = utc_now()
        return cls(
            id=create_run_id(created_at),
            scenario=ScenarioType.LOCAL_CHANGES,
            repo_path=repo_path.resolve(),
            worktree_path=worktree_path.resolve(),
            state=RunState.QUEUED,
            base_sha=base_sha,
            head_sha=head_sha,
            diff_digest=diff_digest,
            supersedes_run_id=supersedes_run_id,
            created_at=created_at,
            updated_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class IssueJob:
    """Persistent state for one immutable provider issue review."""

    id: str
    state: RunState
    provider: str
    host: str
    remote_url: str
    namespace: str
    project: str
    issue_number: int
    title: str
    author: str
    source_updated_at: str
    source_digest: str
    iteration: int = 0
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        host: str,
        remote_url: str,
        namespace: str,
        project: str,
        issue_number: int,
        title: str,
        author: str,
        source_updated_at: str,
        source_digest: str,
    ) -> IssueJob:
        """Create a queued issue-review job."""

        created_at = utc_now()
        return cls(
            id=create_run_id(created_at),
            state=RunState.QUEUED,
            provider=provider,
            host=host,
            remote_url=remote_url,
            namespace=namespace,
            project=project,
            issue_number=issue_number,
            title=title,
            author=author,
            source_updated_at=source_updated_at,
            source_digest=source_digest,
            created_at=created_at,
            updated_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class JobTransition:
    """One ordered persistent state transition for any job scenario."""

    job_id: str
    scenario: ScenarioType
    from_state: RunState | None
    to_state: RunState
    scope_digest: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderAction:
    """Durable identity of one issue-provider write."""

    job_id: str
    iteration: int
    action: str
    provider_id: str
    remote_url: str
    created_at: datetime = field(default_factory=utc_now)
