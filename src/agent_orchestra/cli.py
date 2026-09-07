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
    InvocationRecord,
    derive_task_status,
    read_records,
)
from agent_orchestra.models import Run, RunState
from agent_orchestra.skill_install import (
    AgentTarget,
    SkillInstallError,
    install_skills,
)
from agent_orchestra.store import ConcurrentUpdateError, RunNotFoundError, RunStore
from agent_orchestra.worker import WorkerError, resume_review, run_queued_review

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_DATABASE = Path.home() / '.local/state/agent-orchestra/state.db'
DEFAULT_RUNS_DIRECTORY = Path.home() / '.local/state/agent-orchestra/runs'
CLI_SCHEMA_VERSION = 8
HASH_CHUNK_SIZE = 1024 * 1024
STATE_DATABASE_INSIDE_WORKTREE = 'state database must be outside the worktree'
PUBLIC_WORKER_ERROR_CODES = {'run_not_resumable': 'job_not_resumable'}


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


def _git_bytes(repo: Path, *arguments: str) -> bytes:
    """Run a read-only Git command and return its raw output."""

    git = shutil.which('git')
    if git is None:
        message = 'git executable not found'
        raise GitCommandError(message)
    try:
        completed = subprocess.run(
            [git, '-C', str(repo), *arguments],
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


def _git(repo: Path, *arguments: str) -> str:
    """Run a read-only Git command and return stripped text output."""

    return _git_bytes(repo, *arguments).decode(errors='replace').strip()


def _git_locations(repo: Path) -> tuple[Path, Path]:
    """Return the primary repository location and selected worktree root."""

    worktree_path = Path(
        _git(repo, 'rev-parse', '--path-format=absolute', '--show-toplevel')
    ).resolve()
    git_directory = Path(_git(repo, 'rev-parse', '--absolute-git-dir')).resolve()
    common_directory = Path(
        _git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    ).resolve()
    output = _git_bytes(repo, 'worktree', 'list', '--porcelain', '-z')
    records = tuple(record for record in output.split(b'\0\0') if record)
    prefix = b'worktree '
    listed_paths: list[Path] = []
    for record in records:
        first_field = record.split(b'\0', 1)[0]
        if not first_field.startswith(prefix) or first_field == prefix:
            message = 'invalid git worktree list output'
            raise GitCommandError(message)
        listed_paths.append(
            Path(os.fsdecode(first_field.removeprefix(prefix))).resolve()
        )
    if not listed_paths:
        message = 'git worktree list returned no worktrees'
        raise GitCommandError(message)
    if git_directory == common_directory:
        return worktree_path, worktree_path
    if worktree_path not in listed_paths:
        message = 'selected worktree is missing from git worktree list'
        raise GitCommandError(message)
    return listed_paths[0], worktree_path


def _working_tree_digest(repo: Path, base_sha: str) -> str | None:
    """Return a stable digest for tracked and untracked changes from a base."""

    tracked_diff = _git_bytes(repo, 'diff', '--binary', '--full-index', base_sha, '--')
    untracked_output = _git_bytes(
        repo, 'ls-files', '--others', '--exclude-standard', '-z'
    )
    untracked_paths = sorted(path for path in untracked_output.split(b'\0') if path)
    if not tracked_diff and not untracked_paths:
        return None

    digest = hashlib.sha256()
    digest.update(b'tracked\0')
    digest.update(tracked_diff)
    for raw_path in untracked_paths:
        path = repo / os.fsdecode(raw_path)
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
    enqueue.add_argument(
        'repo', metavar='repository', nargs='?', type=Path, default=Path.cwd()
    )
    enqueue.add_argument('--base', default='HEAD')
    enqueue.add_argument('--supersedes', metavar='JOB_ID')

    enqueue_many = commands.add_parser(
        'enqueue-locals',
        help='enqueue local changes from immediate child Git repositories',
    )
    enqueue_many.add_argument('directory', type=Path)
    enqueue_many.add_argument('--base', default='HEAD')

    commands.add_parser('jobs', help='list stored jobs')

    job = commands.add_parser('job', help='show one stored job')
    job.add_argument('job_id')
    job.add_argument('--runs-directory', type=Path, default=DEFAULT_RUNS_DIRECTORY)

    tasks = commands.add_parser('tasks', help='show one job task history')
    tasks.add_argument('job_id')
    tasks.add_argument('--runs-directory', type=Path, default=DEFAULT_RUNS_DIRECTORY)

    task = commands.add_parser('task', help='show one task and its attempts')
    task.add_argument('task_id')
    task.add_argument('--runs-directory', type=Path, default=DEFAULT_RUNS_DIRECTORY)

    run = commands.add_parser('run', help='run a bounded review-remediation loop')
    run.add_argument('job_id')
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
        default=DEFAULT_RUNS_DIRECTORY,
    )
    run.set_defaults(reviewer_command=())

    resume = commands.add_parser('resume', help='resume one recoverable job')
    resume.add_argument('job_id')
    resume.add_argument(
        '--runs-directory',
        type=Path,
        default=DEFAULT_RUNS_DIRECTORY,
    )

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


