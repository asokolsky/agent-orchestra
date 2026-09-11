"""Tests for command-line operations."""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, fields, replace
from importlib.metadata import version
from pathlib import Path
from threading import Barrier, Lock, Thread
from typing import Any
from uuid import uuid4

import pytest

from agent_orchestra import (
    cli,
    developer_remediation,
    invocations,
    queued_review,
    worker,
)
from agent_orchestra import evidence as evidence_module
from agent_orchestra.adapter.registry import (
    RuntimeDefinition,
    RuntimeRegistry,
    RuntimeRole,
)
from agent_orchestra.agents import AgentRequest, AgentResult, CommandAgentAdapter
from agent_orchestra.audit import _canonical_evidence_type
from agent_orchestra.cli import (
    DEFAULT_DATABASE,
    _working_tree_digest,
    build_parser,
    main,
)
from agent_orchestra.evidence import (
    WorkerError,
    resolve_evidence_path,
)
from agent_orchestra.execution_context import (
    ITERATION_LIMIT,
    WorkerContext,
)
from agent_orchestra.invocations import (
    AttemptIdentity,
    InvocationEvidenceStore,
    InvocationIdentity,
    InvocationRecord,
)
from agent_orchestra.manifests import ENGINE_TOO_OLD, ManifestError
from agent_orchestra.messages import (
    NO_REMEDIATION_CHANGE,
)
from agent_orchestra.models import HUMAN_ACTION_STATES, IssueJob, Run, RunState
from agent_orchestra.queued_review import (
    run_queued_review,
)
from agent_orchestra.reviewer_plan import ReviewerExecutionPlan
from agent_orchestra.settings import load_settings
from agent_orchestra.store import JobStore
from agent_orchestra.worker import (
    resume_review,
)


@dataclass(frozen=True, slots=True)
class CliRunContext:
    """Paths and persisted state shared by CLI run tests."""

    repo: Path
    database: Path
    store: JobStore
    run: Run
    runs_directory: Path


def evidence_directory(context: CliRunContext) -> Path:
    """Return the canonical evidence directory for one CLI test job."""

    return resolve_evidence_path(context.runs_directory, str(context.run.id))


def test_cli_rejects_incompatible_manifest_with_stable_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail before argument handling when packaged knowledge is incompatible."""

    def reject_manifests() -> None:
        raise ManifestError(ENGINE_TOO_OLD, 'codex')

    monkeypatch.setattr(cli, 'validate_packaged_manifests', reject_manifests)
    assert main(['--help']) == 2
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': cli.CLI_SCHEMA_VERSION,
        'error': {
            'code': 'manifest_engine_too_old',
            'message': 'manifest_engine_too_old: codex',
        },
    }


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
    store = JobStore(database)
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


def create_worker_run(tmp_path: Path, *, job_id: str | None = None) -> CliRunContext:
    """Create one persisted changed run for direct worker tests."""

    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    (repo / 'tracked.txt').write_text('changed\n')
    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    digest = _working_tree_digest(repo, 'HEAD')
    assert digest is not None
    run = Run.create_local(repo, repo, 'HEAD', 'HEAD', digest)
    if job_id is not None:
        run = replace(run, id=job_id)
    store.add(run)
    return CliRunContext(
        repo=repo,
        database=database,
        store=store,
        run=run,
        runs_directory=tmp_path / 'runs',
    )


@pytest.mark.parametrize(
    ('identity', 'registry', 'expected_code'),
    [
        (
            InvocationIdentity(vendor='example', model=None, runtime='unknown'),
            RuntimeRegistry(
                (
                    RuntimeDefinition(
                        identifier='known',
                        vendor='example',
                        module='example',
                        reviewer_adapter='example.Reviewer',
                        developer_adapter=None,
                        issue_reviewer_adapter=None,
                        manifest_placeholders=frozenset(),
                        reports_runtime_metadata=False,
                        skill_home_environment='EXAMPLE_HOME',
                        skill_home_directory='.example',
                    ),
                )
            ),
            'runtime_unknown',
        ),
        (
            InvocationIdentity(vendor='example', model=None, runtime='known'),
            RuntimeRegistry(
                (
                    RuntimeDefinition(
                        identifier='known',
                        vendor='example',
                        module='example',
                        reviewer_adapter=None,
                        developer_adapter=None,
                        issue_reviewer_adapter=None,
                        manifest_placeholders=frozenset(),
                        reports_runtime_metadata=False,
                        skill_home_environment='EXAMPLE_HOME',
                        skill_home_directory='.example',
                    ),
                )
            ),
            'runtime_role_unsupported',
        ),
    ],
)
def test_fresh_worker_rejects_invalid_runtime_before_command(
    tmp_path: Path,
    identity: InvocationIdentity,
    registry: RuntimeRegistry,
    expected_code: str,
) -> None:
    """Validate programmatic runtime identities before changing durable state."""

    context = create_worker_run(tmp_path)
    marker = tmp_path / 'executed'

    with pytest.raises(WorkerError) as raised:
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
                registry=registry,
            ),
            run=context.run,
            objective='Review the change.',
            reviewer_command=(sys.executable, '-c', f'open({str(marker)!r}, "w")'),
            developer_command=(),
            timeout_seconds=30,
            reviewer_identity=identity,
        )

    assert raised.value.code == expected_code
    assert context.store.get(str(context.run.id)).state is RunState.QUEUED
    assert not marker.exists()


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


def write_provenance_reviewer(path: Path) -> None:
    """Write an approved reviewer that reports effective model provenance."""

    write_reviewer(path, 'approved')
    content = path.read_text()
    content = content.replace('import json\n', 'import json\nimport os\n', 1)
    content = content.replace(
        'request_path = Path(sys.argv[1])',
        """metadata_path = Path(os.environ["AGENT_ORCHESTRA_RUNTIME_METADATA_PATH"])
metadata_path.write_text(json.dumps({
    "schema_version": 2,
    "effective_models": ["claude-primary", "claude-fallback"],
    "status": "reported",
    "timed_out": False,
}))
request_path = Path(sys.argv[1])""",
        1,
    )
    path.write_text(content)


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


def add_execution_counter(path: Path, counter: Path) -> None:
    """Make a generated test agent count each process activation."""

    content = path.read_text()
    content = content.replace(
        'request_path = Path(sys.argv[1])',
        f"""counter_path = Path({str(counter)!r})
counter_path.write_text(counter_path.read_text() + "1\\n" if counter_path.exists() else "1\\n")
request_path = Path(sys.argv[1])""",
        1,
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
        'timeout': 'time.sleep(20)',
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
        'schema_version': 22,
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


def test_jobs_does_not_create_missing_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep job listing read-only when no state database exists."""

    database = tmp_path / 'missing' / 'state.db'

    result = main(['--database', str(database), 'jobs'])

    assert result == 2
    assert 'state database not found' in capsys.readouterr().out
    assert not database.exists()


def test_jobs_rejects_evidence_directory_option(tmp_path: Path) -> None:
    """Do not accept an option that job listing cannot use."""

    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(['jobs', '--runs-directory', str(tmp_path / 'evidence')])


