"""Interfaces for bounded external agent invocations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True, slots=True, kw_only=True)
class DeveloperRequest:
    """Input supplied to a development agent adapter."""

    role: Literal['developer'] = 'developer'
    objective: str
    worktree_path: Path
    iteration: int
    allowed_actions: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewerRequest:
    """Input supplied to a reviewer agent adapter."""

    role: Literal['reviewer'] = 'reviewer'
    objective: str
    worktree_path: Path
    iteration: int
    allowed_actions: tuple[str, ...]
    timeout_seconds: int
    base_sha: str
    head_sha: str
    diff_digest: str
    artifact_path: Path


type AgentRequest = DeveloperRequest | ReviewerRequest


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Normalized result of an agent invocation."""

    succeeded: bool
    summary: str
    stdout: str
    stderr: str
    exit_code: int


class AgentAdapter(Protocol):
    """Contract implemented by each external agent integration."""

    def execute(self, request: AgentRequest) -> AgentResult:
        """Run one bounded agent invocation."""
