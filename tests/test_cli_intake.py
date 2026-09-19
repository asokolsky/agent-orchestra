"""Tests for command-line initialization and local intake."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from importlib.metadata import version
from typing import TYPE_CHECKING

from agent_orchestra.cli import (
    _working_tree_digest,
    main,
)
from agent_orchestra.store import JobStore
from tests.cli_helpers import (
    add_linked_worktree,
    initialize_git_repo,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


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

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    (repo / 'untracked.txt').write_text('new\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(repo)])

    assert result == 0
    run = JobStore(database).list_runs()[0]
    assert re.fullmatch(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}', run.id)
    assert run.diff_digest is not None
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', run.diff_digest)
    assert run.base_sha == run.head_sha
    assert run.repo_path == repo
    assert run.worktree_path == repo
    assert str(run.id) in capsys.readouterr().out


def test_enqueue_local_from_subdirectory_captures_complete_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Capture changes outside a caller-supplied worktree subdirectory."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    subdirectory = repo / 'nested'
    subdirectory.mkdir()
    (repo / 'outside.txt').write_text('new\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(subdirectory)])

    assert result == 0
    run = JobStore(database).list_runs()[0]
    assert run.repo_path == repo
    assert run.worktree_path == repo
    assert run.diff_digest is not None
    assert str(run.id) in capsys.readouterr().out


def test_enqueue_local_distinguishes_linked_worktree_from_primary_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist the primary and selected linked-worktree paths independently."""

    repo = tmp_path / 'primary repository'
    repo.mkdir()
    initialize_git_repo(repo)
    worktree = tmp_path / 'linked worktree'
    add_linked_worktree(repo, worktree)
    (worktree / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(worktree)])

    assert result == 0
    run = JobStore(database).list_runs()[0]
    assert run.repo_path == repo
    assert run.worktree_path == worktree
    assert str(run.id) in capsys.readouterr().out


def test_enqueue_local_uses_bare_repo_backing_linked_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Use a bare main repository when it owns the selected linked worktree."""

    source = tmp_path / 'source'
    source.mkdir()
    initialize_git_repo(source)
    bare_repo = tmp_path / 'bare repository.git'
    git = shutil.which('git')
    assert git is not None
    subprocess.run(
        [git, 'clone', '--bare', str(source), str(bare_repo)],
        check=True,
        capture_output=True,
    )
    worktree = tmp_path / 'bare linked worktree'
    add_linked_worktree(bare_repo, worktree)
    (worktree / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(worktree)])

    assert result == 0
    run = JobStore(database).list_runs()[0]
    assert run.repo_path == bare_repo
    assert run.worktree_path == worktree
    assert str(run.id) in capsys.readouterr().out


def test_enqueue_local_supports_separate_git_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Identify a primary worktree whose Git directory lives elsewhere."""

    repo = tmp_path / 'separate worktree'
    git_directory = tmp_path / 'separate metadata.git'
    git = shutil.which('git')
    assert git is not None
    subprocess.run(
        [git, 'init', f'--separate-git-dir={git_directory}', str(repo)],
        check=True,
        capture_output=True,
    )
    (repo / 'tracked.txt').write_text('initial\n')
    subprocess.run(
        [git, '-C', str(repo), 'add', 'tracked.txt'],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            git,
            '-C',
            str(repo),
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
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(repo)])

    assert result == 0
    run = JobStore(database).list_runs()[0]
    assert run.repo_path == repo
    assert run.worktree_path == repo
    assert run.repo_path != git_directory
    assert str(run.id) in capsys.readouterr().out


