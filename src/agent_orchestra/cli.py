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
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING

from agent_orchestra.invocations import (
    InvocationEvidenceError,
    InvocationIdentity,
    legacy_log_groups,
    read_records,
)
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
CLI_SCHEMA_VERSION = 3
HASH_CHUNK_SIZE = 1024 * 1024
STATE_DATABASE_INSIDE_WORKTREE = 'state database must be outside the worktree'


def _distribution_version() -> str:
    """Return the installed distribution version or a source-tree fallback."""

    try:
        return version('agent-orchestra')
    except PackageNotFoundError:
        return '0+unknown'


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
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {_distribution_version()}',
    )
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

    logs = commands.add_parser('logs', help='show process logs for one run')
    logs.add_argument('run_id')
    logs.add_argument('--iteration', type=int)
    logs.add_argument('--role', choices=('developer', 'reviewer'))
    logs.add_argument('--invocation')
    logs.add_argument('--runtime')
    logs.add_argument('--stream', choices=('stdout', 'stderr'))
    logs.add_argument(
        '--runs-directory',
        type=Path,
        default=Path('~/.local/state/agent-orchestra/runs'),
    )

    run = commands.add_parser('run', help='run a bounded review-remediation loop')
    run.add_argument('run_id')
    run.add_argument('--objective', required=True)
    run.add_argument('--timeout', type=int, default=1800)
    run.add_argument('--developer-timeout', type=int, default=1800)
    run.add_argument('--max-iterations', type=int, default=3)
    run.add_argument(
        '--developer-agent', choices=('codex', 'claude-code'), default='codex'
    )
    run.add_argument('--developer-model')
    run.add_argument(
        '--reviewer-agent', choices=('codex', 'claude-code'), default='codex'
    )
    run.add_argument('--reviewer-model')
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
    """Enqueue changed child repos and write one versioned JSON result."""

    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        print(
            json.dumps(
                {
                    'schema_version': CLI_SCHEMA_VERSION,
                    'directory': str(directory),
                    'runs': [],
                    'summary': {'enqueued': 0, 'clean': 0, 'failed': 0},
                    'failures': [],
                    'error': {
                        'code': 'directory_not_found',
                        'message': f'directory not found: {directory}',
                    },
                },
                indent=2,
            )
        )
        return 2

    repositories = sorted(
        (
            child.resolve()
            for child in directory.iterdir()
            if child.is_dir() and (child / '.git').exists()
        ),
        key=lambda path: path.name,
    )
    runs: list[Run] = []
    clean_count = 0
    failures: list[dict[str, str]] = []
    for repository in repositories:
        try:
            run = _capture_local_run(repository, args.base)
        except (GitCommandError, OSError) as error:
            failures.append({'repository_path': str(repository), 'message': str(error)})
            continue
        if run is None:
            clean_count += 1
        else:
            runs.append(run)

    if runs:
        store.initialize()
        for run in runs:
            store.add(run)
    print(
        json.dumps(
            {
                'schema_version': CLI_SCHEMA_VERSION,
                'directory': str(directory),
                'runs': [
                    {'id': str(run.id), 'worktree_path': str(run.worktree_path)}
                    for run in runs
                ],
                'summary': {
                    'enqueued': len(runs),
                    'clean': clean_count,
                    'failed': len(failures),
                },
                'failures': failures,
                'error': None,
            },
            indent=2,
        )
    )
    return 2 if not runs and failures else 0


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
        'schema_version': CLI_SCHEMA_VERSION,
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
    """Consume one queued local run through its bounded agent loop."""

    if not args.database.is_file():
        print(f'state database not found: {args.database}', file=sys.stderr)
        return 2
    if args.reviewer_command and (
        args.reviewer_model
        or args.reviewer_agent != 'codex'
        or args.developer_model
        or args.developer_agent != 'codex'
    ):
        print(
            'error: built-in reviewer options cannot be combined with a custom '
            'reviewer command',
            file=sys.stderr,
        )
        return 2
    try:
        run = store.get(args.run_id)
        _require_external_database(args.database, run.worktree_path)
        if args.reviewer_command:
            reviewer_command = args.reviewer_command
            reviewer_identity = InvocationIdentity(
                vendor='unknown', model=None, runtime='custom-command'
            )
        else:
            module = (
                'agent_orchestra.adapter.codex'
                if args.reviewer_agent == 'codex'
                else 'agent_orchestra.adapter.claude_code'
            )
            reviewer_command = [
                sys.executable,
                '-m',
                module,
            ]
            if args.reviewer_model:
                reviewer_command.extend(['--model', args.reviewer_model])
            reviewer_identity = InvocationIdentity(
                vendor='openai' if args.reviewer_agent == 'codex' else 'anthropic',
                model=args.reviewer_model,
                runtime=args.reviewer_agent,
            )
        developer_command: list[str] = []
        if not args.reviewer_command:
            developer_module = (
                'agent_orchestra.adapter.codex'
                if args.developer_agent == 'codex'
                else 'agent_orchestra.adapter.claude_code'
            )
            developer_command = [
                sys.executable,
                '-m',
                developer_module,
                '--role',
                'developer',
            ]
            if args.developer_model:
                developer_command.extend(['--model', args.developer_model])
        developer_identity = InvocationIdentity(
            vendor=('openai' if args.developer_agent == 'codex' else 'anthropic'),
            model=args.developer_model,
            runtime=args.developer_agent,
        )
        result = run_queued_review(
            store=store,
            run=run,
            objective=args.objective,
            reviewer_command=reviewer_command,
            developer_command=developer_command,
            runs_directory=args.runs_directory,
            timeout_seconds=args.timeout,
            developer_timeout_seconds=args.developer_timeout,
            max_iterations=args.max_iterations,
            digest_worktree=_working_tree_digest,
            reviewer_identity=reviewer_identity,
            developer_identity=developer_identity,
        )
    except (OSError, RunNotFoundError, WorkerError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                'schema_version': CLI_SCHEMA_VERSION,
                'run_id': str(result.id),
                'state': result.state,
            },
            indent=2,
        )
    )
    return 0


