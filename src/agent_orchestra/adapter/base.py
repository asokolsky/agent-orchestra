"""Shared abstract interfaces for canonical agent role adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class ReviewerAdapter(ABC):
    """Abstract runtime adapter for one canonical code-review request."""

    @abstractmethod
    def execute(self, request_path: Path, response_path: Path) -> None:
        """Execute a review and persist its canonical response."""


class DeveloperAdapter(ABC):
    """Abstract runtime adapter for one canonical development request."""

    @abstractmethod
    def execute(self, request_path: Path, response_path: Path) -> None:
        """Execute development and persist its canonical response."""


@dataclass(frozen=True, slots=True)
class IssueReviewExecution:
    """Structured result plus truthful process evidence for an issue review."""

    result: dict[str, Any]
    stdout: str
    stderr: str
    exit_code: int
    effective_models: tuple[str, ...] = ()


class IssueReviewerAdapter(ABC):
    """Abstract runtime adapter for one bounded issue-readiness review."""

    @abstractmethod
    def execute(self, request: dict[str, Any], *, timeout: int) -> IssueReviewExecution:
        """Return a structured issue review and its process evidence."""
