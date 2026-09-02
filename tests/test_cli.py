"""Tests for command-line operations."""

import shutil
import subprocess
from typing import TYPE_CHECKING
from uuid import uuid4

from agent_orchestra.cli import _working_tree_digest, main
from agent_orchestra.models import Run
from agent_orchestra.store import RunStore

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def initialize_git_repository(path: Path) -> None:
    """Create a repository with one committed file."""

    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', str(path)], check=True, capture_output=True)
    (path / 'tracked.txt').write_text('initial\n')
    subprocess.run([git, '-C', str(path), 'add', 'tracked.txt'], check=True)
    subprocess.run(
        [
            git,
            '-C',
            str(path),
            '-c',
            'user.name=Test User',
            '-c',
            'user.email=test@example.invalid',
            'commit',
            '-m',
            'initial',
        ],
        check=True,
        capture_output=True,
    )


def test_init_creates_database(tmp_path: Path) -> None:
    """Initialize the requested state database."""

    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'init'])

    assert result == 0
    assert database.exists()


def test_enqueue_local_captures_current_diff(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist a digest for tracked and untracked local changes."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    (repository / 'untracked.txt').write_text('new\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(repository)])

    assert result == 0
    run = RunStore(database).list_runs()[0]
    assert run.diff_digest is not None
    assert run.base_sha == run.head_sha
    assert str(run.id) in capsys.readouterr().out


def test_untracked_executable_mode_changes_working_tree_digest(tmp_path: Path) -> None:
    """Bind approval digests to executable-mode changes on untracked files."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    script = repository / 'script.sh'
    script.write_text('#!/bin/sh\n')
    before = _working_tree_digest(repository, 'HEAD')

    script.chmod(script.stat().st_mode | 0o100)
    after = _working_tree_digest(repository, 'HEAD')

    assert before is not None
    assert after is not None
    assert after != before


def test_enqueue_local_reports_git_failure_without_creating_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a stable error for a path that is not a Git repository."""

    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(tmp_path)])

    assert result == 2
    assert 'error:' in capsys.readouterr().err
    assert not database.exists()


def test_enqueue_local_rejects_clean_repository(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Do not enqueue an empty local-change scope."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repository(repository)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(repository)])

    assert result == 2
    assert 'no local changes' in capsys.readouterr().err
    assert not database.exists()


def test_status_does_not_create_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep status read-only when no state database exists."""

    database = tmp_path / 'missing' / 'state.db'

    result = main(['--database', str(database), 'status'])

    assert result == 2
    assert 'state database not found' in capsys.readouterr().err
    assert not database.exists()


def test_status_lists_persisted_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Display a persisted run without changing state."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)

    result = main(['--database', str(database), 'status'])

    assert result == 0
    output = capsys.readouterr().out
    assert str(run.id) in output
    assert 'queued' in output


def test_status_reports_unknown_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a distinct error when the requested run does not exist."""

    database = tmp_path / 'state.db'
    RunStore(database).initialize()

    result = main(['--database', str(database), 'status', str(uuid4())])

    assert result == 2
    assert 'run not found' in capsys.readouterr().err


def test_skills_install_for_both_agents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Install a requested skill through the public CLI."""

    source = tmp_path / 'source'
    skill = source / 'example-skill'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('instructions\n')
    codex_home = tmp_path / 'codex'
    claude_home = tmp_path / 'claude'

    result = main(
        [
            'skills',
            'install',
            '--agent',
            'all',
            '--skill',
            'example-skill',
            '--source',
            str(source),
            '--codex-home',
            str(codex_home),
            '--claude-home',
            str(claude_home),
        ]
    )

    assert result == 0
    assert 'installed example-skill for codex' in capsys.readouterr().out
    assert (codex_home / 'skills/example-skill/SKILL.md').is_file()
    assert (claude_home / 'skills/example-skill/SKILL.md').is_file()