def test_jobs_lists_persisted_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Display a persisted job without changing state."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)

    result = main(['--database', str(database), 'jobs'])

    assert result == 0
    output = capsys.readouterr().out
    assert output.startswith('{\n  "schema_version": 22,\n  "jobs": [\n    {\n')
    assert output.endswith('\n}\n')
    document = json.loads(output)
    expected_fields = {
        'job_id'
        if field.name == 'id'
        else 'repository_path'
        if field.name == 'repo_path'
        else 'supersedes_job_id'
        if field.name == 'supersedes_run_id'
        else field.name
        for field in fields(Run)
    }
    expected_fields.add('worktree_status')
    assert set(document['jobs'][0]) == expected_fields
    assert document == {
        'schema_version': 22,
        'jobs': [
            {
                'job_id': str(run.id),
                'scenario': 'local_changes',
                'repository_path': str(tmp_path),
                'worktree_path': str(tmp_path),
                'worktree_status': 'not_git_worktree',
                'state': 'queued',
                'base_sha': 'base',
                'head_sha': 'head',
                'diff_digest': 'digest',
                'iteration': 0,
                'remote_url': None,
                'supersedes_job_id': None,
                'created_at': run.created_at.isoformat().replace('+00:00', 'Z'),
                'updated_at': run.updated_at.isoformat().replace('+00:00', 'Z'),
            }
        ],
        'error': None,
    }


def test_jobs_filters_repeated_states_across_scenarios(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Union repeated durable-state selections for both job scenarios."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)
    issue = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url='https://github.com/acme/widgets/issues/12',
        namespace='acme',
        project='widgets',
        issue_number=12,
        title='Feature',
        author='author',
        source_updated_at='2026-09-08T08:00:00Z',
        source_digest='sha256:' + 'd' * 64,
    )
    store.add_issue(issue)
    published = replace(issue, state=RunState.PUBLISHED)
    store.update_issue(published, RunState.QUEUED)

    result = main(
        [
            '--database',
            str(database),
            'jobs',
            '--state',
            'queued',
            '--state',
            'published',
        ]
    )

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert {job['scenario'] for job in document['jobs']} == {
        'local_changes',
        'issue_review',
    }
    assert {job['state'] for job in document['jobs']} == {'queued', 'published'}

    assert main(['--database', str(database), 'jobs', '--state', 'failed']) == 0
    assert json.loads(capsys.readouterr().out)['jobs'] == []