def _log_stream_document(
    *,
    role: str,
    vendor: str | None,
    model: str | None,
    runtime: str | None,
    iteration: int | None,
    invocation_id: str,
    stream: str,
    path: Path,
    started_at: str | None,
    finished_at: str | None,
    exit_code: int | None,
    timed_out: bool | None,
    interrupted: bool | None,
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Return one log stream document or a structured missing-file failure."""

    if not path.is_file():
        return None, {
            'code': 'missing_log',
            'stream': stream,
            'path': str(path),
            'message': f'missing {stream} log: {path}',
        }
    return (
        {
            'invocation_id': invocation_id,
            'role': role,
            'agent_vendor': vendor,
            'agent_model': model,
            'runtime': runtime,
            'iteration': iteration,
            'started_at': started_at,
            'finished_at': finished_at,
            'exit_code': exit_code,
            'timed_out': timed_out,
            'interrupted': interrupted,
            'stream': stream,
            'path': str(path),
            'content': path.read_text(encoding='utf-8', errors='replace'),
            'legacy': iteration is None,
        },
        None,
    )


def _write_logs_document(
    run_id: str,
    *,
    streams: list[dict[str, object]] | None = None,
    failures: list[dict[str, object]] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Write one versioned JSON logs result to standard output."""

    print(
        json.dumps(
            {
                'schema_version': CLI_SCHEMA_VERSION,
                'run_id': run_id,
                'streams': streams or [],
                'failures': failures or [],
                'error': (
                    {'code': error_code, 'message': error_message}
                    if error_code is not None
                    else None
                ),
            },
            indent=2,
        )
    )


def _logs(args: argparse.Namespace, store: RunStore) -> int:  # noqa: PLR0911
    """Write process logs as JSON without initializing or modifying run state."""

    if not args.database.is_file():
        _write_logs_document(
            args.run_id,
            error_code='state_database_not_found',
            error_message=f'state database not found: {args.database}',
        )
        return 2
    try:
        store.get(args.run_id)
    except RunNotFoundError as error:
        _write_logs_document(
            args.run_id,
            error_code='run_not_found',
            error_message=f'run not found: {error}',
        )
        return 2
    root = args.runs_directory.expanduser().resolve()
    run_directory = root / args.run_id
    if run_directory.is_symlink() or not run_directory.resolve().is_relative_to(root):
        _write_logs_document(
            args.run_id,
            error_code='run_evidence_escape',
            error_message='run evidence escapes the configured runs directory',
        )
        return 2
    if not run_directory.is_dir():
        _write_logs_document(
            args.run_id,
            error_code='run_evidence_not_found',
            error_message=f'run evidence not found: {run_directory}',
        )
        return 2
    requested_streams = (args.stream,) if args.stream else ('stdout', 'stderr')
    stream_documents: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    try:
        records = read_records(run_directory, args.run_id)
        if records:
            for record in records:
                if args.iteration is not None and record.iteration != args.iteration:
                    continue
                if args.role is not None and record.role != args.role:
                    continue
                if (
                    args.invocation is not None
                    and record.invocation_id != args.invocation
                ):
                    continue
                if args.runtime is not None and record.runtime != args.runtime:
                    continue
                for stream in requested_streams:
                    path = Path(
                        record.stdout_path if stream == 'stdout' else record.stderr_path
                    )
                    document, failure = _log_stream_document(
                        role=record.role,
                        vendor=record.agent_vendor,
                        model=record.agent_model,
                        runtime=record.runtime,
                        iteration=record.iteration,
                        invocation_id=record.invocation_id,
                        stream=stream,
                        path=path,
                        started_at=record.started_at,
                        finished_at=record.finished_at,
                        exit_code=record.exit_code,
                        timed_out=record.timed_out,
                        interrupted=record.interrupted,
                    )
                    if document is not None:
                        stream_documents.append(document)
                    if failure is not None:
                        failures.append(failure)
        else:
            if (
                args.iteration is not None
                or args.invocation is not None
                or args.runtime is not None
            ):
                _write_logs_document(
                    args.run_id,
                    error_code='legacy_metadata_unavailable',
                    error_message=(
                        'legacy logs do not contain iteration, invocation, or runtime metadata'
                    ),
                )
                return 2
            for sequence, role, stdout_path, stderr_path in legacy_log_groups(
                run_directory
            ):
                if args.role is not None and role != args.role:
                    continue
                for stream in requested_streams:
                    path = stdout_path if stream == 'stdout' else stderr_path
                    document, failure = _log_stream_document(
                        role=role,
                        vendor=None,
                        model=None,
                        runtime=None,
                        iteration=None,
                        invocation_id=f'legacy-{sequence}-{role}',
                        stream=stream,
                        path=path,
                        started_at=None,
                        finished_at=None,
                        exit_code=None,
                        timed_out=None,
                        interrupted=None,
                    )
                    if document is not None:
                        stream_documents.append(document)
                    if failure is not None:
                        failures.append(failure)
    except (InvocationEvidenceError, OSError) as error:
        _write_logs_document(
            args.run_id,
            error_code='invalid_evidence',
            error_message=str(error),
        )
        return 2
    if not stream_documents and not failures:
        _write_logs_document(
            args.run_id,
            error_code='no_matching_logs',
            error_message='no matching logs found',
        )
        return 2
    _write_logs_document(
        args.run_id,
        streams=stream_documents,
        failures=failures,
    )
    return 2 if failures else 0


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


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911
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
    if args.command == 'logs':
        return _logs(args, store)
    if args.command == 'run':
        return _run(args, store)
    if args.command == 'skills' and args.skill_command == 'install':
        return _install_skills(args)

    raise AssertionError(f'unhandled command: {args.command}')


if __name__ == '__main__':
    raise SystemExit(main())