def test_untracked_executable_mode_changes_working_tree_digest(tmp_path: Path) -> None:
    """Bind approval digests to executable-mode changes on untracked files."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    script = repo / 'script.sh'
    script.write_text('#!/bin/sh\n')
    before = _working_tree_digest(repo, 'HEAD')

    script.chmod(script.stat().st_mode | 0o100)
    after = _working_tree_digest(repo, 'HEAD')

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


def test_enqueue_local_rejects_clean_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Do not enqueue an empty local-change scope."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-local', str(repo)])

    assert result == 2
    assert 'no local changes' in capsys.readouterr().err
    assert not database.exists()


def test_enqueue_local_records_terminal_run_lineage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Link an exceptional replacement to the failed run it supersedes."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('first change\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    predecessor = JobStore(database).list_runs()[0]
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET state = 'failed' WHERE id = ?", (predecessor.id,)
        )
    (repo / 'tracked.txt').write_text('replacement change\n')

    result = main(
        [
            '--database',
            str(database),
            'enqueue-local',
            str(repo),
            '--supersedes',
            str(predecessor.id),
        ]
    )

    assert result == 0
    replacement = JobStore(database).list_runs()[0]
    assert replacement.id != predecessor.id
    assert replacement.supersedes_run_id == predecessor.id
    assert str(replacement.id) in capsys.readouterr().out


def test_enqueue_local_supersedes_errors_use_job_vocabulary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Use the public job noun when a predecessor cannot be found."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('change\n')
    database = tmp_path / 'missing.db'

    result = main(
        [
            '--database',
            str(database),
            'enqueue-local',
            str(repo),
            '--supersedes',
            'missing-job',
        ]
    )

    assert result == 2
    assert capsys.readouterr().err == 'error: job not found: missing-job\n'
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
    not_a_repo = projects / 'notes'
    for repo in (changed_b, changed_a, clean):
        repo.mkdir()
        initialize_git_repo(repo)
    not_a_repo.mkdir()
    (changed_a / 'tracked.txt').write_text('changed a\n')
    (changed_b / 'untracked.txt').write_text('changed b\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    runs = JobStore(database).list_runs()
    assert {run.worktree_path for run in runs} == {changed_a, changed_b}
    output = json.loads(capsys.readouterr().out)
    assert output == {
        'schema_version': 23,
        'agent_orchestra_version': version('py-agent-orchestra'),
        'directory': str(projects),
        'jobs': [
            {'job_id': str(runs[1].id), 'worktree_path': str(changed_a)},
            {'job_id': str(runs[0].id), 'worktree_path': str(changed_b)},
        ],
        'summary': {'enqueued': 2, 'clean': 1, 'failed': 0},
        'failures': [],
        'error': None,
    }


def test_enqueue_locals_distinguishes_linked_worktree_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Apply repository identity resolution to batch-enqueued worktrees."""

    repo = tmp_path / 'primary'
    repo.mkdir()
    initialize_git_repo(repo)
    projects = tmp_path / 'projects'
    projects.mkdir()
    worktree = projects / 'linked'
    add_linked_worktree(repo, worktree)
    (worktree / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    run = JobStore(database).list_runs()[0]
    assert run.repo_path == repo
    assert run.worktree_path == worktree
    assert json.loads(capsys.readouterr().out)['jobs'] == [
        {'job_id': str(run.id), 'worktree_path': str(worktree)}
    ]


def test_enqueue_locals_accepts_tilde_directory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expand a leading tilde in the projects directory argument."""

    projects = tmp_path / 'Projects'
    repo = projects / 'repo'
    repo.mkdir(parents=True)
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    monkeypatch.setenv('HOME', str(tmp_path))
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', '~/Projects'])

    assert result == 0
    assert JobStore(database).list_runs()[0].worktree_path == repo
    assert json.loads(capsys.readouterr().out)['summary']['enqueued'] == 1


def test_enqueue_locals_with_no_changed_repositories_does_not_create_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Succeed without state when no child repository has local changes."""

    projects = tmp_path / 'projects'
    repo = projects / 'clean'
    repo.mkdir(parents=True)
    initialize_git_repo(repo)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    assert not database.exists()
    assert json.loads(capsys.readouterr().out)['summary'] == {
        'enqueued': 0,
        'clean': 1,
        'failed': 0,
    }


def test_enqueue_locals_continues_after_repo_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enqueue valid changes despite an unreadable sibling repository."""

    projects = tmp_path / 'projects'
    changed = projects / 'changed'
    fresh = projects / 'fresh'
    changed.mkdir(parents=True)
    fresh.mkdir()
    initialize_git_repo(changed)
    (changed / 'tracked.txt').write_text('changed\n')
    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', str(fresh)], check=True, capture_output=True)
    database = tmp_path / 'state.db'

    result = main(['--database', str(database), 'enqueue-locals', str(projects)])

    assert result == 0
    assert JobStore(database).list_runs()[0].worktree_path == changed
    captured = capsys.readouterr()
    assert captured.err == ''
    document = json.loads(captured.out)
    assert document['summary'] == {'enqueued': 1, 'clean': 0, 'failed': 1}
    assert document['failures'][0]['repository_path'] == str(fresh)
    assert document['failures'][0]['message']


def test_enqueue_locals_fails_when_every_repo_fails(
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
    assert captured.err == ''
    document = json.loads(captured.out)
    assert document['summary'] == {'enqueued': 0, 'clean': 0, 'failed': 1}
    assert document['failures'][0]['repository_path'] == str(fresh)


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
    document = json.loads(capsys.readouterr().out)
    assert document['directory'] == str(projects)
    assert document['jobs'] == []
    assert document['summary'] == {'enqueued': 0, 'clean': 0, 'failed': 0}
    assert document['failures'] == []
    assert document['error'] is None


def test_enqueue_locals_rejects_missing_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a stable error when the projects directory does not exist."""

    database = tmp_path / 'state.db'

    result = main(
        ['--database', str(database), 'enqueue-locals', str(tmp_path / 'missing')]
    )

    assert result == 2
    captured = capsys.readouterr()
    assert captured.err == ''
    document = json.loads(captured.out)
    assert document['error']['code'] == 'directory_not_found'
    assert 'directory not found' in document['error']['message']
    assert not database.exists()
