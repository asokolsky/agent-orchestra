"""Tests for command-line operations."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, fields
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import pytest

from agent_orchestra import cli, worker
from agent_orchestra.agents import AgentRequest, AgentResult, CommandAgentAdapter
from agent_orchestra.cli import (
    DEFAULT_DATABASE,
    DEFAULT_RUNS_DIRECTORY,
    _working_tree_digest,
    build_parser,
    main,
)
from agent_orchestra.invocations import InvocationIdentity
from agent_orchestra.models import Run, RunState
from agent_orchestra.store import RunStore
from agent_orchestra.worker import (
    ITERATION_LIMIT,
    NO_REMEDIATION_CHANGE,
    WorkerError,
    resume_review,
    run_queued_review,
)


@dataclass(frozen=True, slots=True)
class CliRunContext:
    """Paths and persisted state shared by CLI run tests."""

    repo: Path
    database: Path
    store: RunStore
    run: Run
    runs_directory: Path


@pytest.fixture
def enqueued_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> CliRunContext:
    """Create and enqueue one changed worktree for a CLI test."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    assert main(['--database', str(database), 'enqueue-local', str(repo)]) == 0
    capsys.readouterr()
    store = RunStore(database)
    return CliRunContext(
        repo=repo,
        database=database,
        store=store,
        run=store.list_runs()[0],
        runs_directory=tmp_path / 'runs',
    )


def run_arguments(
    context: CliRunContext,
    *options: str,
    reviewer: Path | None = None,
    objective: str = 'Review the change.',
) -> list[str]:
    """Build arguments for one run command against a shared test context."""

    arguments = [
        '--database',
        str(context.database),
        'run',
        str(context.run.id),
        '--objective',
        objective,
        '--runs-directory',
        str(context.runs_directory),
        *options,
    ]
    if reviewer is not None:
        arguments.extend(['--', sys.executable, str(reviewer)])
    return arguments


def resume_arguments(context: CliRunContext) -> list[str]:
    """Build arguments for resuming the context's run."""

    return [
        '--database',
        str(context.database),
        'resume',
        str(context.run.id),
        '--runs-directory',
        str(context.runs_directory),
    ]


