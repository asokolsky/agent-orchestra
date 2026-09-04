"""Tests for command-line operations."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import fields
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import pytest

from agent_orchestra.agents import AgentRequest, AgentResult, CommandAgentAdapter
from agent_orchestra.cli import DEFAULT_DATABASE, _working_tree_digest, main
from agent_orchestra.invocations import InvocationIdentity
from agent_orchestra.models import Run
from agent_orchestra.store import RunStore
from agent_orchestra.worker import (
    ITERATION_LIMIT,
    NO_REMEDIATION_CHANGE,
    WorkerError,
    run_queued_review,
)


def test_version_reports_installed_distribution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report the package version without requiring a subcommand."""

    with pytest.raises(SystemExit) as raised:
        main(['--version'])

    assert raised.value.code == 0
    assert capsys.readouterr().out == f'agent-orchestra {version("agent-orchestra")}\n'


def initialize_git_repo(path: Path) -> None:
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


def add_linked_worktree(repo: Path, worktree: Path) -> None:
    """Create a linked worktree on a unique test branch."""

    git = shutil.which('git')
    assert git is not None
    subprocess.run(
        [
            git,
            '-C',
            str(repo),
            'worktree',
            'add',
            '-b',
            f'test-{uuid4().hex}',
            str(worktree),
        ],
        check=True,
        capture_output=True,
    )


def write_reviewer(path: Path, verdict: str) -> None:
    """Write a deterministic reviewer command for CLI integration tests."""

    path.write_text(
        f'''"""Test reviewer command."""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

request_path = Path(sys.argv[1])
response_path = Path(sys.argv[2])
request = json.loads(request_path.read_text())
artifact_path = Path(request["payload"]["artifact_path"])
artifact_path.write_text("# Review\\n")
response = {{
    "schema_version": 1,
    "message_id": str(uuid4()),
    "in_reply_to": request["message_id"],
    "run_id": request["run_id"],
    "sequence": request["sequence"] + 1,
    "iteration": request["iteration"],
    "message_type": "review_result",
    "sender": "reviewer",
    "recipient": "orchestrator",
    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "scope": request["scope"],
    "payload": {{
        "verdict": "{verdict}",
        "summary": "reviewed",
        "findings": [] if "{verdict}" == "approved" else [{{
            "finding_id": "F-001",
            "severity": "medium",
            "title": "fix",
            "path": "tracked.txt",
            "line": 1,
            "explanation": "Needs correction.",
            "acceptance_criterion": "Correct the content.",
        }}],
        "validation": [],
        "verification_gaps": [],
        "artifact_path": str(artifact_path),
    }},
}}
response_path.write_text(json.dumps(response))
'''
    )


def write_loop_reviewer(path: Path) -> None:
    """Write a reviewer that requests one remediation and then approves."""

    write_reviewer(path, 'approved')
    content = path.read_text()
    content = content.replace(
        '"verdict": "approved",',
        '"verdict": "changes_requested" if request["iteration"] == 1 else "approved",',
    ).replace(
        '[] if "approved" == "approved" else [{',
        '[] if request["iteration"] > 1 else [{',
    )
    path.write_text(content)


def write_developer(
    path: Path, *, change_worktree: bool = True, disposition: str = 'addressed'
) -> None:
    """Write a deterministic developer that changes the diff and hands off."""

    edit = (
        '(worktree / "tracked.txt").write_text("remediated\\n")'
        if change_worktree
        else 'pass'
    )
    path.write_text(
        f'''"""Test developer command."""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

request_path = Path(sys.argv[1])
response_path = Path(sys.argv[2])
request = json.loads(request_path.read_text())
worktree = Path(request["scope"]["worktree_path"])
{edit}
review = json.loads(Path(request["payload"]["review_result_path"]).read_text())
response = {{
    "schema_version": 1,
    "message_id": str(uuid4()),
    "in_reply_to": request["message_id"],
    "run_id": request["run_id"],
    "sequence": request["sequence"] + 1,
    "iteration": request["iteration"],
    "message_type": "developer_handoff",
    "sender": "developer",
    "recipient": "orchestrator",
    "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "scope": request["scope"],
    "payload": {{
        "status": "ready_for_review",
        "summary": "remediated",
        "files_changed": ["tracked.txt"],
        "validation": [],
        "dispositions": [
            {{"finding_id": item["finding_id"], "disposition": "{disposition}", "rationale": "evaluated"}}
            for item in review["payload"]["findings"]
        ],
        "remaining_risks": [],
    }},
}}
response_path.write_text(json.dumps(response))
'''
    )