def test_jobs_attention_selects_exact_human_action_states(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose the shared human-action set and union it with explicit states."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    repo = tmp_path / 'repo'
    repo.mkdir()
    initialize_git_repo(repo)
    for state in [*sorted(HUMAN_ACTION_STATES, key=str), RunState.PUBLISHED]:
        run = Run.create_local(repo, repo, 'base', 'head', f'digest-{state}')
        store.add(run)
        store.update(replace(run, state=state), RunState.QUEUED)
    issue = IssueJob.create(
        provider='gitlab',
        host='gitlab.com',
        remote_url='https://gitlab.com/acme/widgets/-/issues/12',
        namespace='acme',
        project='widgets',
        issue_number=12,
        title='Feature',
        author='author',
        source_updated_at='2026-09-08T08:00:00Z',
        source_digest='sha256:' + 'e' * 64,
    )
    store.add_issue(issue)
    interrupted = replace(issue, state=RunState.INTERRUPTED)
    store.update_issue(interrupted, RunState.QUEUED)
    before = database.read_bytes()

    assert main(['--database', str(database), 'jobs', '--attention']) == 0

    attention = json.loads(capsys.readouterr().out)
    assert {job['state'] for job in attention['jobs']} == {
        state.value for state in HUMAN_ACTION_STATES
    }
    assert {job['scenario'] for job in attention['jobs']} == {
        'local_changes',
        'issue_review',
    }
    assert database.read_bytes() == before

    assert (
        main(
            [
                '--database',
                str(database),
                'jobs',
                '--attention',
                '--state',
                'published',
            ]
        )
        == 0
    )
    combined = json.loads(capsys.readouterr().out)
    assert {job['state'] for job in combined['jobs']} == {
        *(state.value for state in HUMAN_ACTION_STATES),
        'published',
    }


def test_jobs_rejects_unknown_state_with_stable_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject a state typo without modifying or consulting durable state."""

    database = tmp_path / 'missing.db'

    result = main(['--database', str(database), 'jobs', '--state', 'needs-coffee'])

    assert result == 2
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 22,
        'error': {
            'code': 'invalid_job_state',
            'message': 'unknown durable job state: needs-coffee',
        },
    }
    assert not database.exists()


def test_jobs_reports_broken_worktree_and_excludes_it_from_attention(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep unrunnable jobs visible without presenting them as actionable."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    missing = tmp_path / 'missing'
    run = Run.create_local(missing, missing, 'base', 'head', 'digest')
    store.add(run)
    store.update(replace(run, state=RunState.CHANGES_REQUESTED), RunState.QUEUED)
    before = database.read_bytes()

    assert main(['--database', str(database), 'jobs']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['jobs'][0]['worktree_status'] == 'missing'
    assert main(['--database', str(database), 'jobs', '--attention']) == 0
    assert json.loads(capsys.readouterr().out)['jobs'] == []
    assert database.read_bytes() == before

    assert (
        main(
            [
                '--database',
                str(database),
                'jobs',
                '--attention',
                '--state',
                'changes_requested',
            ]
        )
        == 0
    )
    selected = json.loads(capsys.readouterr().out)
    assert [job['job_id'] for job in selected['jobs']] == [str(run.id)]


def test_cancel_records_reason_without_deleting_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Cancel an unrunnable job and preserve its durable evidence."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    missing = tmp_path / 'missing'
    run = Run.create_local(missing, missing, 'base', 'head', 'digest')
    store.add(run)
    evidence = tmp_path / 'runs' / str(run.id) / '.integrity.json'
    evidence.parent.mkdir(parents=True)
    evidence.write_text('keep me')
    arguments = [
        '--database',
        str(database),
        'cancel',
        str(run.id),
        '--reason',
        'worktree removed',
    ]

    assert main(arguments) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['state'] == 'cancelled'
    assert evidence.read_text() == 'keep me'
    transition = store.list_transitions(str(run.id))[-1]
    assert transition.to_state is RunState.CANCELLED
    assert transition.reason == 'worktree removed'
    assert main(arguments) == 2
    error = json.loads(capsys.readouterr().out)
    assert error['error']['code'] == 'job_not_cancellable'


def test_cancel_refuses_job_with_available_worktree(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Require an unrunnable worktree before terminating a source-code job."""

    repository = tmp_path / 'repo'
    repository.mkdir()
    initialize_git_repo(repository)
    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    run = Run.create_local(repository, repository, 'base', 'head', 'digest')
    store.add(run)

    assert (
        main(
            [
                '--database',
                str(database),
                'cancel',
                str(run.id),
                '--reason',
                'wrong job',
            ]
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'job_not_cancellable'
    assert store.get(run.id).state is RunState.QUEUED


def test_cancel_reports_unrecognized_persisted_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return the established JSON error when a job state cannot be decoded."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    missing = tmp_path / 'missing'
    run = Run.create_local(missing, missing, 'base', 'head', 'digest')
    store.add(run)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET state = 'future_state' WHERE id = ?", (str(run.id),)
        )

    assert (
        main(['--database', str(database), 'cancel', str(run.id), '--reason', 'stale'])
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['job_id'] == str(run.id)
    assert document['error']['code'] == 'unknown_job_state'


def test_cancel_reports_issue_job_as_not_cancellable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinguish an existing issue-review job from an unknown identifier."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    issue = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url='https://github.com/acme/widgets/issues/12',
        namespace='acme',
        project='widgets',
        issue_number=12,
        title='Feature',
        author='author',
        source_updated_at='2026-09-08T08:00:00Z',
        source_digest='sha256:' + 'a' * 64,
    )
    store.add_issue(issue)

    assert (
        main(
            [
                '--database',
                str(database),
                'cancel',
                issue.id,
                '--reason',
                'wrong scenario',
            ]
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['job_id'] == issue.id
    assert document['error'] == {
        'code': 'job_not_cancellable',
        'message': 'cancellation applies to source-code jobs; '
        'this is an issue-review job',
    }


def test_run_writes_to_identifier_shard_in_non_utc_timezone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep end-to-end placement bound to the ID rather than local time."""

    original_timezone = os.environ.get('TZ')
    try:
        monkeypatch.setenv('TZ', 'Pacific/Kiritimati')
        time.tzset()
        job_id = '20260909T063000Z-b4517e73'
        context = create_worker_run(tmp_path, job_id=job_id)
        reviewer = tmp_path / 'reviewer.py'
        write_reviewer(reviewer, 'approved')

        result = main(run_arguments(context, reviewer=reviewer))
    finally:
        if original_timezone is None:
            monkeypatch.delenv('TZ', raising=False)
        else:
            monkeypatch.setenv('TZ', original_timezone)
        time.tzset()

    assert result == 0
    assert evidence_directory(context) == context.runs_directory / '2026/09/09' / job_id
    assert (evidence_directory(context) / 'execution.json').is_file()
    assert not (context.runs_directory / job_id).exists()


def test_job_selects_one_job_by_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a single job with no current tasks before execution."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    first = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'first')
    second = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'second')
    store.add(first)
    store.add(second)
    runs = tmp_path / 'runs'
    resolve_evidence_path(runs, str(first.id)).mkdir(parents=True)

    result = main(
        [
            '--database',
            str(database),
            'job',
            str(first.id),
            '--runs-directory',
            str(runs),
        ]
    )

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == 22
    assert document['job']['job_id'] == str(first.id)
    assert document['job']['current'] == []


def test_job_reads_persisted_review_state_without_initializing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expose the renamed state without requiring a separate init command."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(run)
    runs = tmp_path / 'runs'
    resolve_evidence_path(runs, str(run.id)).mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET state = 'awaiting_review' WHERE id = ?", (str(run.id),)
        )

    result = main(
        [
            '--database',
            str(database),
            'job',
            str(run.id),
            '--runs-directory',
            str(runs),
        ]
    )

    assert result == 0
    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == 22
    assert document['job']['state'] == 'reviewing'
    with sqlite3.connect(database) as connection:
        stored_state = connection.execute(
            'SELECT state FROM runs WHERE id = ?', (str(run.id),)
        ).fetchone()
    assert stored_state == ('awaiting_review',)


def test_jobs_lists_empty_jobs_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a stable empty collection for an initialized database."""

    database = tmp_path / 'state.db'
    JobStore(database).initialize()

    result = main(['--database', str(database), 'jobs'])

    assert result == 0
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 22,
        'jobs': [],
        'error': None,
    }


def test_task_commands_share_resolved_default_runs_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve one default evidence root for every evidence-aware view."""

    database = tmp_path / 'state.db'
    JobStore(database).initialize()
    actual_parent = tmp_path / 'actual'
    actual_parent.mkdir()
    linked_parent = tmp_path / 'linked'
    linked_parent.symlink_to(actual_parent, target_is_directory=True)
    monkeypatch.setattr(cli, 'DEFAULT_RUNS_DIRECTORY', linked_parent / 'runs')

    job = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    JobStore(database).add(job)
    resolve_evidence_path(actual_parent / 'runs', str(job.id)).mkdir(parents=True)

    for command, identifier in (
        ('job', str(job.id)),
        ('tasks', str(job.id)),
        ('task', f'{job.id}:000001-reviewer'),
    ):
        arguments = ['--database', str(database), command, identifier]
        result = main(arguments)
        document = json.loads(capsys.readouterr().out)
        assert result == (2 if command == 'task' else 0)
        if command == 'task':
            assert document['error']['code'] == 'task_not_found'


def test_run_and_task_share_default_runs_directory() -> None:
    """Keep evidence producers and consumers on the same default root."""

    parser = build_parser()

    run_args = parser.parse_args(['run', 'run-id', '--objective', 'Review.'])
    task_args = parser.parse_args(['task', 'job-id:000001-reviewer'])

    assert run_args.runs_directory == cli.DEFAULT_RUNS_DIRECTORY
    assert task_args.runs_directory == cli.DEFAULT_RUNS_DIRECTORY


def test_default_runs_directory_is_session_owned(
    isolated_default_runs_directory: Path,
) -> None:
    """Keep the session default off the live root so concurrency cannot leak in."""

    assert isolated_default_runs_directory == cli.DEFAULT_RUNS_DIRECTORY
    live_root = Path('~/.local/state/agent-orchestra/runs').expanduser()
    assert isolated_default_runs_directory != live_root
    assert not isolated_default_runs_directory.is_relative_to(live_root)


def test_unrelated_evidence_activity_does_not_reach_the_session_default(
    isolated_default_runs_directory: Path, tmp_path: Path
) -> None:
    """Ignore job directories another process creates while the session runs."""

    concurrent_root = tmp_path / 'other-process-runs'
    (concurrent_root / '2026' / '09' / '08' / 'job-id').mkdir(parents=True)

    assert not any(isolated_default_runs_directory.rglob('*'))


def test_default_database_is_session_owned(
    isolated_default_database: Path,
) -> None:
    """Keep the session default off the live database in the home directory."""

    assert isolated_default_database == cli.DEFAULT_DATABASE
    live_database = Path('~/.local/state/agent-orchestra/state.db').expanduser()
    assert isolated_default_database != live_database
    assert not isolated_default_database.exists()


def test_command_without_database_reaches_the_session_default(
    isolated_default_database: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove the redirect is load-bearing rather than incidentally unused."""

    opened: list[Path] = []

    class RecordingStore:
        """Record the resolved database instead of creating one."""

        def __init__(self, database: Path) -> None:
            opened.append(database)

        def initialize(self) -> None:
            return None

    # `init` is the cheapest command that resolves a database, so running it
    # without --database shows exactly where an omission lands. Recording the
    # path rather than letting the real store create the file keeps this test
    # from writing and then deleting the very file the session guard inspects,
    # which would let an unrelated test's leak pass unnoticed.
    monkeypatch.setattr(cli, 'JobStore', RecordingStore)

    assert main(['init']) == 0
    assert capsys.readouterr().out.strip() == f'initialized {isolated_default_database}'
    assert opened == [isolated_default_database]
    assert not isolated_default_database.exists()


def test_developer_settings_file_cannot_redirect_the_session_defaults(
    isolated_settings_source: Path,
    isolated_default_database: Path,
    isolated_default_runs_directory: Path,
) -> None:
    """Keep a real config.toml from steering the suite at developer state."""

    # Redirecting the module attributes alone would leave both guards open,
    # because settings take precedence over built-in defaults. The suite must
    # find no settings file at all.
    assert Path(os.environ['XDG_CONFIG_HOME']) == isolated_settings_source
    assert not (isolated_settings_source / 'agent-orchestra/config.toml').exists()

    settings = load_settings(
        default_database=cli.DEFAULT_DATABASE,
        default_runs_directory=cli.DEFAULT_RUNS_DIRECTORY,
    )

    assert settings.database.value == isolated_default_database
    assert settings.database.source == 'built_in'
    assert settings.runs_directory.value == isolated_default_runs_directory
    assert settings.runs_directory.source == 'built_in'


def test_job_reports_unknown_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a distinct error when the requested run does not exist."""

    database = tmp_path / 'state.db'
    JobStore(database).initialize()

    result = main(['--database', str(database), 'job', str(uuid4())])

    assert result == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'job_not_found'


@pytest.mark.parametrize(
    ('column', 'value', 'code'),
    [
        ('state', 'future_state', 'unknown_job_state'),
        ('scenario', 'future_scenario', 'unknown_job_scenario'),
    ],
)
def test_read_only_views_report_unrecognized_job_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    column: str,
    value: str,
    code: str,
) -> None:
    """Return stable JSON errors without hiding other readable jobs."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    unreadable = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    readable = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(unreadable)
    store.add(readable)
    with sqlite3.connect(database) as connection:
        connection.execute(
            f'UPDATE runs SET {column} = ? WHERE id = ?',  # noqa: S608
            (value, str(unreadable.id)),
        )

    commands = (
        ['jobs'],
        ['jobs', '--state', 'queued'],
        ['jobs', '--attention'],
        ['job', str(unreadable.id)],
        ['tasks', str(unreadable.id)],
        ['audit', str(unreadable.id)],
    )
    for command in commands:
        assert main(['--database', str(database), *command]) == 2
        document = json.loads(capsys.readouterr().out)
        assert document['schema_version'] == 22
        assert document['error']['code'] == code
        if command[0] == 'jobs':
            listed_ids = {item['job_id'] for item in document['jobs']}
            assert str(unreadable.id) in listed_ids
            if '--attention' not in command:
                assert str(readable.id) in listed_ids
            broken = next(
                item
                for item in document['jobs']
                if item['job_id'] == str(unreadable.id)
            )
            assert broken['error']['code'] == code


@pytest.mark.parametrize('state', ['future_state', 'awaiting_review'])
def test_read_only_views_report_unrecognized_issue_job_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    """Apply the same persisted-state error contract to issue-review jobs."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()
    issue = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url='https://github.com/acme/widgets/issues/1',
        namespace='acme',
        project='widgets',
        issue_number=1,
        title='Issue',
        author='octocat',
        source_updated_at='2026-09-08T00:00:00Z',
        source_digest='sha256:' + 'a' * 64,
    )
    store.add_issue(issue)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE issue_jobs SET state = ? WHERE id = ?',
            (state, issue.id),
        )

    assert main(['--database', str(database), 'jobs']) == 2
    listing = json.loads(capsys.readouterr().out)
    assert listing['error']['code'] == 'unknown_job_state'
    assert listing['jobs'][0]['job_id'] == issue.id

    for command in ('job', 'tasks', 'audit'):
        assert main(['--database', str(database), command, issue.id]) == 2
        document = json.loads(capsys.readouterr().out)
        assert document['error']['code'] == 'unknown_job_state'


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
    run_directory = evidence_directory(enqueued_run)
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
    integrity = json.loads((run_directory / '.integrity.json').read_text())
    indexed_paths = {entry['path'] for entry in integrity['entries']}
    indexed_types = {
        entry['path']: entry['evidence_type'] for entry in integrity['entries']
    }
    assert indexed_types['messages/000002-review-result.json'] == 'review_result'
    assert {
        'execution.json',
        'messages/000001-review-request.json',
        'messages/000002-review-result.json',
        'artifacts/review-0001.md',
        'invocations/000001-reviewer.json',
        'logs/000001-reviewer.stdout.log',
        'logs/000001-reviewer.stderr.log',
    } <= indexed_paths
    assert json.loads(capsys.readouterr().out) == {
        'schema_version': 22,
        'job_id': str(enqueued_run.run.id),
        'state': 'awaiting_commit_authorization',
        'error': None,
    }
    assert (
        main(
            [
                '--database',
                str(enqueued_run.database),
                'audit',
                str(enqueued_run.run.id),
                '--runs-directory',
                str(enqueued_run.runs_directory),
                '--verify',
            ]
        )
        == 0
    )
    audit = json.loads(capsys.readouterr().out)
    assert audit['result'] == 'verified', audit['findings']


def test_run_persists_reported_effective_model_metadata(tmp_path: Path) -> None:
    """Consume normal-exit provenance before opening the commit gate."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_provenance_reviewer(reviewer)

    result = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review the change.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(),
        timeout_seconds=30,
        reviewer_identity=InvocationIdentity(
            vendor='anthropic', model='requested-model', runtime='claude-code'
        ),
    )

    assert result.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    run_directory = evidence_directory(context)
    invocation = json.loads(
        (run_directory / 'invocations/000001-reviewer.json').read_text()
    )
    assert invocation['schema_version'] == 4
    assert invocation['task_id'] == f'{context.run.id}:000001-reviewer'
    assert invocation['invocation_id'] == (
        f'{context.run.id}:000001-reviewer:attempt-0001'
    )
    assert invocation['status'] == 'completed'
    assert invocation['conclusion'] == 'succeeded'
    assert invocation['response_received_at'] is not None
    assert invocation['validation_started_at'] is not None
    assert invocation['requested_model'] == 'requested-model'
    assert invocation['effective_models'] == ['claude-primary', 'claude-fallback']
    assert invocation['effective_model_status'] == 'reported'
    assert not tuple(run_directory.glob('.*.runtime.json'))


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
    stdout_log = evidence_directory(enqueued_run) / 'logs/000001-reviewer.stdout.log'
    assert stdout_log.read_bytes() == b'caf\xe9 latin-1 byte\n'


@pytest.mark.parametrize('interrupted_role', ['reviewer', 'developer'])
def test_worker_persists_interrupted_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interrupted_role: str,
) -> None:
    """Keep pre-activation interruption uncertain and non-retryable."""

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
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=('unused-developer',),
            timeout_seconds=30,
        )

    assert context.store.get(context.run.id).state is RunState.INTERRUPTED
    invocation_files = sorted(
        (evidence_directory(context) / 'invocations').glob('*.json')
    )
    invocation_names = [path.name for path in invocation_files]
    invocation = json.loads(invocation_files[-1].read_text())
    assert invocation['role'] == interrupted_role
    assert invocation['status'] == 'pending'
    assert invocation['conclusion'] is None
    assert invocation['interrupted'] is False

    assert main(resume_arguments(context)) == 2
    error = json.loads(capsys.readouterr().out)
    assert error['error']['code'] == 'resume_activation_uncertain'
    assert (
        sorted(
            path.name
            for path in (evidence_directory(context) / 'invocations').glob('*.json')
        )
        == invocation_names
    )