def create_worker_run(tmp_path: Path) -> CliRunContext:
    """Create one persisted changed run for direct worker tests."""

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
    return CliRunContext(
        repo=repo,
        database=database,
        store=store,
        run=run,
        runs_directory=tmp_path / 'runs',
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


def write_reviewer(path: Path, verdict: str, *, write_artifact: bool = True) -> None:
    """Write a deterministic reviewer command for CLI integration tests."""

    artifact_write = 'artifact_path.write_text("# Review\\n")' if write_artifact else ''
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
{artifact_write}
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


def write_recoverable_developer(path: Path) -> None:
    """Write a developer that blocks once and succeeds when resumed."""

    write_developer(path)
    content = path.read_text()
    content = content.replace(
        '"status": "ready_for_review",',
        '"status": "blocked" if request["sequence"] == 3 else "ready_for_review",',
    )
    path.write_text(content)


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
    predecessor = RunStore(database).list_runs()[0]
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
    replacement = RunStore(database).list_runs()[0]
    assert replacement.id != predecessor.id
    assert replacement.supersedes_run_id == predecessor.id
    assert str(replacement.id) in capsys.readouterr().out


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
        'schema_version': 5,
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
    assert output.startswith(
        '{\n  "schema_version": 5,\n'
        f'  "runs_directory": "{DEFAULT_RUNS_DIRECTORY.expanduser().resolve()}",\n'
        '  "runs": [\n    {\n'
    )
    assert output.endswith('\n}\n')
    document = json.loads(output)
    expected_fields = {
        'repository_path' if field.name == 'repo_path' else field.name
        for field in fields(Run)
    }
    assert set(document['runs'][0]) == expected_fields
    assert document == {
        'schema_version': 5,
        'runs_directory': str(DEFAULT_RUNS_DIRECTORY.expanduser().resolve()),
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
                'supersedes_run_id': None,
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
    assert document['schema_version'] == 5
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
    assert document['schema_version'] == 5
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
        'schema_version': 5,
        'runs_directory': str(DEFAULT_RUNS_DIRECTORY.expanduser().resolve()),
        'runs': [],
    }


def test_status_reports_resolved_default_runs_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report the canonical evidence root when its configured ancestor is a symlink."""

    database = tmp_path / 'state.db'
    RunStore(database).initialize()
    actual_parent = tmp_path / 'actual'
    actual_parent.mkdir()
    linked_parent = tmp_path / 'linked'
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    monkeypatch.setattr(cli, 'DEFAULT_RUNS_DIRECTORY', linked_parent / 'runs')

    result = main(['--database', str(database), 'status'])

    assert result == 0
    assert json.loads(capsys.readouterr().out)['runs_directory'] == str(
        (actual_parent / 'runs').resolve()
    )


def test_run_and_logs_share_default_runs_directory() -> None:
    """Keep both evidence consumers aligned with the status contract."""

    parser = build_parser()

    run_args = parser.parse_args(['run', 'run-id', '--objective', 'Review.'])
    logs_args = parser.parse_args(['logs', 'run-id'])

    assert run_args.runs_directory == DEFAULT_RUNS_DIRECTORY
    assert logs_args.runs_directory == DEFAULT_RUNS_DIRECTORY


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
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Persist a correlated approved review and stop at the commit gate."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')

    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 0
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    run_directory = enqueued_run.runs_directory / str(enqueued_run.run.id)
    assert (run_directory / 'messages/000001-review-request.json').is_file()
    assert (run_directory / 'messages/000002-review-result.json').is_file()
    assert (run_directory / 'artifacts/review-0001.md').is_file()
    execution = json.loads((run_directory / 'execution.json').read_text())
    assert execution == {
        'schema_version': 2,
        'run_id': str(enqueued_run.run.id),
        'objective': 'Review the change.',
        'reviewer': {
            'command': [sys.executable, str(reviewer)],
            'identity': {
                'vendor': 'unknown',
                'model': None,
                'runtime': 'custom-command',
            },
            'timeout_seconds': 1800,
        },
        'developer': {
            'command': [],
            'identity': {
                'vendor': 'openai',
                'model': None,
                'runtime': 'codex',
            },
            'timeout_seconds': 1800,
        },
        'max_review_iterations': 3,
        'created_at': execution['created_at'],
    }
    invocation = json.loads(
        (run_directory / 'invocations/000001-reviewer.json').read_text()
    )
    assert invocation['run_id'] == str(enqueued_run.run.id)
    assert invocation['role'] == 'reviewer'
    assert invocation['agent_vendor'] == 'unknown'
    assert invocation['runtime'] == 'custom-command'
    assert invocation['exit_code'] == 0
    assert invocation['timed_out'] is False
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 5,
        'run_id': str(enqueued_run.run.id),
        'state': 'awaiting_commit_authorization',
        'error': None,
    }


def test_run_preserves_non_utf8_reviewer_output(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Archive the exact bytes written by a redirected reviewer process."""

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
    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 0
    stdout_log = (
        enqueued_run.runs_directory
        / str(enqueued_run.run.id)
        / 'logs/000001-reviewer.stdout.log'
    )
    assert stdout_log.read_bytes() == b'caf\xe9 latin-1 byte\n'


@pytest.mark.parametrize('interrupted_role', ['reviewer', 'developer'])
def test_worker_persists_interrupted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_role: str,
) -> None:
    """Keep durable run state aligned with interrupted invocation evidence."""

    context = create_worker_run(tmp_path)
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
    with pytest.raises(KeyboardInterrupt):
        run_queued_review(
            store=context.store,
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=('unused-developer',),
            runs_directory=context.runs_directory,
            timeout_seconds=30,
            digest_worktree=_working_tree_digest,
        )

    assert context.store.get(context.run.id).state is RunState.INTERRUPTED
    invocation_files = sorted(
        (context.runs_directory / str(context.run.id) / 'invocations').glob('*.json')
    )
    invocation = json.loads(invocation_files[-1].read_text())
    assert invocation['role'] == interrupted_role
    assert invocation['interrupted'] is True