def write_fake_codex(path: Path, *, mode: str) -> None:
    """Write a fake Codex executable that emits identifiable child output."""

    terminal_statement = {
        'approved': '',
        'nonzero': 'raise SystemExit(9)',
        'timeout': 'time.sleep(5)',
    }[mode]
    path.parent.mkdir(parents=True)
    path.write_text(
        f'''#!/usr/bin/env python3
"""Fake Codex process for built-in adapter integration tests."""
import json
import sys
import time
from pathlib import Path

sys.stdin.read()
print("child stdout", flush=True)
print("child stderr", file=sys.stderr, flush=True)
{terminal_statement}
result_path = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
result_path.write_text(json.dumps({{
    "verdict": "approved",
    "summary": "Ready.",
    "findings": [],
    "validation": [],
    "verification_gaps": [],
}}))
'''
    )
    path.chmod(0o755)


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
    run = RunStore(database).list_runs()[0]
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
    run = RunStore(database).list_runs()[0]
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
    run = RunStore(database).list_runs()[0]
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
    run = RunStore(database).list_runs()[0]
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
    run = RunStore(database).list_runs()[0]
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
    runs = RunStore(database).list_runs()
    assert {run.worktree_path for run in runs} == {changed_a, changed_b}
    output = json.loads(capsys.readouterr().out)
    assert output == {
        'schema_version': 3,
        'directory': str(projects),
        'runs': [
            {'id': str(runs[1].id), 'worktree_path': str(changed_a)},
            {'id': str(runs[0].id), 'worktree_path': str(changed_b)},
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
    run = RunStore(database).list_runs()[0]
    assert run.repo_path == repo
    assert run.worktree_path == worktree
    assert json.loads(capsys.readouterr().out)['runs'] == [
        {'id': str(run.id), 'worktree_path': str(worktree)}
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
    assert RunStore(database).list_runs()[0].worktree_path == repo
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
    assert RunStore(database).list_runs()[0].worktree_path == changed
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
    assert document['runs'] == []
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
    assert output.startswith('{\n  "schema_version": 3,\n  "runs": [\n    {\n')
    assert output.endswith('\n}\n')
    document = json.loads(output)
    expected_fields = {
        'repository_path' if field.name == 'repo_path' else field.name
        for field in fields(Run)
    }
    assert set(document['runs'][0]) == expected_fields
    assert document == {
        'schema_version': 3,
        'runs': [
            {
                'id': str(run.id),
                'scenario': 'local_changes',
                'repository_path': str(tmp_path),
                'worktree_path': str(tmp_path),
                'state': 'queued',
                'base_sha': 'base',
                'head_sha': 'head',
                'diff_digest': 'digest',
                'iteration': 0,
                'remote_url': None,
                'created_at': run.created_at.isoformat().replace('+00:00', 'Z'),
                'updated_at': run.updated_at.isoformat().replace('+00:00', 'Z'),
            }
        ],
    }


def test_status_filters_json_document_by_run_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep single-run status output in the versioned runs envelope."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    first = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'first')
    second = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'second')
    store.add(first)
    store.add(second)

    result = main(['--database', str(database), 'status', str(first.id)])

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == 3
    assert [run['id'] for run in document['runs']] == [str(first.id)]


def test_status_reads_legacy_review_state_without_initializing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose the renamed state without requiring a separate init command."""

    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET state = 'awaiting_review' WHERE id = ?", (str(run.id),)
        )

    result = main(['--database', str(database), 'status', str(run.id)])

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == 3
    assert document['runs'][0]['state'] == 'reviewing'
    with sqlite3.connect(database) as connection:
        stored_state = connection.execute(
            'SELECT state FROM runs WHERE id = ?', (str(run.id),)
        ).fetchone()
    assert stored_state == ('awaiting_review',)


def test_status_lists_empty_runs_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a stable empty collection for an initialized database."""

    database = tmp_path / 'state.db'
    RunStore(database).initialize()

    result = main(['--database', str(database), 'status'])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 3,
        'runs': [],
    }


def test_status_reports_unknown_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a distinct error when the requested run does not exist."""

    database = tmp_path / 'state.db'
    RunStore(database).initialize()

    result = main(['--database', str(database), 'status', str(uuid4())])

    assert result == 2
    assert 'run not found' in capsys.readouterr().err


def test_run_dispatches_review_and_awaits_commit_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist a correlated approved review and stop at the commit gate."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    runs_directory = tmp_path / 'runs'

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(runs_directory),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 0
    assert RunStore(database).get(run.id).state.value == 'awaiting_commit_authorization'
    run_directory = runs_directory / str(run.id)
    assert (run_directory / 'messages/000001-review-request.json').is_file()
    assert (run_directory / 'messages/000002-review-result.json').is_file()
    assert (run_directory / 'artifacts/review-0001.md').is_file()
    invocation = json.loads(
        (run_directory / 'invocations/000001-reviewer.json').read_text()
    )
    assert invocation['run_id'] == str(run.id)
    assert invocation['role'] == 'reviewer'
    assert invocation['agent_vendor'] == 'unknown'
    assert invocation['runtime'] == 'custom-command'
    assert invocation['exit_code'] == 0
    assert invocation['timed_out'] is False
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 3,
        'run_id': str(run.id),
        'state': 'awaiting_commit_authorization',
    }


def test_run_preserves_non_utf8_reviewer_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Archive the exact bytes written by a redirected reviewer process."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    reviewer.write_bytes(
        reviewer.read_bytes().replace(
            b'import sys\n',
            b'import sys\nsys.stdout.buffer.write(b"caf\\xe9 latin-1 byte\\n")\n'
            b'sys.stdout.buffer.flush()\n',
            1,
        )
    )
    runs_directory = tmp_path / 'runs'

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(runs_directory),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 0
    stdout_log = runs_directory / str(run.id) / 'logs/000001-reviewer.stdout.log'
    assert stdout_log.read_bytes() == b'caf\xe9 latin-1 byte\n'


@pytest.mark.parametrize('interrupted_role', ['reviewer', 'developer'])
def test_worker_persists_interrupted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_role: str,
) -> None:
    """Keep durable run state aligned with interrupted invocation evidence."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    digest = _working_tree_digest(repo, 'HEAD')
    assert digest is not None
    run = Run.create_local(repo, repo, 'HEAD', 'HEAD', digest)
    store.add(run)
    reviewer = tmp_path / 'reviewer.py'
    write_loop_reviewer(reviewer)
    original_execute = CommandAgentAdapter.execute

    def interrupt_selected(
        adapter: CommandAgentAdapter, request: AgentRequest
    ) -> AgentResult:
        """Interrupt only the requested role."""

        if request.role == interrupted_role:
            raise KeyboardInterrupt
        return original_execute(adapter, request)

    monkeypatch.setattr(CommandAgentAdapter, 'execute', interrupt_selected)
    runs_directory = tmp_path / 'runs'

    with pytest.raises(KeyboardInterrupt):
        run_queued_review(
            store=store,
            run=run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=('unused-developer',),
            runs_directory=runs_directory,
            timeout_seconds=30,
            digest_worktree=_working_tree_digest,
        )

    assert store.get(run.id).state.value == 'interrupted'
    invocation_files = sorted(
        (runs_directory / str(run.id) / 'invocations').glob('*.json')
    )
    invocation = json.loads(invocation_files[-1].read_text())
    assert invocation['role'] == interrupted_role
    assert invocation['interrupted'] is True


def test_run_uses_builtin_codex_adapter_by_default(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Select the packaged Codex adapter when no custom command is supplied."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    observed: dict[str, object] = {}

    def review(**kwargs: object) -> Run:
        """Capture the selected command without starting Codex."""

        observed.update(kwargs)
        return run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_review', review)

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
        ]
    )

    assert result == 0
    assert observed['reviewer_command'] == [
        sys.executable,
        '-m',
        'agent_orchestra.adapter.codex',
    ]
    identity = observed['reviewer_identity']
    assert isinstance(identity, InvocationIdentity)
    assert identity.vendor == 'openai'
    assert identity.model is None
    assert identity.runtime == 'codex'