@pytest.mark.parametrize('interrupted_role', ['reviewer', 'developer'])
def test_worker_finalizes_interruption_after_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted_role: str,
) -> None:
    """Complete interrupted evidence once process activation is durable."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_loop_reviewer(reviewer)
    original_execute = CommandAgentAdapter.execute

    def interrupt_selected(
        adapter: CommandAgentAdapter, request: AgentRequest
    ) -> AgentResult:
        """Persist activation before interrupting the selected role."""

        if request.role == interrupted_role:
            assert request.on_started is not None
            request.on_started()
            raise KeyboardInterrupt
        return original_execute(adapter, request)

    monkeypatch.setattr(CommandAgentAdapter, 'execute', interrupt_selected)
    with pytest.raises(KeyboardInterrupt):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=('unused-developer',),
            timeout_seconds=30,
        )

    invocation_files = sorted(
        (evidence_directory(context) / 'invocations').glob('*.json')
    )
    invocation = json.loads(invocation_files[-1].read_text())
    assert invocation['role'] == interrupted_role
    assert invocation['status'] == 'completed'
    assert invocation['conclusion'] == 'interrupted'
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


def test_run_selects_configured_reviewer_set(
    tmp_path: Path,
    enqueued_run: CliRunContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolve a named reviewer set into the worker's immutable batch plan."""

    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra/config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[reviewer_sets.default]\n'
        'members = [\n'
        '  { id = "security", runtime = "codex" },\n'
        '  { id = "portability", runtime = "claude-code" },\n'
        ']\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    observed: dict[str, object] = {}

    def review_set(**kwargs: object) -> Run:
        """Capture the selected batch without starting its reviewers."""

        observed.update(kwargs)
        return enqueued_run.run

    monkeypatch.setattr('agent_orchestra.cli.run_queued_reviewer_set', review_set)

    assert main(run_arguments(enqueued_run, '--reviewer-set', 'default')) == 0

    plan = observed['reviewer_plan']
    assert isinstance(plan, ReviewerExecutionPlan)
    assert [reviewer.reviewer_id for reviewer in plan.reviewers] == [
        'security',
        'portability',
    ]