@pytest.mark.parametrize(
    'case',
    [
        ((), 'agent_orchestra.adapter.codex', 'openai', None, 'codex'),
        (
            ('--reviewer-model', 'compatible-model'),
            'agent_orchestra.adapter.codex',
            'openai',
            'compatible-model',
            'codex',
        ),
        (
            ('--reviewer-agent', 'claude-code', '--reviewer-model', 'sonnet'),
            'agent_orchestra.adapter.claude_code',
            'anthropic',
            'sonnet',
            'claude-code',
        ),
    ],
)
def test_run_selects_reviewer_adapter(
    enqueued_run: CliRunContext,
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[tuple[str, ...], str, str, str | None, str],
) -> None:
    """Build the requested reviewer command and identity independently."""

    options, module, vendor, model, runtime = case
    observed: dict[str, object] = {}

    def review(**kwargs: object) -> Run:
        """Capture the selected command without starting an agent."""

        observed.update(kwargs)
        return enqueued_run.run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_review', review)

    assert main(run_arguments(enqueued_run, *options)) == 0

    expected_command = [sys.executable, '-m', module]
    if model is not None:
        expected_command.extend(['--model', model])
    assert observed['reviewer_command'] == expected_command
    identity = observed['reviewer_identity']
    assert isinstance(identity, InvocationIdentity)
    assert identity == InvocationIdentity(vendor=vendor, model=model, runtime=runtime)


def test_default_database_is_outside_a_repo_in_the_home_directory() -> None:
    """Keep default orchestration state outside a typical reviewed repo."""

    repo = Path.home() / 'Projects/repo'

    assert Path.home() / '.local/state/agent-orchestra/state.db' == DEFAULT_DATABASE
    assert not DEFAULT_DATABASE.is_relative_to(repo)


def test_run_records_requested_changes(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Advance a rejected review to the remediation boundary."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'changes_requested')

    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 0
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state is RunState.CHANGES_REQUESTED
    )


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

    context = create_worker_run(tmp_path)
    digest = context.run.diff_digest
    assert digest is not None
    reviewer = tmp_path / f'{reviewer_runtime}-reviewer.py'
    developer = tmp_path / f'{developer_runtime}-developer.py'
    write_loop_reviewer(reviewer)
    write_developer(developer)

    result = run_queued_review(
        store=context.store,
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=context.runs_directory,
        timeout_seconds=30,
        max_iterations=2,
        digest_worktree=_working_tree_digest,
    )

    assert result.state.value == 'awaiting_commit_authorization'
    assert result.iteration == 2
    assert result.diff_digest != digest
    messages = context.runs_directory / context.run.id / 'messages'
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


def test_resume_validation_required_continues_same_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Re-run blocked validation and approve the next digest under one run ID."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)

    blocked = run_queued_review(
        store=context.store,
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=context.runs_directory,
        timeout_seconds=30,
        max_iterations=3,
        digest_worktree=_working_tree_digest,
    )

    assert blocked.id == context.run.id
    assert blocked.state is RunState.VALIDATION_REQUIRED
    messages = context.runs_directory / context.run.id / 'messages'
    assert (messages / '000004-developer-handoff.json').is_file()
    assert not (
        context.runs_directory
        / context.run.id
        / 'logs/000004-rejected-developer-handoff.json'
    ).exists()

    result = main(resume_arguments(context))

    assert result == 0
    resumed = context.store.get(context.run.id)
    assert resumed.id == context.run.id
    assert resumed.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    assert resumed.iteration == 2
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-remediation-request.json',
        '000006-developer-handoff.json',
        '000007-review-request.json',
        '000008-review-result.json',
    ]
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 5,
        'run_id': str(context.run.id),
        'state': 'awaiting_commit_authorization',
        'error': None,
    }

    assert main(resume_arguments(context)) == 2
    repeated = json.loads(capsys.readouterr().out)
    assert repeated['error']['code'] == 'run_not_resumable'
    assert len(tuple(messages.iterdir())) == 8