def test_default_database_is_outside_a_repo_in_the_home_directory() -> None:
    """Keep default orchestration state outside a typical reviewed repo."""

    repo = Path.home() / 'Projects/repo'

    assert Path.home() / '.local/state/agent-orchestra/state.db' == DEFAULT_DATABASE
    assert not DEFAULT_DATABASE.is_relative_to(repo)


def test_run_passes_explicit_reviewer_model_to_codex_adapter(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow callers to avoid an incompatible managed Codex model default."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    observed: dict[str, object] = {}

    def review(**kwargs: object) -> Run:
        """Capture the selected command without starting Codex."""

        observed.update(kwargs)
        return run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_review', review)

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--reviewer-model',
            'compatible-model',
        ]
    )

    assert result == 0
    assert observed['reviewer_command'] == [
        sys.executable,
        '-m',
        'agent_orchestra.adapter.codex',
        '--model',
        'compatible-model',
    ]


def test_run_selects_claude_code_reviewer_independently(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Select Claude Code and its role-specific model without changing state."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    observed: dict[str, object] = {}

    def review(**kwargs: object) -> Run:
        """Capture the selected command without starting Claude Code."""

        observed.update(kwargs)
        return run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_review', review)

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--reviewer-agent',
            'claude-code',
            '--reviewer-model',
            'sonnet',
        ]
    )

    assert result == 0
    assert observed['reviewer_command'] == [
        sys.executable,
        '-m',
        'agent_orchestra.adapter.claude_code',
        '--model',
        'sonnet',
    ]


