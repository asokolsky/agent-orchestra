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
    stdout_path: Path | None = None
    stderr_path: Path | None = None


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
    stdout_path: Path | None = None
    stderr_path: Path | None = None


type AgentRequest = DeveloperRequest | ReviewerRequest


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Normalized result of an agent invocation."""

    succeeded: bool
    summary: str
    stdout: str | None
    stderr: str | None
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

        command = [*self.command, str(request.request_path), str(request.response_path)]
        if request.stdout_path is None or request.stderr_path is None:
            captured = subprocess.run(
                command,
                cwd=request.worktree_path,
                check=False,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
            )
            stdout = captured.stdout
            stderr = captured.stderr
            exit_code = captured.returncode
        else:
            with (
                request.stdout_path.open('wb') as stdout_file,
                request.stderr_path.open('wb') as stderr_file,
            ):
                redirected = subprocess.run(
                    command,
                    cwd=request.worktree_path,
                    check=False,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    timeout=request.timeout_seconds,
                )
            stdout = None
            stderr = None
            exit_code = redirected.returncode
        return AgentResult(
            succeeded=exit_code == 0,
            summary='',
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
        )