def test_default_database_is_outside_a_repo_in_the_home_directory() -> None:
    """Keep default orchestration state outside a typical reviewed repo."""

    # This asserts the production location, so it deliberately uses the
    # module-level import rather than cli.DEFAULT_DATABASE: the session guard
    # redirects the attribute, and reading it here would assert a temporary path
    # and quietly stop checking anything.
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
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        timeout_seconds=30,
        max_iterations=2,
    )

    assert result.state.value == 'awaiting_commit_authorization'
    assert result.iteration == 2
    assert result.diff_digest != digest
    messages = evidence_directory(context) / 'messages'
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-review-request.json',
        '000006-review-result.json',
    ]
    integrity = json.loads(
        (evidence_directory(context) / '.integrity.json').read_text()
    )
    indexed_types = {
        entry['path']: entry['evidence_type'] for entry in integrity['entries']
    }
    assert {
        path: _canonical_evidence_type(path)
        for path in indexed_types
        if path.startswith('messages/')
    } == {
        path: evidence_type
        for path, evidence_type in indexed_types.items()
        if path.startswith('messages/')
    }
    assert indexed_types['messages/000004-developer-handoff.json'] == (
        'developer_handoff'
    )
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
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        timeout_seconds=30,
        max_iterations=3,
    )

    assert blocked.id == context.run.id
    assert blocked.state is RunState.VALIDATION_REQUIRED
    messages = evidence_directory(context) / 'messages'
    assert (messages / '000004-developer-handoff.json').is_file()
    assert not (
        evidence_directory(context) / 'logs/000004-rejected-developer-handoff.json'
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
        'schema_version': 22,
        'job_id': str(context.run.id),
        'state': 'awaiting_commit_authorization',
        'error': None,
    }

    assert main(resume_arguments(context)) == 2
    repeated = json.loads(capsys.readouterr().out)
    assert repeated['error']['code'] == 'job_not_resumable'
    assert repeated['error']['message'] == (
        'job is not resumable from awaiting_commit_authorization'
    )
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
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        timeout_seconds=30,
        developer_timeout_seconds=1,
        max_iterations=3,
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
    messages = evidence_directory(context) / 'messages'
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
    invocations = evidence_directory(context) / 'invocations'
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
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        timeout_seconds=30,
        max_iterations=3,
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
        evidence_directory(context)
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
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        timeout_seconds=30,
        max_iterations=3,
    )

    messages = evidence_directory(context) / 'messages'
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
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        timeout_seconds=30,
        max_iterations=3,
    )

    messages = evidence_directory(context) / 'messages'
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
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=1,
            max_iterations=3,
        )

    messages = evidence_directory(context) / 'messages'
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
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=max_iterations,
        )

    assert context.store.get(context.run.id).state is RunState.FAILED
    failure = json.loads((evidence_directory(context) / 'failure.json').read_text())
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
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        timeout_seconds=30,
        max_iterations=2,
    )

    assert result.state.value == 'changes_requested'
    evidence = json.loads(
        (evidence_directory(context) / 'decision-required.json').read_text()
    )
    assert evidence['reason']['code'] == 'developer_disagreement'
    assert 'disputed every finding' in evidence['reason']['message']
    assert not (evidence_directory(context) / 'failure.json').exists()


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
        (evidence_directory(enqueued_run) / 'failure.json').read_text()
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
            evidence_directory(enqueued_run) / 'invocations/000001-reviewer.json'
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
        evidence_directory(enqueued_run) / 'invocations/000001-reviewer.json'
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
    messages = evidence_directory(enqueued_run) / 'messages'
    request_path = messages / '000001-review-request.json'
    gapped_path = messages / '000003-review-request.json'
    request_path.rename(gapped_path)
    assert main(resume_arguments(enqueued_run)) == 2
    invalid_chain = json.loads(capsys.readouterr().out)
    assert invalid_chain['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    gapped_path.rename(request_path)
    execution_path = evidence_directory(enqueued_run) / 'execution.json'
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
    reviewer_record = execution.pop('reviewer')
    execution['schema_version'] = 3
    execution['reviewer_plan'] = {
        'schema_version': 1,
        'reviewer_set_id': 'default',
        'aggregation_policy': 'all_required',
        'reviewers': [
            {'reviewer_id': reviewer_id, **reviewer_record}
            for reviewer_id in ('security', 'portability')
        ],
    }
    execution_path.write_text(json.dumps(execution))

    assert main(resume_arguments(enqueued_run)) == 2
    invalid_reviewer_set = json.loads(capsys.readouterr().out)
    assert invalid_reviewer_set['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    execution['schema_version'] = 2
    execution['reviewer'] = reviewer_record
    execution.pop('reviewer_plan')
    execution_path.write_text(json.dumps(execution))
    write_reviewer(reviewer, 'approved')

    assert main(resume_arguments(enqueued_run)) == 0

    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
    ]
    invocations = evidence_directory(enqueued_run) / 'invocations'
    assert sorted(path.name for path in invocations.iterdir()) == [
        '000001-reviewer-attempt-0002.json',
        '000001-reviewer.json',
    ]
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_reports_unrecognized_interrupted_origin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Return resume's JSON contract for an unknown transition origin."""

    reviewer = tmp_path / 'reviewer.py'
    reviewer.write_text('"""Slow reviewer."""\nimport time\ntime.sleep(5)\n')
    assert main(run_arguments(enqueued_run, '--timeout', '1', reviewer=reviewer)) == 2
    capsys.readouterr()
    with sqlite3.connect(enqueued_run.database) as connection:
        connection.execute(
            """UPDATE transitions SET from_state = 'future_state'
            WHERE job_id = ? AND to_state = 'interrupted'""",
            (str(enqueued_run.run.id),),
        )

    assert main(resume_arguments(enqueued_run)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'unknown_job_state'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED


def test_resume_revalidates_reviewer_response_without_relaunching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enqueued_run: CliRunContext,
) -> None:
    """Recover a response persisted before its validation milestone."""

    reviewer = tmp_path / 'reviewer.py'
    counter = tmp_path / 'reviewer-count.txt'
    write_reviewer(reviewer, 'approved')
    add_execution_counter(reviewer, counter)
    original_write = InvocationEvidenceStore.write

    def fail_validation_record(
        self: InvocationEvidenceStore, path: Path, record: InvocationRecord
    ) -> None:
        """Simulate a crash before the reviewer validation milestone is durable."""

        if (
            record.role == 'reviewer'
            and record.status == 'running'
            and record.validation_started_at is not None
        ):
            message = 'simulated validation milestone failure'
            raise OSError(message)
        original_write(self, path, record)

    monkeypatch.setattr(InvocationEvidenceStore, 'write', fail_validation_record)
    with pytest.raises(OSError, match='simulated validation milestone failure'):
        run_queued_review(
            context=WorkerContext(
                store=enqueued_run.store,
                runs_directory=enqueued_run.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=enqueued_run.run,
            objective='Review.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(),
            timeout_seconds=30,
        )
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.REVIEWING

    monkeypatch.setattr(InvocationEvidenceStore, 'write', original_write)
    assert main(resume_arguments(enqueued_run)) == 0

    assert counter.read_text().splitlines() == ['1']
    record = json.loads(
        (
            evidence_directory(enqueued_run) / 'invocations/000001-reviewer.json'
        ).read_text()
    )
    assert record['status'] == 'completed'
    assert record['conclusion'] == 'succeeded'
    assert record['response_received_at'] is not None
    assert record['validation_started_at'] is not None
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_applies_completed_reviewer_conclusion_without_relaunching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enqueued_run: CliRunContext,
) -> None:
    """Recover when a completed review predates its workflow transition."""

    reviewer = tmp_path / 'reviewer.py'
    counter = tmp_path / 'reviewer-count.txt'
    write_reviewer(reviewer, 'approved')
    add_execution_counter(reviewer, counter)
    original_update = enqueued_run.store.update

    def fail_decision(run: Run, *, expected_state: RunState) -> None:
        """Simulate a crash after completion but before the review decision."""

        if run.state is RunState.APPROVED and expected_state is RunState.REVIEWING:
            message = 'simulated workflow transition failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(enqueued_run.store, 'update', fail_decision)
    with pytest.raises(OSError, match='simulated workflow transition failure'):
        run_queued_review(
            context=WorkerContext(
                store=enqueued_run.store,
                runs_directory=enqueued_run.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=enqueued_run.run,
            objective='Review.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(),
            timeout_seconds=30,
        )
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.REVIEWING

    monkeypatch.setattr(enqueued_run.store, 'update', original_update)
    assert main(resume_arguments(enqueued_run)) == 0

    assert counter.read_text().splitlines() == ['1']
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_advances_persisted_approval_without_relaunching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enqueued_run: CliRunContext,
) -> None:
    """Recover after approval persists but its authorization wait does not."""

    reviewer = tmp_path / 'reviewer.py'
    counter = tmp_path / 'reviewer-count.txt'
    write_reviewer(reviewer, 'approved')
    add_execution_counter(reviewer, counter)
    original_update = enqueued_run.store.update

    def fail_authorization_wait(run: Run, *, expected_state: RunState) -> None:
        """Simulate a crash immediately after durable approval."""

        if (
            run.state is RunState.AWAITING_COMMIT_AUTHORIZATION
            and expected_state is RunState.APPROVED
        ):
            message = 'simulated authorization transition failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(enqueued_run.store, 'update', fail_authorization_wait)
    with pytest.raises(OSError, match='simulated authorization transition failure'):
        run_queued_review(
            context=WorkerContext(
                store=enqueued_run.store,
                runs_directory=enqueued_run.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=enqueued_run.run,
            objective='Review.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(),
            timeout_seconds=30,
        )
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.APPROVED

    monkeypatch.setattr(enqueued_run.store, 'update', original_update)
    assert main(resume_arguments(enqueued_run)) == 0

    assert counter.read_text().splitlines() == ['1']
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_starts_persisted_remediation_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover after a remediation request persists before its active state."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    developer_counter = tmp_path / 'developer-count.txt'
    write_loop_reviewer(reviewer)
    write_developer(developer)
    add_execution_counter(developer, developer_counter)
    original_update = context.store.update

    def fail_developing(run: Run, *, expected_state: RunState) -> None:
        """Simulate a crash after the request but before active development."""

        if (
            run.state is RunState.DEVELOPING
            and expected_state is RunState.CHANGES_REQUESTED
        ):
            message = 'simulated development transition failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(context.store, 'update', fail_developing)
    with pytest.raises(OSError, match='simulated development transition failure'):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            developer_timeout_seconds=30,
            max_iterations=3,
        )
    assert context.store.get(context.run.id).state is RunState.CHANGES_REQUESTED
    assert (
        evidence_directory(context) / 'messages/000003-remediation-request.json'
    ).is_file()

    monkeypatch.setattr(context.store, 'update', original_update)
    assert main(resume_arguments(context)) == 0

    assert developer_counter.read_text().splitlines() == ['1']
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_recovered_review_survives_pre_attempt_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume again when recovered review acceptance predates attempt evidence."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    developer_counter = tmp_path / 'developer-count.txt'
    write_loop_reviewer(reviewer)
    write_developer(developer)
    add_execution_counter(developer, developer_counter)
    original_update = context.store.update

    def fail_review_decision(run: Run, *, expected_state: RunState) -> None:
        """Simulate a crash before the accepted review decision persists."""

        if (
            run.state is RunState.CHANGES_REQUESTED
            and expected_state is RunState.REVIEWING
        ):
            message = 'simulated review decision failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(context.store, 'update', fail_review_decision)
    with pytest.raises(OSError, match='simulated review decision failure'):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            developer_timeout_seconds=30,
            max_iterations=3,
        )
    assert context.store.get(context.run.id).state is RunState.REVIEWING

    monkeypatch.setattr(context.store, 'update', original_update)
    original_record = invocations.record_invocation

    def fail_developer_record(
        attempt: AttemptIdentity, *args: Any, **kwargs: Any
    ) -> str:
        """Simulate a crash after activating development but before evidence."""

        if attempt.role is RuntimeRole.DEVELOPER:
            message = 'simulated developer record failure'
            raise OSError(message)
        return original_record(attempt, *args, **kwargs)

    monkeypatch.setattr(
        developer_remediation, 'record_invocation', fail_developer_record
    )
    with pytest.raises(OSError, match='simulated developer record failure'):
        resume_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.store.get(context.run.id),
        )
    assert context.store.get(context.run.id).state is RunState.DEVELOPING

    monkeypatch.setattr(developer_remediation, 'record_invocation', original_record)
    assert main(resume_arguments(context)) == 0

    assert developer_counter.read_text().splitlines() == ['1']
    assert (
        context.store.get(context.run.id).state
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
        (evidence_directory(enqueued_run) / 'failure.json').read_text()
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
    run_directory = evidence_directory(enqueued_run)
    artifact_path = run_directory / 'artifacts/review-0001.md'
    archived_path = (
        run_directory / 'logs/000002-rejected-review-artifact-attempt-0001.md'
    )
    assert not artifact_path.exists()
    assert archived_path.read_text() == '# Stale review\n'
    integrity = json.loads((run_directory / '.integrity.json').read_text())
    indexed = {entry['path']: entry for entry in integrity['entries']}
    assert 'artifacts/review-0001.md' not in indexed
    archived_relative = 'logs/000002-rejected-review-artifact-attempt-0001.md'
    assert indexed[archived_relative]['evidence_type'] == 'rejected_review_artifact'
    assert all((run_directory / path).is_file() for path in indexed)

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
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            developer_timeout_seconds=1,
            max_iterations=3,
        )

    assert context.store.get(context.run.id).state is RunState.INTERRUPTED
    invocation_path = evidence_directory(context) / 'invocations/000003-developer.json'
    invocation = invocation_path.read_text()
    first_attempt = json.loads(invocation)
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
    messages = evidence_directory(context) / 'messages'
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-review-request.json',
        '000006-review-result.json',
    ]
    invocations = evidence_directory(context) / 'invocations'
    assert (invocations / '000003-developer.json').is_file()
    retry = json.loads((invocations / '000003-developer-attempt-0002.json').read_text())
    assert retry['attempt'] == 2
    assert retry['timed_out'] is False
    assert retry['task_id'] == first_attempt['task_id']
    assert first_attempt['invocation_id'] == (
        f'{context.run.id}:000003-developer:attempt-0001'
    )
    assert retry['invocation_id'] == (f'{context.run.id}:000003-developer:attempt-0002')
    assert first_attempt['conclusion'] == 'timed_out'
    assert retry['conclusion'] == 'succeeded'


