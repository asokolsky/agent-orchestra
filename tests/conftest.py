"""Shared pytest configuration isolating tests from live developer state."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_orchestra import cli

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(scope='session', autouse=True)
def isolated_settings_source(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Hide the developer's settings file from every command the suite runs."""

    # Redirecting the module defaults is not enough on its own: load_settings
    # reads $XDG_CONFIG_HOME/agent-orchestra/config.toml, and a configured
    # storage.database or storage.runs_directory overrides the patched value.
    # A developer with a settings file would otherwise have the suite write to
    # whatever it names, past both guards below. Point the search at an empty
    # session directory so no settings file is found and the built-in defaults
    # win. Tests that need configuration set XDG_CONFIG_HOME themselves, which
    # a function-scoped monkeypatch does over this one.
    config_home = tmp_path_factory.mktemp('config-home')
    patch = pytest.MonkeyPatch()
    patch.setenv('XDG_CONFIG_HOME', str(config_home))
    yield config_home
    patch.undo()


@pytest.fixture(scope='session', autouse=True)
def isolated_default_runs_directory(
    isolated_settings_source: Path,
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


@pytest.fixture(scope='session', autouse=True)
def isolated_default_database(
    isolated_settings_source: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Redirect the default state database and fail when a test writes to it."""

    # DEFAULT_DATABASE sits beside DEFAULT_RUNS_DIRECTORY and needs the same
    # protection for a stronger reason: an evidence leak only appends orphan
    # directories, while a stray write here mutates durable job state in a
    # database the developer depends on, and leaves nothing to count afterwards.
    #
    # As with the evidence guard, the real database is never read or stat-ed, so
    # concurrent Agent Orchestra activity cannot fail the session.
    session_database = tmp_path_factory.mktemp('default-database') / 'state.db'
    patch = pytest.MonkeyPatch()
    patch.setattr(cli, 'DEFAULT_DATABASE', session_database)
    yield session_database
    patch.undo()
    # SQLite may leave -wal and -shm beside the database, so match on the stem
    # rather than the exact name.
    written = sorted(
        path.name
        for path in session_database.parent.iterdir()
        if path.name.startswith(session_database.name)
    )
    if written:
        pytest.fail(
            'the test session wrote to the default state database: '
            f'{written}; pass --database so state is written beneath tmp_path'
        )
