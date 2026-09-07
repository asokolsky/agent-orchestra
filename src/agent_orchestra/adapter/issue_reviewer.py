"""Vendor-neutral contract helpers for issue-readiness reviewers."""

from __future__ import annotations

import json
from typing import Any


class IssueReviewerError(RuntimeError):
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
    """Build the shared, provider- and runtime-neutral review assignment."""

    return f"""Review the issue snapshot in the request below for implementation readiness.
This is issue-prose review, not code review. Do not modify files, access the network,
post feedback, or invent file and line locations. Evaluate problem clarity, scope,
constraints, dependencies, risks, acceptance criteria, testability, and implementation
readiness. Return only the JSON object required by the output schema. A ready verdict
must have no findings; changes_requested must have actionable findings. Preserve the
request source_digest exactly in the result.

Issue review request:
{json.dumps(request, indent=2)}
"""