def test_resume_revalidates_developer_response_without_relaunching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover a developer response after edits but before validation is durable."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    counter = tmp_path / 'developer-count.txt'
    write_loop_reviewer(reviewer)
    write_developer(developer)
    add_execution_counter(developer, counter)
    original_write = InvocationEvidenceStore.write

    def fail_validation_record(
        self: InvocationEvidenceStore, path: Path, record: InvocationRecord
    ) -> None:
        """Simulate a crash before the developer validation milestone is durable."""

        if (
            record.role == 'developer'
            and record.status == 'running'
            and record.validation_started_at is not None
        ):
            message = 'simulated developer validation milestone failure'
            raise OSError(message)
        original_write(self, path, record)

    monkeypatch.setattr(InvocationEvidenceStore, 'write', fail_validation_record)
    with pytest.raises(
        OSError, match='simulated developer validation milestone failure'
    ):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=3,
        )
    assert context.store.get(context.run.id).state is RunState.DEVELOPING

    monkeypatch.setattr(InvocationEvidenceStore, 'write', original_write)
    assert main(resume_arguments(context)) == 0

    assert counter.read_text().splitlines() == ['1']
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    record = json.loads(
        (evidence_directory(context) / 'invocations/000003-developer.json').read_text()
    )
    assert record['status'] == 'completed'
    assert record['conclusion'] == 'succeeded'


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
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        timeout_seconds=30,
        max_iterations=3,
    )
    assert blocked.state is RunState.VALIDATION_REQUIRED
    original_write = evidence_module.write_json_atomic

    def fail_recovery_request(
        path: Path, document: dict[str, object], evidence_type: Any
    ) -> None:
        """Simulate failure to persist only the recovery request."""

        if path.name == '000005-remediation-request.json':
            message = 'simulated write failure'
            raise OSError(message)
        original_write(path, document, evidence_type)

    monkeypatch.setattr(worker, 'write_json_atomic', fail_recovery_request)
    assert main(resume_arguments(context)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED
    messages = evidence_directory(context) / 'messages'
    assert not (messages / '000005-remediation-request.json').exists()


def test_resume_recovers_request_when_activation_state_did_not_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launch after a recovery request persists but its active state does not."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    blocked = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        objective='Review and remediate.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(sys.executable, str(developer)),
        timeout_seconds=30,
        max_iterations=3,
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
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=blocked,
        )
    messages = evidence_directory(context) / 'messages'
    assert (messages / '000005-remediation-request.json').is_file()
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED
    invocations = evidence_directory(context) / 'invocations'
    assert not (invocations / '000005-developer.json').exists()

    monkeypatch.setattr(context.store, 'update', original_update)
    assert main(resume_arguments(context)) == 0
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    assert (invocations / '000005-developer.json').is_file()


