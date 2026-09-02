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


def test_enqueue_locals_captures_changed_child_repositories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enqueue changed immediate child repos while skipping clean repos."""

    projects = tmp_path / 'projects'
    projects.mkdir()
    changed_b = projects / 'changed-b'
    changed_a = projects / 'changed-a'
    clean = projects / 'clean'
    not_a_repository = projects / 'notes'
    for repository in (changed_b, changed_a, clean):
        repository.mkdir()
        initialize_git_repository(repository)
    not_a_repository.mkdir()
    (changed_a / 'tracked.txt').write_text('changed a\n')
    (changed_b / 'untracked.txt').write_text('changed b\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    runs = RunStore(database).list_runs()
    assert {run.worktree_path for run in runs} == {changed_a, changed_b}
    output = capsys.readouterr().out
    assert output.index(str(changed_a)) < output.index(str(changed_b))
    assert (
        'enqueued 2 repositories; skipped 1 clean repository; failed 0 repositories'
    ) in output


def test_enqueue_locals_accepts_tilde_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expand a leading tilde in the projects directory argument."""

    projects = tmp_path / 'Projects'
    repository = projects / 'repo'
    repository.mkdir(parents=True)
    initialize_git_repository(repository)
    (repository / 'tracked.txt').write_text('changed\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', '~/Projects'])

    assert result == 0
    assert RunStore(database).list_runs()[0].worktree_path == repository
    assert 'enqueued 1 repository' in capsys.readouterr().out


def test_enqueue_locals_with_no_changed_repositories_does_not_create_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Succeed without state when no child repository has local changes."""

    projects = tmp_path / 'projects'
    repository = projects / 'clean'
    repository.mkdir(parents=True)
    initialize_git_repository(repository)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    assert not database.exists()
    assert (
        'enqueued 0 repositories; skipped 1 clean repository; failed 0 repositories'
    ) in capsys.readouterr().out


def test_enqueue_locals_continues_after_repository_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enqueue valid changes despite an unreadable sibling repository."""

    projects = tmp_path / 'projects'
    changed = projects / 'changed'
    fresh = projects / 'fresh'
    changed.mkdir(parents=True)
    fresh.mkdir()
    initialize_git_repository(changed)
    (changed / 'tracked.txt').write_text('changed\n')
    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', str(fresh)], check=True, capture_output=True)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    assert RunStore(database).list_runs()[0].worktree_path == changed
    captured = capsys.readouterr()
    assert f'error: {fresh}:' in captured.err
    assert (
        'enqueued 1 repository; skipped 0 clean repositories; failed 1 repository'
    ) in captured.out


def test_enqueue_locals_fails_when_every_repository_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return nonzero when no repository can be enqueued and one fails."""

    projects = tmp_path / 'projects'
    fresh = projects / 'fresh'
    fresh.mkdir(parents=True)
    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', str(fresh)], check=True, capture_output=True)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 2
    assert not database.exists()
    captured = capsys.readouterr()
    assert f'error: {fresh}:' in captured.err
    assert 'failed 1 repository' in captured.out


def test_enqueue_locals_reports_directory_without_repositories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinguish an empty repository scan from an all-clean scan."""

    projects = tmp_path / 'projects'
    (projects / 'notes').mkdir(parents=True)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    assert not database.exists()
    assert f'no Git repositories found in {projects}' in capsys.readouterr().out


def test_enqueue_locals_rejects_missing_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a stable error when the projects directory does not exist."""

    database = tmp_path / 'state.db'

    result = main(
        ['--database', str(database), 'enqueue-locals', str(tmp_path / 'missing')]
    )

    assert result == 2
    assert 'directory not found' in capsys.readouterr().err
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
