"""Interfaces for bounded external agent invocations."""

from __future__ import annotations

import subprocess
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
    request_path: Path
    response_path: Path


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
    request_path: Path
    response_path: Path


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


@dataclass(frozen=True, slots=True)
class CommandAgentAdapter:
    """Invoke a role adapter command using the common typed boundary."""

    command: tuple[str, ...]

    def execute(self, request: AgentRequest) -> AgentResult:
        """Execute the configured adapter with canonical message paths."""

        completed = subprocess.run(
            [*self.command, str(request.request_path), str(request.response_path)],
            cwd=request.worktree_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=request.timeout_seconds,
        )
        return AgentResult(
            succeeded=completed.returncode == 0,
            summary='',
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
        )
