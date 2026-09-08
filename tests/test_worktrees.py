"""Tests for read-only persisted worktree health checks."""

from typing import TYPE_CHECKING

from agent_orchestra.worktrees import WorktreeStatus, worktree_status
from tests.test_cli import initialize_git_repo

if TYPE_CHECKING:
    from pathlib import Path


def test_worktree_status_distinguishes_missing_and_non_git_paths(
    tmp_path: Path,
) -> None:
    """Report absence separately from a present directory without Git metadata."""

    missing = tmp_path / 'missing'
    plain = tmp_path / 'plain'
    plain.mkdir()
    repository = tmp_path / 'repository'
    repository.mkdir()
    initialize_git_repo(repository)

    assert worktree_status(missing) is WorktreeStatus.MISSING
    assert worktree_status(plain) is WorktreeStatus.NOT_GIT_WORKTREE
    assert worktree_status(repository) is WorktreeStatus.AVAILABLE