def test_resume_retries_an_interrupted_validation_required_recovery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retry an interrupted recovery developer request under the same run."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    blocked = run_queued_review(
        store=context.store,
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=context.runs_directory,
        timeout_seconds=30,
        developer_timeout_seconds=1,
        max_iterations=3,
        digest_worktree=_working_tree_digest,
    )

    assert blocked.state is RunState.VALIDATION_REQUIRED
    developer.write_text('"""Slow developer."""\nimport time\ntime.sleep(5)\n')

    assert main(resume_arguments(context)) == 2
    interrupted = json.loads(capsys.readouterr().out)
    assert interrupted['error']['code'] == 'resume_interrupted'
    assert context.store.get(context.run.id).state is RunState.INTERRUPTED

    write_developer(developer)

    assert main(resume_arguments(context)) == 0
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    messages = context.runs_directory / context.run.id / 'messages'
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-remediation-request.json',
        '000006-developer-handoff.json',
        '000007-review-request.json',
        '000008-review-result.json',
    ]
    invocations = context.runs_directory / context.run.id / 'invocations'
    retry = json.loads((invocations / '000005-developer-attempt-0002.json').read_text())
    assert retry['attempt'] == 2
    assert retry['timed_out'] is False


def test_resume_archives_rejected_developer_handoff_by_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retain an attempt-qualified rejected handoff during validation recovery."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    blocked = run_queued_review(
        store=context.store,
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=context.runs_directory,
        timeout_seconds=30,
        max_iterations=3,
        digest_worktree=_working_tree_digest,
    )
    assert blocked.state is RunState.VALIDATION_REQUIRED
    write_developer(developer)
    developer.write_text(
        developer.read_text().replace(
            '"status": "ready_for_review",', '"status": "invalid",'
        )
    )

    assert main(resume_arguments(context)) == 2

    assert json.loads(capsys.readouterr().out)['error']['code'] == (
        'resume_evidence_invalid'
    )
    rejected = (
        context.runs_directory
        / context.run.id
        / 'logs/000006-rejected-developer-handoff-attempt-0001.json'
    )
    assert rejected.is_file()


def test_resume_rejects_handoff_with_a_non_remediation_parent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail closed when a recoverable handoff replies to the wrong message type."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    run_queued_review(
        store=context.store,
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=context.runs_directory,
        timeout_seconds=30,
        max_iterations=3,
        digest_worktree=_working_tree_digest,
    )

    messages = context.runs_directory / context.run.id / 'messages'
    review_result = json.loads((messages / '000002-review-result.json').read_text())
    handoff_path = messages / '000004-developer-handoff.json'
    handoff = json.loads(handoff_path.read_text())
    handoff['in_reply_to'] = review_result['message_id']
    handoff_path.write_text(json.dumps(handoff))

    assert main(resume_arguments(context)) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED


def test_resume_rejects_handoff_linked_to_a_different_review_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject a valid-looking chain whose blocked handoff uses stale review evidence."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    run_queued_review(
        store=context.store,
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=context.runs_directory,
        timeout_seconds=30,
        max_iterations=3,
        digest_worktree=_working_tree_digest,
    )

    messages = context.runs_directory / context.run.id / 'messages'
    first_request = json.loads((messages / '000001-review-request.json').read_text())
    first_result = json.loads((messages / '000002-review-result.json').read_text())
    remediation = json.loads((messages / '000003-remediation-request.json').read_text())
    handoff = json.loads((messages / '000004-developer-handoff.json').read_text())

    extra_request = dict(first_request)
    extra_request.update(
        {'message_id': str(uuid4()), 'sequence': 5, 'in_reply_to': None}
    )
    extra_result = dict(first_result)
    extra_result.update(
        {
            'message_id': str(uuid4()),
            'in_reply_to': extra_request['message_id'],
            'sequence': 6,
        }
    )
    remediation.update({'message_id': str(uuid4()), 'sequence': 7})
    handoff.update(
        {
            'message_id': str(uuid4()),
            'in_reply_to': remediation['message_id'],
            'sequence': 8,
        }
    )

    (messages / '000005-review-request.json').write_text(json.dumps(extra_request))
    (messages / '000006-review-result.json').write_text(json.dumps(extra_result))
    (messages / '000007-remediation-request.json').write_text(json.dumps(remediation))
    (messages / '000008-developer-handoff.json').write_text(json.dumps(handoff))

    assert main(resume_arguments(context)) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED


@pytest.mark.parametrize(
    'tamper',
    [
        'initial-prior-review-path',
        'repeat-prior-review-path',
        'result-artifact-path',
        'missing-result-artifact',
    ],
)
def test_resume_rejects_tampered_review_exchange_payload_links(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], tamper: str
) -> None:
    """Fail closed when review evidence links do not describe the chain."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    reviewer.write_text(
        reviewer.read_text().replace(
            'artifact_path = Path(request["payload"]["artifact_path"])',
            'artifact_path = Path(request["payload"]["artifact_path"])\n'
            'if request["iteration"] == 2:\n'
            '    import time\n'
            '    time.sleep(5)',
        )
    )
    write_developer(developer)

    with pytest.raises(WorkerError, match='reviewer timed out'):
        run_queued_review(
            store=context.store,
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            runs_directory=context.runs_directory,
            timeout_seconds=1,
            max_iterations=3,
            digest_worktree=_working_tree_digest,
        )

    messages = context.runs_directory / context.run.id / 'messages'
    first_request_path = messages / '000001-review-request.json'
    first_result_path = messages / '000002-review-result.json'
    repeat_request_path = messages / '000005-review-request.json'
    first_request = json.loads(first_request_path.read_text())
    first_result = json.loads(first_result_path.read_text())
    repeat_request = json.loads(repeat_request_path.read_text())
    if tamper == 'initial-prior-review-path':
        first_request['payload']['prior_review_path'] = str(first_result_path)
        first_request_path.write_text(json.dumps(first_request))
    elif tamper == 'repeat-prior-review-path':
        repeat_request['payload']['prior_review_path'] = str(first_request_path)
        repeat_request_path.write_text(json.dumps(repeat_request))
    elif tamper == 'result-artifact-path':
        first_result['payload']['artifact_path'] = str(first_request_path)
        first_result_path.write_text(json.dumps(first_result))
    else:
        Path(first_result['payload']['artifact_path']).unlink()

    assert main(resume_arguments(context)) == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.INTERRUPTED


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

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'loop-reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_developer(developer, change_worktree=change_worktree)

    with pytest.raises(WorkerError, match=expected):
        run_queued_review(
            store=context.store,
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            runs_directory=context.runs_directory,
            timeout_seconds=30,
            max_iterations=max_iterations,
            digest_worktree=_working_tree_digest,
        )

    assert context.store.get(context.run.id).state is RunState.FAILED
    failure = json.loads(
        (context.runs_directory / context.run.id / 'failure.json').read_text()
    )
    assert failure['run_id'] == context.run.id
    assert failure['state'] == 'failed'
    assert failure['error'] == {'code': 'worker_error', 'message': expected}


def test_worker_surfaces_developer_disagreement_for_human_decision(
    tmp_path: Path,
) -> None:
    """Preserve a justified no-change disagreement without failing the run."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_developer(developer, change_worktree=False, disposition='rejected')

    result = run_queued_review(
        store=context.store,
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=context.runs_directory,
        timeout_seconds=30,
        max_iterations=2,
        digest_worktree=_working_tree_digest,
    )

    assert result.state.value == 'changes_requested'
    evidence = json.loads(
        (context.runs_directory / context.run.id / 'decision-required.json').read_text()
    )
    assert evidence['reason']['code'] == 'developer_disagreement'
    assert 'disputed every finding' in evidence['reason']['message']
    assert not (context.runs_directory / context.run.id / 'failure.json').exists()