def test_run_records_requested_changes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Advance a rejected review to the remediation boundary."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'changes_requested')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 0
    assert RunStore(database).get(run.id).state.value == 'changes_requested'


@pytest.mark.parametrize(
    ('developer_runtime', 'reviewer_runtime'),
    [
        ('codex', 'codex'),
        ('codex', 'claude-code'),
        ('claude-code', 'codex'),
        ('claude-code', 'claude-code'),
    ],
)
def test_worker_remediates_and_reviews_new_digest(
    tmp_path: Path, developer_runtime: str, reviewer_runtime: str
) -> None:
    """Complete the same canonical loop for every runtime combination."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    digest = _working_tree_digest(repo, 'HEAD')
    assert digest is not None
    run = Run.create_local(repo, repo, 'HEAD', 'HEAD', digest)
    store.add(run)
    reviewer = tmp_path / f'{reviewer_runtime}-reviewer.py'
    developer = tmp_path / f'{developer_runtime}-developer.py'
    write_loop_reviewer(reviewer)
    write_developer(developer)
    runs_directory = tmp_path / 'runs'

    result = run_queued_review(
        store=store,
        run=run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=runs_directory,
        timeout_seconds=30,
        max_iterations=2,
        digest_worktree=_working_tree_digest,
    )

    assert result.state.value == 'awaiting_commit_authorization'
    assert result.iteration == 2
    assert result.diff_digest != digest
    messages = runs_directory / run.id / 'messages'
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-review-request.json',
        '000006-review-result.json',
    ]
    second_request = json.loads((messages / '000005-review-request.json').read_text())
    assert second_request['iteration'] == 2
    assert second_request['scope']['diff_digest'] == result.diff_digest


@pytest.mark.parametrize(
    ('max_iterations', 'change_worktree', 'expected'),
    [(1, True, ITERATION_LIMIT), (2, False, NO_REMEDIATION_CHANGE)],
)
def test_worker_stops_bounded_non_progress(
    tmp_path: Path,
    max_iterations: int,
    change_worktree: bool,
    expected: str,
) -> None:
    """Fail durably on iteration exhaustion or a no-change handoff."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    digest = _working_tree_digest(repo, 'HEAD')
    assert digest is not None
    run = Run.create_local(repo, repo, 'HEAD', 'HEAD', digest)
    store.add(run)
    reviewer = tmp_path / 'loop-reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_developer(developer, change_worktree=change_worktree)

    with pytest.raises(WorkerError, match=expected):
        run_queued_review(
            store=store,
            run=run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            runs_directory=tmp_path / 'runs',
            timeout_seconds=30,
            max_iterations=max_iterations,
            digest_worktree=_working_tree_digest,
        )

    assert store.get(run.id).state.value == 'failed'
    failure = json.loads((tmp_path / 'runs' / run.id / 'failure.json').read_text())
    assert failure['run_id'] == run.id
    assert failure['state'] == 'failed'
    assert failure['error'] == {'code': 'worker_error', 'message': expected}


