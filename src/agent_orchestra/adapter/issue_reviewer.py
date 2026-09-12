"""Vendor-neutral contract helpers for issue-readiness reviewers."""

from __future__ import annotations

import json
from typing import Any

from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.errors import AgentOrchestraError
from agent_orchestra.manifests import role_assignment


class IssueReviewerError(AgentOrchestraError):
    """Raised when an issue reviewer cannot return a structured result."""

    def __init__(
        self,
        message: str,
        *,
        stdout: str = '',
        stderr: str = '',
        exit_code: int | None = None,
        timed_out: bool = False,
        interrupted: bool = False,
    ) -> None:
        """Create a failure carrying any available process evidence."""

        super().__init__(message)
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.interrupted = interrupted


def issue_review_prompt(request: dict[str, Any]) -> str:
    """
    Build the shared, provider- and runtime-neutral review assignment.

    The assignment text is packaged data rather than a literal here, so it can
    be versioned and reviewed. It is still inlined into the request: this role
    is granted no tools and must not need any to read its own instructions.
    """

    return role_assignment(
        RuntimeRole.ISSUE_REVIEWER.value,
        request=json.dumps(request, indent=2),
    )