@pytest.mark.parametrize('role', ['reviewer', 'developer'])
def test_concurrent_active_resumes_launch_one_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    """Let only the atomic first-attempt creator activate the selected role."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    if role == 'reviewer':
        write_reviewer(reviewer, 'approved')
    else:
        write_loop_reviewer(reviewer)
    write_developer(developer)
    original_record = invocations.record_invocation

    def crash_before_attempt(
        attempt: AttemptIdentity, *args: Any, **kwargs: Any
    ) -> str:
        """Leave the selected role active with a request but no attempt record."""

        if attempt.role == role:
            message = 'simulated crash before attempt persistence'
            raise OSError(message)
        return original_record(attempt, *args, **kwargs)

    monkeypatch.setattr(queued_review, 'record_invocation', crash_before_attempt)
    with pytest.raises(OSError, match='simulated crash before attempt persistence'):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=3,
        )
    monkeypatch.setattr(queued_review, 'record_invocation', original_record)
    active = context.store.get(context.run.id)
    expected_state = RunState.REVIEWING if role == 'reviewer' else RunState.DEVELOPING
    assert active.state is expected_state

    sequence = 1 if role == 'reviewer' else 3
    target = evidence_directory(context) / 'invocations' / f'{sequence:06d}-{role}.json'
    path_type = type(target)
    real_exists = path_type.exists
    barrier = Barrier(2)
    lock = Lock()
    initial_checks = 0

    def synchronized_exists(candidate: Path) -> bool:
        """Give both resumers the same pre-creation view of the attempt path."""

        nonlocal initial_checks
        should_wait = False
        if candidate == target:
            with lock:
                if initial_checks < 2:
                    initial_checks += 1
                    should_wait = True
        if should_wait:
            barrier.wait(timeout=5)
            return False
        return real_exists(candidate)

    monkeypatch.setattr(path_type, 'exists', synchronized_exists)
    original_execute = CommandAgentAdapter.execute
    activations: list[str] = []

    def count_execute(
        adapter: CommandAgentAdapter, request: AgentRequest
    ) -> AgentResult:
        """Count process activations for the selected role."""

        if request.role == role:
            activations.append(role)
        return original_execute(adapter, request)

    monkeypatch.setattr(CommandAgentAdapter, 'execute', count_execute)
    outcomes: list[str] = []

    def resume_once() -> None:
        """Resume through an independent store handle like a separate CLI process."""

        try:
            resume_review(
                context=WorkerContext(
                    store=JobStore(context.database),
                    runs_directory=context.runs_directory,
                    digest_worktree=_working_tree_digest,
                ),
                run=active,
            )
        except WorkerError as error:
            outcomes.append(error.code or type(error).__name__)
        except BaseException as error:  # pragma: no cover - assertion reports type
            outcomes.append(type(error).__name__)
        else:
            outcomes.append('success')

    threads = (Thread(target=resume_once), Thread(target=resume_once))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ['resume_activation_uncertain', 'success']
    assert activations == [role]


@pytest.mark.parametrize(
    'case',
    [
        ('approved', '30', 0, 'succeeded', None),
        ('nonzero', '30', 2, 'failed', 'codex exec failed with code 9'),
        ('timeout', '8', 2, 'timed_out', 'codex review timed out'),
    ],
)
def test_builtin_run_exposes_child_output_through_task_view(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    enqueued_run: CliRunContext,
    case: tuple[str, str, int, str, str | None],
) -> None:
    """Retain built-in child streams across success, error, and timeout."""

    # The timeout case exercises the adapter's own bound on the child, not the
    # orchestrator's bound on the adapter. The adapter allows the child
    # timeout_seconds - 5, so --timeout 8 kills the child three seconds in while
    # the orchestrator is still waiting, and the child's twenty-second sleep
    # keeps it alive well past that deadline. Both margins have to stay wide:
    # equal deadlines make which bound fires a race, which is what made this
    # test flaky under CI load. test_run_marks_reviewer_timeout covers the
    # orchestrator killing an adapter that overruns, where nothing competes.
    # Either bound records timed_out: the adapter reports its own expiry through
    # the runtime metadata sidecar, so the two agree.
    mode, timeout, expected_result, conclusion, diagnostic = case
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
                'task',
                f'{enqueued_run.run.id}:000001-reviewer',
                '--runs-directory',
                str(enqueued_run.runs_directory),
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    attempt = document['task']['attempts'][0]
    assert 'child stdout\n' in attempt['streams']['stdout']['content']
    stderr = attempt['streams']['stderr']['content']
    assert 'child stderr\n' in stderr
    assert attempt['requested_model'] == 'test-model'
    assert attempt['effective_models'] == []
    assert attempt['effective_model_status'] == 'unavailable'
    if diagnostic is not None:
        assert diagnostic in stderr, (
            f'expected the adapter to report {diagnostic!r}; '
            f'the child ended for another reason. stderr: {stderr!r}'
        )
    assert attempt['conclusion'] == conclusion
    assert attempt['timed_out'] is (conclusion == 'timed_out')


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
    run = JobStore(database).list_runs()[0]

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
        ]
    )

    assert result == 2
    assert JobStore(database).get(run.id).state.value == 'queued'
    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out)['error'] == {
        'code': 'worker_error',
        'message': 'state database must be outside the worktree',
    }


def test_run_missing_database_is_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep an expected run lookup failure in its versioned JSON contract."""

    database = tmp_path / 'missing.db'

    assert (
        main(
            [
                '--database',
                str(database),
                'run',
                'job-1',
                '--objective',
                'Review the change.',
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out) == {
        'schema_version': 22,
        'job_id': 'job-1',
        'error': {
            'code': 'state_database_not_found',
            'message': f'state database not found: {database}',
        },
    }


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
    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out)['error']['code'] == 'worker_error'
    assert 'evidence directory must be outside' in captured.out


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
            '--runs-directory',
            str(enqueued_run.runs_directory),
            '--',
            '/usr/bin/true',
        ]
    )

    assert result == 2
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.QUEUED
    captured = capsys.readouterr()
    assert captured.err == ''
    assert json.loads(captured.out)['error']['code'] == 'worker_error'
    assert 'cannot compute worktree digest' in captured.out


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
    codex_home = tmp_path / 'codex=custom'
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
            '--skill-home',
            f'codex={codex_home}',
            '--skill-home',
            f'claude-code={claude_home}',
        ]
    )

    assert result == 0
    assert 'installed example-skill for codex' in capsys.readouterr().out
    assert (codex_home / 'skills/example-skill/SKILL.md').is_file()
    assert (claude_home / 'skills/example-skill/SKILL.md').is_file()


@pytest.mark.parametrize(
    ('override', 'message'),
    [
        ('codex', 'expected RUNTIME=PATH'),
        ('codex=', 'expected RUNTIME=PATH'),
        ('=/tmp/skills', 'runtime_unknown: '),
        ('unknown=/tmp/skills', 'runtime_unknown: unknown'),
    ],
)
def test_skills_install_rejects_malformed_home_override(
    override: str, message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject malformed or unknown runtime skill-home overrides."""

    with pytest.raises(SystemExit) as error:
        main(
            [
                'skills',
                'install',
                '--skill',
                'agent-orchestra-developer',
                '--skill-home',
                override,
            ]
        )

    assert error.value.code == 2
    assert message in capsys.readouterr().err


def test_skills_install_rejects_duplicate_home_override(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject two skill-home overrides for the same runtime."""

    result = main(
        [
            'skills',
            'install',
            '--skill',
            'agent-orchestra-developer',
            '--skill-home',
            f'codex={tmp_path / "first"}',
            '--skill-home',
            f'codex={tmp_path / "second"}',
        ]
    )

    assert result == 2
    assert 'skill home specified twice for codex' in capsys.readouterr().err
