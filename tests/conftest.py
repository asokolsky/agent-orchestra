"""Shared pytest configuration guarding the developer's live evidence root."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_orchestra.cli import DEFAULT_RUNS_DIRECTORY

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _evidence_root_entries(root: Path) -> frozenset[str] | None:
    """Return the directories beneath an evidence root, or None when absent."""

    if not root.is_dir():
        return None
    # The walk is recursive because evidence is stored under UTC date shards, so
    # a leaked job appears at <root>/YYYY/MM/DD/<job-id> and leaves the root's
    # immediate entries unchanged. Comparing only the top level would miss every
    # leak on a root that already holds the current shard.
    #
    # Only directories are compared, and dot-prefixed names are ignored. A leak
    # always creates a new job directory, while locks, pending records, and
    # temporary files churn inside existing ones. Comparing every path instead
    # would fail the suite whenever an unrelated process touched the root.
    #
    # Dot-prefixed names are tested on the path relative to the root. Testing
    # the absolute path would match a dot component of the root itself, such as
    # the '.local' in the default location, and exclude every entry.
    directories = (path for path in root.rglob('*') if path.is_dir())
    return frozenset(
        str(relative)
        for relative in (path.relative_to(root) for path in directories)
        if not any(part.startswith('.') for part in relative.parts)
    )


@pytest.fixture(scope='session', autouse=True)
def guard_default_runs_directory() -> Iterator[None]:
    """Fail the session when a test writes to the configured evidence root."""

    # A command invoked without an explicit evidence root falls back to
    # DEFAULT_RUNS_DIRECTORY, a real directory belonging to whoever runs the
    # suite. The mistake is invisible in review because the option is simply
    # absent, and the resulting evidence is unreachable: its job row lives in a
    # temporary database that pytest discards, so no command can list or remove
    # it. Compare the root around the session so a leak fails here instead of
    # accumulating in a home directory.
    root = DEFAULT_RUNS_DIRECTORY.expanduser()
    before = _evidence_root_entries(root)
    yield
    after = _evidence_root_entries(root)
    if before is None and after is not None:
        pytest.fail(
            f'the test session created the live evidence root {root}; '
            'pass --runs-directory so evidence is written beneath tmp_path'
        )
    if before is not None and after is not None and after != before:
        added = sorted(after - before)[:5]
        removed = sorted(before - after)[:5]
        pytest.fail(
            f'the test session modified the live evidence root {root}; '
            f'added {added}, removed {removed}; '
            'pass --runs-directory so evidence is written beneath tmp_path'
        )
