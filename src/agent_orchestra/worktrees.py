"""Read-only health checks for persisted Git worktree bindings."""

from __future__ import annotations

import shutil
import subprocess
from enum import StrEnum
from pathlib import Path


class WorktreeStatus(StrEnum):
    """Observed availability of a source-code job's bound worktree."""

    AVAILABLE = 'available'
    MISSING = 'missing'
    NOT_GIT_WORKTREE = 'not_git_worktree'


def worktree_status(path: Path) -> WorktreeStatus:
    """Classify a recorded worktree path without changing repository state."""

    if not path.exists():
        return WorktreeStatus.MISSING
    if not path.is_dir():
        return WorktreeStatus.NOT_GIT_WORKTREE
    git = shutil.which('git')
    if git is None:
        return WorktreeStatus.NOT_GIT_WORKTREE
    try:
        result = subprocess.run(
            [git, '-C', str(path), 'rev-parse', '--show-toplevel'],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        top_level = Path(result.stdout.strip()).resolve()
    except OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired:
        return WorktreeStatus.NOT_GIT_WORKTREE
    return (
        WorktreeStatus.AVAILABLE
        if top_level == path.resolve()
        else WorktreeStatus.NOT_GIT_WORKTREE
    )
