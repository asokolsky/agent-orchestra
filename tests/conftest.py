"""Shared pytest configuration isolating tests from the live evidence root."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_orchestra import cli

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(scope='session', autouse=True)
def isolated_default_runs_directory(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Redirect the default evidence root and fail when a test writes to it."""

    # A command invoked without an explicit evidence root falls back to
    # DEFAULT_RUNS_DIRECTORY, which in production is a real directory belonging
    # to whoever runs the suite. Point it at a session-owned directory instead,
    # so the mistake cannot reach a home directory at all, and fail the session
    # when anything lands there.
    #
    # Watching the live directory instead would make the suite fail whenever a
    # concurrent Agent Orchestra process created or removed a job while the
    # session happened to be running, which is plausible with several worktrees
    # in use.
    session_root = tmp_path_factory.mktemp('default-runs-directory')
    patch = pytest.MonkeyPatch()
    patch.setattr(cli, 'DEFAULT_RUNS_DIRECTORY', session_root)
    yield session_root
    patch.undo()
    written = sorted(
        path.relative_to(session_root).as_posix() for path in session_root.rglob('*')
    )
    if written:
        pytest.fail(
            'the test session wrote to the default evidence root: '
            f'{written[:5]}; pass --runs-directory so evidence is written '
            'beneath tmp_path'
        )