def test_worker_surfaces_developer_disagreement_for_human_decision(
    tmp_path: Path,
) -> None:
    """Preserve a justified no-change disagreement without failing the run."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    digest = _working_tree_digest(repo, 'HEAD')
    assert digest is not None
    run = Run.create_local(repo, repo, 'HEAD', 'HEAD', digest)
    store.add(run)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_developer(developer, change_worktree=False, disposition='rejected')
    runs_directory = tmp_path / 'runs'

    result = run_queued_review(
        store=store,
        run=run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=runs_directory,
        timeout_seconds=30,
        max_iterations=2,
        digest_worktree=_working_tree_digest,
    )

    assert result.state.value == 'changes_requested'
    evidence = json.loads(
        (runs_directory / run.id / 'decision-required.json').read_text()
    )
    assert evidence['reason']['code'] == 'developer_disagreement'
    assert 'disputed every finding' in evidence['reason']['message']
    assert not (runs_directory / run.id / 'failure.json').exists()


def test_run_keeps_blocked_review_awaiting_resolution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist a blocked review without inventing a terminal state."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'blocked')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 0
    assert RunStore(database).get(run.id).state.value == 'reviewing'


@pytest.mark.parametrize(
    'reviewer_command', [['/missing/reviewer'], ['/usr/bin/false']]
)
def test_run_marks_reviewer_execution_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    reviewer_command: list[str],
) -> None:
    """Persist failed state for missing and nonzero reviewer commands."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            *reviewer_command,
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'failed'


def test_run_marks_reviewer_timeout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persist failed state when the bounded reviewer exceeds its timeout."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'slow.py'
    reviewer.write_text('"""Slow test reviewer."""\nimport time\ntime.sleep(5)\n')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--timeout',
            '1',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'failed'
    invocation = json.loads(
        (tmp_path / f'runs/{run.id}/invocations/000001-reviewer.json').read_text()
    )
    assert invocation['exit_code'] is None
    assert invocation['timed_out'] is True


@pytest.mark.parametrize(
    'case',
    [
        ('approved', '30', 0, False),
        ('nonzero', '30', 2, False),
        ('timeout', '1', 2, True),
    ],
)
def test_builtin_run_exposes_child_output_through_logs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, str, int, bool],
) -> None:
    """Retain built-in child streams across success, error, and timeout."""

    mode, timeout, expected_result, timed_out = case
    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    runs_directory = tmp_path / 'runs'
    fake_codex = tmp_path / 'bin/codex'
    write_fake_codex(fake_codex, mode=mode)
    current_path = os.environ.get('PATH', '')
    monkeypatch.setenv('PATH', f'{fake_codex.parent}{os.pathsep}{current_path}')
    codex_home = tmp_path / 'codex-home'
    skill = codex_home / 'skills/agent-orchestra-reviewer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('review instructions\n')
    monkeypatch.setenv('CODEX_HOME', str(codex_home))
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--reviewer-model',
            'test-model',
            '--timeout',
            timeout,
            '--runs-directory',
            str(runs_directory),
        ]
    )
    capsys.readouterr()

    assert result == expected_result
    assert (
        main(
            [
                '--database',
                str(database),
                'logs',
                str(run.id),
                '--runs-directory',
                str(runs_directory),
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    streams = {entry['stream']: entry for entry in document['streams']}
    assert 'child stdout\n' in streams['stdout']['content']
    assert 'child stderr\n' in streams['stderr']['content']
    assert streams['stdout']['agent_model'] == 'test-model'
    assert streams['stdout']['timed_out'] is timed_out


def test_run_rejects_state_database_inside_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep mutable orchestration state outside the reviewed worktree."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = repo / '.agent-orchestra/state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'queued'
    assert 'state database must be outside' in capsys.readouterr().err


def test_run_rejects_evidence_directory_inside_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep mutable review messages and artifacts outside the reviewed worktree."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(repo / 'runs'),
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'queued'
    assert 'evidence directory must be outside' in capsys.readouterr().err


def test_run_handles_digest_failure_before_transition(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a disappeared repo without a traceback or state mutation."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    shutil.rmtree(repo / '.git')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--',
            '/usr/bin/true',
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'queued'
    assert 'cannot compute worktree digest' in capsys.readouterr().err


def test_run_marks_post_review_digest_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail durably when Git state disappears during reviewer execution."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    run = RunStore(database).list_runs()[0]
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    with reviewer.open('a') as file:
        file.write('\nimport shutil\nshutil.rmtree(Path.cwd() / ".git")\n')

    result = main(
        [
            '--database',
            str(database),
            'run',
            str(run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(tmp_path / 'runs'),
            '--',
            sys.executable,
            str(reviewer),
        ]
    )

    assert result == 2
    assert RunStore(database).get(run.id).state.value == 'failed'


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