def _capture_local_run(repo: Path, base: str) -> Run | None:
    """Capture a local-changes run without persisting it."""

    repo_path, worktree_path = _git_locations(repo)
    base_sha = _git(worktree_path, 'rev-parse', '--verify', base)
    head_sha = _git(worktree_path, 'rev-parse', '--verify', 'HEAD')
    diff_digest = _working_tree_digest(worktree_path, base_sha)
    if diff_digest is None:
        return None
    return Run.create_local(repo_path, worktree_path, base_sha, head_sha, diff_digest)


def _enqueue_local(  # noqa: PLR0911
    args: argparse.Namespace, store: RunStore
) -> int:
    """Enqueue local changes described by parsed CLI arguments."""

    repo = args.repo.resolve()
    try:
        run = _capture_local_run(repo, args.base)
    except (GitCommandError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    if run is None:
        print('error: no local changes to enqueue', file=sys.stderr)
        return 2
    if args.supersedes is not None:
        if not args.database.is_file():
            print(f'error: job not found: {args.supersedes}', file=sys.stderr)
            return 2
        try:
            predecessor = store.get(args.supersedes)
        except RunNotFoundError as error:
            print(f'error: job not found: {error}', file=sys.stderr)
            return 2
        if predecessor.state not in {RunState.FAILED, RunState.SUPERSEDED}:
            print(
                f'error: job {predecessor.id} is {predecessor.state}; resume '
                'recoverable jobs instead',
                file=sys.stderr,
            )
            return 2
        if (
            predecessor.repo_path != run.repo_path
            or predecessor.worktree_path != run.worktree_path
        ):
            print(
                'error: superseded job belongs to a different worktree', file=sys.stderr
            )
            return 2
        run = Run.create_local(
            run.repo_path,
            run.worktree_path,
            run.base_sha,
            run.head_sha,
            run.diff_digest or '',
            supersedes_run_id=str(predecessor.id),
        )
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
                    'jobs': [],
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

    repos = sorted(
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
    for repo in repos:
        try:
            run = _capture_local_run(repo, args.base)
        except (GitCommandError, OSError) as error:
            failures.append({'repository_path': str(repo), 'message': str(error)})
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
                'jobs': [
                    {'job_id': str(run.id), 'worktree_path': str(run.worktree_path)}
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


def _job_summary(run: Run) -> dict[str, object]:
    """Return one job using the public job vocabulary."""

    return {
        'job_id': str(run.id),
        'scenario': str(run.scenario),
        'repository_path': str(run.repo_path),
        'worktree_path': str(run.worktree_path),
        'state': str(run.state),
        'base_sha': run.base_sha,
        'head_sha': run.head_sha,
        'diff_digest': run.diff_digest,
        'iteration': run.iteration,
        'remote_url': run.remote_url,
        'supersedes_job_id': run.supersedes_run_id,
        'created_at': run.created_at.astimezone(UTC).isoformat().replace('+00:00', 'Z'),
        'updated_at': run.updated_at.astimezone(UTC).isoformat().replace('+00:00', 'Z'),
    }


def _stream_document(path_value: str, *, include_content: bool) -> dict[str, object]:
    """Describe one attempt stream, optionally reading its content."""

    path = Path(path_value)
    document: dict[str, object] = {
        'path': str(path),
        'available': path.is_file(),
    }
    if include_content:
        document['content'] = (
            path.read_text(encoding='utf-8', errors='replace')
            if path.is_file()
            else None
        )
    return document


def _attempt_document(
    record: InvocationRecord, *, include_stream_content: bool
) -> dict[str, object]:
    """Return one invocation record using the public attempt vocabulary."""

    return {
        'attempt_id': record.invocation_id,
        'attempt': record.attempt,
        'status': record.status,
        'conclusion': record.conclusion,
        'agent_vendor': record.agent_vendor,
        'requested_model': record.requested_model,
        'effective_models': list(record.effective_models),
        'effective_model_status': record.effective_model_status,
        'runtime': record.runtime,
        'started_at': record.started_at,
        'finished_at': record.finished_at,
        'response_received_at': record.response_received_at,
        'validation_started_at': record.validation_started_at,
        'exit_code': record.exit_code,
        'timed_out': record.timed_out,
        'interrupted': record.interrupted,
        'legacy': False,
        'streams': {
            'stdout': _stream_document(
                record.stdout_path, include_content=include_stream_content
            ),
            'stderr': _stream_document(
                record.stderr_path, include_content=include_stream_content
            ),
        },
    }


def _task_documents(
    records: tuple[InvocationRecord, ...], *, include_stream_content: bool
) -> list[dict[str, object]]:
    """Group validated attempt evidence into deterministic task history."""

    grouped: dict[str, list[InvocationRecord]] = {}
    for record in records:
        grouped.setdefault(record.task_id, []).append(record)
    documents: list[dict[str, object]] = []
    for task_id, attempts in sorted(grouped.items()):
        ordered = tuple(sorted(attempts, key=lambda item: item.attempt))
        latest = ordered[-1]
        documents.append(
            {
                'task_id': task_id,
                'job_id': latest.run_id,
                'role': latest.role,
                'iteration': latest.iteration,
                'status': str(derive_task_status(ordered)),
                'conclusion': latest.conclusion,
                'attempt': latest.attempt,
                'attempts': [
                    _attempt_document(
                        record, include_stream_content=include_stream_content
                    )
                    for record in ordered
                ],
            }
        )
    return documents


def _job_directory(job_id: str, runs_directory: Path) -> Path | None:
    """Resolve one contained job evidence directory when it exists."""

    root = runs_directory.expanduser().resolve()
    job_directory = root / job_id
    if job_directory.is_symlink() or not job_directory.resolve().is_relative_to(root):
        message = 'job directory escapes the runs directory'
        raise InvocationEvidenceError(message)
    if not job_directory.is_dir():
        return None
    return job_directory


def _job_tasks(
    job_id: str, runs_directory: Path, *, include_stream_content: bool
) -> list[dict[str, object]]:
    """Read task evidence for one job."""

    job_directory = _job_directory(job_id, runs_directory)
    if job_directory is None:
        return []
    return _task_documents(
        read_records(job_directory, job_id),
        include_stream_content=include_stream_content,
    )


def _write_job_error(
    code: str,
    message: str,
    *,
    job_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Write a versioned error for a public job or task query."""

    document: dict[str, object] = {'schema_version': CLI_SCHEMA_VERSION}
    if job_id is not None:
        document['job_id'] = job_id
    if task_id is not None:
        document['task_id'] = task_id
    document['error'] = {'code': code, 'message': message}
    print(json.dumps(document, indent=2))


def _jobs(args: argparse.Namespace, store: RunStore) -> int:
    """List stored jobs without reading mutable workflow state."""

    if not args.database.is_file():
        _write_job_error(
            'state_database_not_found',
            f'state database not found: {args.database}',
        )
        return 2
    print(
        json.dumps(
            {
                'schema_version': CLI_SCHEMA_VERSION,
                'jobs': [_job_summary(run) for run in store.list_runs()],
                'error': None,
            },
            indent=2,
        )
    )
    return 0


def _selected_job(
    args: argparse.Namespace,
    store: RunStore,
    *,
    include_stream_content: bool,
) -> tuple[Run, list[dict[str, object]]] | None:
    """Resolve one job and its task history, reporting stable query errors."""

    if not args.database.is_file():
        _write_job_error(
            'state_database_not_found',
            f'state database not found: {args.database}',
            job_id=args.job_id,
        )
        return None
    try:
        run = store.get(args.job_id)
        tasks = _job_tasks(
            str(run.id),
            args.runs_directory,
            include_stream_content=include_stream_content,
        )
    except RunNotFoundError as error:
        _write_job_error('job_not_found', f'job not found: {error}', job_id=args.job_id)
        return None
    except (InvocationEvidenceError, OSError) as error:
        _write_job_error('invalid_evidence', str(error), job_id=args.job_id)
        return None
    return run, tasks


def _job(args: argparse.Namespace, store: RunStore) -> int:
    """Show one job and all currently non-terminal tasks."""

    selected = _selected_job(args, store, include_stream_content=False)
    if selected is None:
        return 2
    run, tasks = selected
    document = _job_summary(run)
    document['current'] = [
        {
            'task_id': task['task_id'],
            'role': task['role'],
            'attempt': task['attempt'],
            'status': task['status'],
            'conclusion': task['conclusion'],
        }
        for task in tasks
        if task['status'] in {'pending', 'running'}
    ]
    print(
        json.dumps(
            {'schema_version': CLI_SCHEMA_VERSION, 'job': document, 'error': None},
            indent=2,
        )
    )
    return 0


def _tasks(args: argparse.Namespace, store: RunStore) -> int:
    """Show the complete durable task history for one job."""

    selected = _selected_job(args, store, include_stream_content=True)
    if selected is None:
        return 2
    run, tasks = selected
    print(
        json.dumps(
            {
                'schema_version': CLI_SCHEMA_VERSION,
                'job_id': str(run.id),
                'tasks': tasks,
                'error': None,
            },
            indent=2,
        )
    )
    return 0


def _task(args: argparse.Namespace, store: RunStore) -> int:
    """Show one task addressed by its globally unique durable identifier."""

    separator = args.task_id.rfind(':')
    if separator < 1:
        _write_job_error(
            'invalid_task_id',
            'task ID must contain its job ID',
            task_id=args.task_id,
        )
        return 2
    args.job_id = args.task_id[:separator]
    selected = _selected_job(args, store, include_stream_content=True)
    if selected is None:
        return 2
    _, tasks = selected
    matching = [task for task in tasks if task['task_id'] == args.task_id]
    if not matching:
        _write_job_error(
            'task_not_found',
            f'task not found: {args.task_id}',
            job_id=args.job_id,
            task_id=args.task_id,
        )
        return 2
    print(
        json.dumps(
            {'schema_version': CLI_SCHEMA_VERSION, 'task': matching[0], 'error': None},
            indent=2,
        )
    )
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
        run = store.get(args.job_id)
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
                'job_id': str(result.id),
                'state': result.state,
                'error': None,
            },
            indent=2,
        )
    )
    return 0


def _write_resume_document(
    job_id: str,
    *,
    state: RunState | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Write one versioned resume result to standard output."""

    print(
        json.dumps(
            {
                'schema_version': CLI_SCHEMA_VERSION,
                'job_id': job_id,
                'state': state,
                'error': (
                    {'code': error_code, 'message': error_message}
                    if error_code is not None
                    else None
                ),
            },
            indent=2,
        )
    )


def _resume(args: argparse.Namespace, store: RunStore) -> int:
    """Resume a recoverable run using its durable execution context."""

    if not args.database.is_file():
        _write_resume_document(
            args.job_id,
            error_code='state_database_not_found',
            error_message=f'state database not found: {args.database}',
        )
        return 2
    try:
        run = store.get(args.job_id)
        _require_external_database(args.database, run.worktree_path)
        result = resume_review(
            store=store,
            run=run,
            runs_directory=args.runs_directory,
            digest_worktree=_working_tree_digest,
        )
    except RunNotFoundError as error:
        _write_resume_document(
            args.job_id,
            error_code='job_not_found',
            error_message=f'job not found: {error}',
        )
        return 2
    except ConcurrentUpdateError as error:
        _write_resume_document(
            args.job_id,
            error_code='concurrent_update',
            error_message=f'job changed concurrently: {error}',
        )
        return 2
    except (OSError, WorkerError) as error:
        message = str(error)
        code = error.code if isinstance(error, WorkerError) else None
        if code is not None:
            code = PUBLIC_WORKER_ERROR_CODES.get(code, code)
        _write_resume_document(
            args.job_id,
            error_code=code or 'resume_evidence_invalid',
            error_message=message,
        )
        return 2
    _write_resume_document(str(result.id), state=result.state)
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
    if args.command == 'jobs':
        return _jobs(args, store)
    if args.command == 'job':
        return _job(args, store)
    if args.command == 'tasks':
        return _tasks(args, store)
    if args.command == 'task':
        return _task(args, store)
    if args.command == 'run':
        return _run(args, store)
    if args.command == 'resume':
        return _resume(args, store)
    if args.command == 'skills' and args.skill_command == 'install':
        return _install_skills(args)

    raise AssertionError(f'unhandled command: {args.command}')


if __name__ == '__main__':
    raise SystemExit(main())
