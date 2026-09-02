"""Command-line interface for local orchestration state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING

from agent_orchestra.models import Run
from agent_orchestra.skill_install import (
    AgentTarget,
    SkillInstallError,
    install_skills,
)
from agent_orchestra.store import RunNotFoundError, RunStore
from agent_orchestra.worker import WorkerError, run_queued_review

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_DATABASE = Path.home() / '.local/state/agent-orchestra/state.db'
HASH_CHUNK_SIZE = 1024 * 1024
STATE_DATABASE_INSIDE_WORKTREE = 'state database must be outside the worktree'


class GitCommandError(RuntimeError):
    """Raised when a required read-only Git command fails."""


def _require_external_database(database: Path, worktree: Path) -> None:
    """Reject mutable orchestration state inside the reviewed worktree."""

    if database.resolve().is_relative_to(worktree.resolve()):
        raise WorkerError(STATE_DATABASE_INSIDE_WORKTREE)


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    """Run a read-only Git command and return its raw output."""

    git = shutil.which('git')
    if git is None:
        message = 'git executable not found'
        raise GitCommandError(message)
    try:
        completed = subprocess.run(
            [git, '-C', str(repository), *arguments],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        diagnostic = error.stderr.decode(errors='replace').strip()
        if not diagnostic:
            diagnostic = (
                f'git {" ".join(arguments)} failed with exit code {error.returncode}'
            )
        raise GitCommandError(diagnostic) from None
    return completed.stdout


def _git(repository: Path, *arguments: str) -> str:
    """Run a read-only Git command and return stripped text output."""

    return _git_bytes(repository, *arguments).decode(errors='replace').strip()


def _working_tree_digest(repository: Path, base_sha: str) -> str | None:
    """Return a stable digest for tracked and untracked changes from a base."""

    tracked_diff = _git_bytes(
        repository, 'diff', '--binary', '--full-index', base_sha, '--'
    )
    untracked_output = _git_bytes(
        repository, 'ls-files', '--others', '--exclude-standard', '-z'
    )
    untracked_paths = sorted(path for path in untracked_output.split(b'\0') if path)
    if not tracked_diff and not untracked_paths:
        return None

    digest = hashlib.sha256()
    digest.update(b'tracked\0')
    digest.update(tracked_diff)
    for raw_path in untracked_paths:
        path = repository / os.fsdecode(raw_path)
        digest.update(b'untracked\0')
        digest.update(raw_path)
        digest.update(b'\0')
        if path.is_symlink():
            digest.update(b'symlink\0')
            digest.update(os.fsencode(path.readlink()))
        else:
            digest.update(b'file\0')
            digest.update((path.stat().st_mode & 0o111).to_bytes(2))
            with path.open('rb') as file:
                while chunk := file.read(HASH_CHUNK_SIZE):
                    digest.update(chunk)
    return f'sha256:{digest.hexdigest()}'


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""

    parser = argparse.ArgumentParser(prog='agent-orchestra')
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    commands = parser.add_subparsers(dest='command', required=True)

    commands.add_parser('init', help='initialize the local state database')

    enqueue = commands.add_parser(
        'enqueue-local', help='enqueue the current local changes'
    )
    enqueue.add_argument('repository', nargs='?', type=Path, default=Path.cwd())
    enqueue.add_argument('--base', default='HEAD')

    enqueue_many = commands.add_parser(
        'enqueue-locals',
        help='enqueue local changes from immediate child Git repositories',
    )
    enqueue_many.add_argument('directory', type=Path)
    enqueue_many.add_argument('--base', default='HEAD')

    status = commands.add_parser('status', help='show one run or list all runs')
    status.add_argument('run_id', nargs='?')

    run = commands.add_parser('run', help='review one queued local run')
    run.add_argument('run_id')
    run.add_argument('--objective', required=True)
    run.add_argument('--timeout', type=int, default=1800)
    run.add_argument('--codex-model')
    run.add_argument(
        '--runs-directory',
        type=Path,
        default=Path('~/.local/state/agent-orchestra/runs'),
    )
    run.set_defaults(reviewer_command=())

    skills = commands.add_parser('skills', help='manage bundled agent skills')
    skill_commands = skills.add_subparsers(dest='skill_command', required=True)
    install = skill_commands.add_parser(
        'install', help='install skills for supported local agent runtimes'
    )
    install.add_argument(
        '--agent', choices=('codex', 'claude-code', 'all'), default='all'
    )
    install.add_argument('--skill', action='append', required=True)
    install.add_argument('--source', type=Path)
    install.add_argument('--codex-home', type=Path)
    install.add_argument('--claude-home', type=Path)
    return parser


def _capture_local_run(repository: Path, base: str) -> Run | None:
    """Capture a local-changes run without persisting it."""

    base_sha = _git(repository, 'rev-parse', '--verify', base)
    head_sha = _git(repository, 'rev-parse', '--verify', 'HEAD')
    diff_digest = _working_tree_digest(repository, base_sha)
    if diff_digest is None:
        return None
    return Run.create_local(repository, repository, base_sha, head_sha, diff_digest)


def _enqueue_local(args: argparse.Namespace, store: RunStore) -> int:
    """Enqueue local changes described by parsed CLI arguments."""

    repository = args.repository.resolve()
    try:
        run = _capture_local_run(repository, args.base)
    except (GitCommandError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    if run is None:
        print('error: no local changes to enqueue', file=sys.stderr)
        return 2
    store.initialize()
    store.add(run)
    print(run.id)
    return 0


def _enqueue_locals(args: argparse.Namespace, store: RunStore) -> int:
    """Enqueue changed Git repositories immediately below a directory."""

    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        print(f'error: directory not found: {directory}', file=sys.stderr)
        return 2

    repositories = sorted(
        (
            child.resolve()
            for child in directory.iterdir()
            if child.is_dir() and (child / '.git').exists()
        ),
        key=lambda path: path.name,
    )
    if not repositories:
        print(f'no Git repositories found in {directory}')
        return 0

    runs: list[Run] = []
    clean_count = 0
    failed_count = 0
    for repository in repositories:
        try:
            run = _capture_local_run(repository, args.base)
        except (GitCommandError, OSError) as error:
            print(f'error: {repository}: {error}', file=sys.stderr)
            failed_count += 1
            continue
        if run is None:
            clean_count += 1
        else:
            runs.append(run)

    if runs:
        store.initialize()
        for run in runs:
            store.add(run)
            print(f'{run.id}  {run.worktree_path}')
    repository_word = 'repository' if len(runs) == 1 else 'repositories'
    clean_word = 'repository' if clean_count == 1 else 'repositories'
    failed_word = 'repository' if failed_count == 1 else 'repositories'
    print(
        f'enqueued {len(runs)} {repository_word}; '
        f'skipped {clean_count} clean {clean_word}; '
        f'failed {failed_count} {failed_word}'
    )
    return 2 if not runs and failed_count else 0


def _status(args: argparse.Namespace, store: RunStore) -> int:
    """Write persisted run status as versioned JSON without creating state."""

    if not args.database.is_file():
        print(f'state database not found: {args.database}', file=sys.stderr)
        return 2
    try:
        runs = (store.get(args.run_id),) if args.run_id else store.list_runs()
    except RunNotFoundError as error:
        print(f'run not found: {error}', file=sys.stderr)
        return 2
    document = {
        'schema_version': 1,
        'runs': [
            {
                'id': str(run.id),
                'scenario': str(run.scenario),
                'repository_path': str(run.repository_path),
                'worktree_path': str(run.worktree_path),
                'state': str(run.state),
                'base_sha': run.base_sha,
                'head_sha': run.head_sha,
                'diff_digest': run.diff_digest,
                'iteration': run.iteration,
                'remote_url': run.remote_url,
                'created_at': run.created_at.astimezone(UTC)
                .isoformat()
                .replace('+00:00', 'Z'),
                'updated_at': run.updated_at.astimezone(UTC)
                .isoformat()
                .replace('+00:00', 'Z'),
            }
            for run in runs
        ],
    }
    print(json.dumps(document, indent=2))
    return 0


def _run(args: argparse.Namespace, store: RunStore) -> int:
    """Consume one queued local run through its first review decision."""

    if not args.database.is_file():
        print(f'state database not found: {args.database}', file=sys.stderr)
        return 2
    if args.reviewer_command and args.codex_model:
        print(
            'error: --codex-model cannot be combined with a custom reviewer command',
            file=sys.stderr,
        )
        return 2
    try:
        run = store.get(args.run_id)
        _require_external_database(args.database, run.worktree_path)
        if args.reviewer_command:
            reviewer_command = args.reviewer_command
        else:
            reviewer_command = [
                sys.executable,
                '-m',
                'agent_orchestra.codex_reviewer',
            ]
            if args.codex_model:
                reviewer_command.extend(['--model', args.codex_model])
        result = run_queued_review(
            store=store,
            run=run,
            objective=args.objective,
            reviewer_command=reviewer_command,
            runs_directory=args.runs_directory,
            timeout_seconds=args.timeout,
            digest_worktree=_working_tree_digest,
        )
    except (OSError, RunNotFoundError, WorkerError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(
        json.dumps(
            {'schema_version': 1, 'run_id': str(result.id), 'state': result.state},
            indent=2,
        )
    )
    return 0


def _install_skills(args: argparse.Namespace) -> int:
    """Install requested bundled skills for one or both agent runtimes."""

    agents = (
        (AgentTarget.CODEX, AgentTarget.CLAUDE_CODE)
        if args.agent == 'all'
        else (AgentTarget(args.agent),)
    )
    skill_names = tuple(dict.fromkeys(args.skill))
    try:
        results = install_skills(
            skill_names,
            agents,
            source_root=args.source,
            codex_home=args.codex_home,
            claude_home=args.claude_home,
        )
    except (SkillInstallError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    for result in results:
        status = 'installed' if result.installed else 'already installed'
        print(f'{status} {result.skill} for {result.agent}: {result.destination}')
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""

    arguments = list(argv) if argv is not None else sys.argv[1:]
    reviewer_command: list[str] = []
    if 'run' in arguments and '--' in arguments:
        separator = arguments.index('--')
        reviewer_command = arguments[separator + 1 :]
        arguments = arguments[:separator]
    args = build_parser().parse_args(arguments)
    if args.command == 'run':
        args.reviewer_command = reviewer_command
    store = RunStore(args.database)

    if args.command == 'init':
        store.initialize()
        print(f'initialized {args.database}')
        return 0
    if args.command == 'enqueue-local':
        return _enqueue_local(args, store)
    if args.command == 'enqueue-locals':
        return _enqueue_locals(args, store)
    if args.command == 'status':
        return _status(args, store)
    if args.command == 'run':
        return _run(args, store)
    if args.command == 'skills' and args.skill_command == 'install':
        return _install_skills(args)

    raise AssertionError(f'unhandled command: {args.command}')


if __name__ == '__main__':
    raise SystemExit(main())