def test_run_keeps_blocked_review_awaiting_resolution(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Persist a blocked review without inventing a terminal state."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'blocked')

    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 0
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.REVIEWING


@pytest.mark.parametrize(
    'reviewer_command', [['/missing/reviewer'], ['/usr/bin/false']]
)
def test_run_marks_reviewer_execution_failure(
    enqueued_run: CliRunContext,
    reviewer_command: list[str],
) -> None:
    """Persist failed state for missing and nonzero reviewer commands."""

    result = main(
        [
            '--database',
            str(enqueued_run.database),
            'run',
            str(enqueued_run.run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(enqueued_run.runs_directory),
            '--',
            *reviewer_command,
        ]
    )

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.FAILED
    failure = json.loads(
        (enqueued_run.runs_directory / enqueued_run.run.id / 'failure.json').read_text()
    )
    assert failure['error']['code'] == 'resume_execution_failed'


def test_run_marks_reviewer_timeout(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Persist interrupted state when the bounded reviewer exceeds its timeout."""

    reviewer = tmp_path / 'slow.py'
    reviewer.write_text('"""Slow test reviewer."""\nimport time\ntime.sleep(5)\n')

    result = main(run_arguments(enqueued_run, '--timeout', '1', reviewer=reviewer))

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    invocation = json.loads(
        (
            enqueued_run.runs_directory
            / enqueued_run.run.id
            / 'invocations/000001-reviewer.json'
        ).read_text()
    )
    assert invocation['exit_code'] is None
    assert invocation['timed_out'] is True


def test_resume_interrupted_reviewer_reuses_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Retry an interrupted reviewer without duplicating its canonical request."""

    reviewer = tmp_path / 'reviewer.py'
    reviewer.write_text('"""Slow reviewer."""\nimport time\ntime.sleep(5)\n')

    assert (
        main(
            run_arguments(
                enqueued_run,
                '--timeout',
                '1',
                reviewer=reviewer,
                objective='Review.',
            )
        )
        == 2
    )
    capsys.readouterr()
    invocation_path = (
        enqueued_run.runs_directory
        / enqueued_run.run.id
        / 'invocations/000001-reviewer.json'
    )
    invocation = invocation_path.read_text()
    invocation_path.write_text('{')
    assert main(resume_arguments(enqueued_run)) == 2
    invalid_invocation = json.loads(capsys.readouterr().out)
    assert invalid_invocation['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    invocation_path.write_text(invocation)
    (enqueued_run.repo / 'tracked.txt').write_text('changed again\n')
    assert main(resume_arguments(enqueued_run)) == 2
    changed_scope = json.loads(capsys.readouterr().out)
    assert changed_scope['error']['code'] == 'resume_scope_changed'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    (enqueued_run.repo / 'tracked.txt').write_text('changed\n')
    messages = enqueued_run.runs_directory / enqueued_run.run.id / 'messages'
    request_path = messages / '000001-review-request.json'
    gapped_path = messages / '000003-review-request.json'
    request_path.rename(gapped_path)
    assert main(resume_arguments(enqueued_run)) == 2
    invalid_chain = json.loads(capsys.readouterr().out)
    assert invalid_chain['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    gapped_path.rename(request_path)
    execution_path = (
        enqueued_run.runs_directory / enqueued_run.run.id / 'execution.json'
    )
    execution = json.loads(execution_path.read_text())
    execution['run_id'] = '20260904T000000Z-00000000'
    execution_path.write_text(json.dumps(execution))

    assert main(resume_arguments(enqueued_run)) == 2
    mismatched = json.loads(capsys.readouterr().out)
    assert mismatched['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    execution['run_id'] = enqueued_run.run.id
    execution['schema_version'] = 1
    execution_path.write_text(json.dumps(execution))

    assert main(resume_arguments(enqueued_run)) == 2
    unsupported = json.loads(capsys.readouterr().out)
    assert unsupported['error']['code'] == 'resume_metadata_unsupported'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    execution['schema_version'] = 2
    execution_path.write_text(json.dumps(execution))
    write_reviewer(reviewer, 'approved')

    assert main(resume_arguments(enqueued_run)) == 0

    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
    ]
    invocations = enqueued_run.runs_directory / enqueued_run.run.id / 'invocations'
    assert sorted(path.name for path in invocations.iterdir()) == [
        '000001-reviewer-attempt-0002.json',
        '000001-reviewer.json',
    ]
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_reports_explicit_execution_failure_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Classify a failed retry without matching its human-readable message."""

    reviewer = tmp_path / 'reviewer.py'
    reviewer.write_text('"""Slow reviewer."""\nimport time\ntime.sleep(5)\n')
    assert main(run_arguments(enqueued_run, '--timeout', '1', reviewer=reviewer)) == 2
    capsys.readouterr()
    reviewer.unlink()

    assert main(resume_arguments(enqueued_run)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_execution_failed'
    failure = json.loads(
        (enqueued_run.runs_directory / enqueued_run.run.id / 'failure.json').read_text()
    )
    assert failure['error']['code'] == document['error']['code']


def test_resume_rejects_stale_artifact_from_interrupted_reviewer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Require a retried reviewer to create a fresh human artifact."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    reviewer.write_text(
        reviewer.read_text().replace(
            'artifact_path.write_text("# Review\\n")',
            'artifact_path.write_text("# Stale review\\n")\nimport time\ntime.sleep(5)',
        )
    )

    assert main(run_arguments(enqueued_run, '--timeout', '1', reviewer=reviewer)) == 2
    capsys.readouterr()
    run_directory = enqueued_run.runs_directory / enqueued_run.run.id
    artifact_path = run_directory / 'artifacts/review-0001.md'
    archived_path = (
        run_directory / 'logs/000002-rejected-review-artifact-attempt-0001.md'
    )
    assert not artifact_path.exists()
    assert archived_path.read_text() == '# Stale review\n'

    write_reviewer(reviewer, 'approved', write_artifact=False)
    assert main(resume_arguments(enqueued_run)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.FAILED


def test_resume_interrupted_developer_reuses_remediation_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retry an interrupted developer without starting a replacement run."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    developer.write_text('"""Slow developer."""\nimport time\ntime.sleep(5)\n')

    with pytest.raises(WorkerError, match='developer timed out'):
        run_queued_review(
            store=context.store,
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            runs_directory=context.runs_directory,
            timeout_seconds=30,
            developer_timeout_seconds=1,
            max_iterations=3,
            digest_worktree=_working_tree_digest,
        )

    assert context.store.get(context.run.id).state is RunState.INTERRUPTED
    invocation_path = (
        context.runs_directory / context.run.id / 'invocations/000003-developer.json'
    )
    invocation = invocation_path.read_text()
    invocation_path.write_text('{')
    assert main(resume_arguments(context)) == 2
    invalid_invocation = json.loads(capsys.readouterr().out)
    assert invalid_invocation['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.INTERRUPTED
    invocation_path.write_text(invocation)
    write_developer(developer)
    assert main(resume_arguments(context)) == 0
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    messages = context.runs_directory / context.run.id / 'messages'
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-review-request.json',
        '000006-review-result.json',
    ]
    invocations = context.runs_directory / context.run.id / 'invocations'
    assert (invocations / '000003-developer.json').is_file()
    retry = json.loads((invocations / '000003-developer-attempt-0002.json').read_text())
    assert retry['attempt'] == 2
    assert retry['timed_out'] is False


def test_resume_writes_recovery_request_before_activating_developer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep validation recoverable when its next request cannot be persisted."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    blocked = run_queued_review(
        store=context.store,
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=context.runs_directory,
        timeout_seconds=30,
        max_iterations=3,
        digest_worktree=_working_tree_digest,
    )
    assert blocked.state is RunState.VALIDATION_REQUIRED
    original_write = worker._write_json_atomic

    def fail_recovery_request(path: Path, document: dict[str, object]) -> None:
        """Simulate failure to persist only the recovery request."""

        if path.name == '000005-remediation-request.json':
            message = 'simulated write failure'
            raise OSError(message)
        original_write(path, document)

    monkeypatch.setattr(worker, '_write_json_atomic', fail_recovery_request)
    assert main(resume_arguments(context)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED
    messages = context.runs_directory / context.run.id / 'messages'
    assert not (messages / '000005-remediation-request.json').exists()


def test_resume_reuses_recovery_request_after_activation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse a durable request when activation fails before process launch."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    blocked = run_queued_review(
        store=context.store,
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        runs_directory=context.runs_directory,
        timeout_seconds=30,
        max_iterations=3,
        digest_worktree=_working_tree_digest,
    )
    assert blocked.state is RunState.VALIDATION_REQUIRED
    original_update = context.store.update

    def fail_activation(run: Run, *, expected_state: RunState) -> None:
        """Simulate failure while activating the recovery developer."""

        if (
            run.state is RunState.DEVELOPING
            and expected_state is RunState.VALIDATION_REQUIRED
        ):
            message = 'simulated activation failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(context.store, 'update', fail_activation)
    with pytest.raises(OSError, match='simulated activation failure'):
        resume_review(
            store=context.store,
            run=blocked,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        )
    messages = context.runs_directory / context.run.id / 'messages'
    assert (messages / '000005-remediation-request.json').is_file()
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED

    monkeypatch.setattr(context.store, 'update', original_update)
    assert main(resume_arguments(context)) == 0
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


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
    enqueued_run: CliRunContext,
    case: tuple[str, str, int, bool],
) -> None:
    """Retain built-in child streams across success, error, and timeout."""

    mode, timeout, expected_result, timed_out = case
    fake_codex = tmp_path / 'bin/codex'
    write_fake_codex(fake_codex, mode=mode)
    current_path = os.environ.get('PATH', '')
    monkeypatch.setenv('PATH', f'{fake_codex.parent}{os.pathsep}{current_path}')
    codex_home = tmp_path / 'codex-home'
    skill = codex_home / 'skills/agent-orchestra-reviewer'
    skill.mkdir(parents=True)
    (skill / 'SKILL.md').write_text('review instructions\n')
    monkeypatch.setenv('CODEX_HOME', str(codex_home))
    result = main(
        run_arguments(
            enqueued_run,
            '--reviewer-model',
            'test-model',
            '--timeout',
            timeout,
        )
    )
    capsys.readouterr()

    assert result == expected_result
    assert (
        main(
            [
                '--database',
                str(enqueued_run.database),
                'logs',
                str(enqueued_run.run.id),
                '--runs-directory',
                str(enqueued_run.runs_directory),
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
    capsys: pytest.CaptureFixture[str], enqueued_run: CliRunContext
) -> None:
    """Keep mutable review messages and artifacts outside the reviewed worktree."""

    result = main(
        [
            '--database',
            str(enqueued_run.database),
            'run',
            str(enqueued_run.run.id),
            '--objective',
            'Review the change.',
            '--runs-directory',
            str(enqueued_run.repo / 'runs'),
        ]
    )

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.QUEUED
    assert 'evidence directory must be outside' in capsys.readouterr().err


def test_run_handles_digest_failure_before_transition(
    capsys: pytest.CaptureFixture[str], enqueued_run: CliRunContext
) -> None:
    """Report a disappeared repo without a traceback or state mutation."""

    shutil.rmtree(enqueued_run.repo / '.git')

    result = main(
        [
            '--database',
            str(enqueued_run.database),
            'run',
            str(enqueued_run.run.id),
            '--objective',
            'Review the change.',
            '--',
            '/usr/bin/true',
        ]
    )

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.QUEUED
    assert 'cannot compute worktree digest' in capsys.readouterr().err


def test_run_marks_post_review_digest_failure(
    tmp_path: Path, enqueued_run: CliRunContext
) -> None:
    """Fail durably when Git state disappears during reviewer execution."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    with reviewer.open('a') as file:
        file.write('\nimport shutil\nshutil.rmtree(Path.cwd() / ".git")\n')

    result = main(run_arguments(enqueued_run, reviewer=reviewer))

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.FAILED


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
