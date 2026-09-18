"""Shared builders for command-line integration tests."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_orchestra.cli import (
    _working_tree_digest,
)
from agent_orchestra.evidence import (
    resolve_evidence_path,
)
from agent_orchestra.models import Run
from agent_orchestra.store import JobStore

_AGENT_FIXTURES = Path(__file__).parent / 'data' / 'cli_agents'


def _config_path(path: Path) -> Path:
    """Return the sidecar configuration path for a copied test agent."""

    return path.with_name(f'{path.name}.json')


def _install_agent(path: Path, fixture: str, config: dict[str, Any]) -> None:
    """Copy a test agent fixture and write its sidecar configuration."""

    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_AGENT_FIXTURES / fixture, path)
    _config_path(path).write_text(json.dumps(config))


def configure_agent(path: Path, **overrides: Any) -> None:
    """Update behavior in a copied test agent's sidecar configuration."""

    config_path = _config_path(path)
    config = json.loads(config_path.read_text())
    assert isinstance(config, dict)
    config.update(overrides)
    config_path.write_text(json.dumps(config))


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
    """Install a deterministic reviewer command for CLI integration tests."""

    _install_agent(
        path,
        'reviewer.py',
        {
            'verdict': verdict,
            'write_artifact': write_artifact,
            'loop': False,
            'provenance': False,
            'execution_counter': None,
            'artifact_content': '# Review\n',
            'sleep_iteration': None,
            'sleep_seconds': 0,
        },
    )


def write_provenance_reviewer(path: Path) -> None:
    """Write an approved reviewer that reports effective model provenance."""

    _install_agent(
        path,
        'reviewer.py',
        {
            'verdict': 'approved',
            'write_artifact': True,
            'loop': False,
            'provenance': True,
            'execution_counter': None,
            'artifact_content': '# Review\n',
            'sleep_iteration': None,
            'sleep_seconds': 0,
        },
    )


def write_loop_reviewer(path: Path) -> None:
    """Write a reviewer that requests one remediation and then approves."""

    _install_agent(
        path,
        'reviewer.py',
        {
            'verdict': 'approved',
            'write_artifact': True,
            'loop': True,
            'provenance': False,
            'execution_counter': None,
            'artifact_content': '# Review\n',
            'sleep_iteration': None,
            'sleep_seconds': 0,
        },
    )


def add_execution_counter(path: Path, counter: Path) -> None:
    """Configure a copied test agent to count each process activation."""

    configure_agent(path, execution_counter=str(counter))


def write_developer(
    path: Path, *, change_worktree: bool = True, disposition: str = 'addressed'
) -> None:
    """Install a deterministic developer that changes the diff and hands off."""

    _install_agent(
        path,
        'developer.py',
        {
            'change_worktree': change_worktree,
            'disposition': disposition,
            'recoverable': False,
            'execution_counter': None,
            'status': 'ready_for_review',
        },
    )


def write_recoverable_developer(path: Path) -> None:
    """Write a developer that blocks once and succeeds when resumed."""

    _install_agent(
        path,
        'developer.py',
        {
            'change_worktree': True,
            'disposition': 'addressed',
            'recoverable': True,
            'execution_counter': None,
            'status': 'ready_for_review',
        },
    )


def write_fake_codex(path: Path, *, mode: str) -> None:
    """Install a fake Codex executable that emits identifiable child output."""

    _install_agent(path, 'fake_codex.py', {'mode': mode})
    path.chmod(0o755)
