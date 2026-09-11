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
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from agent_orchestra.adapter.issue_reviewer import IssueReviewerError
from agent_orchestra.adapter.registry import (
    DEFAULT_RUNTIME_REGISTRY,
    RuntimeRegistry,
    RuntimeRegistryError,
    RuntimeRole,
)
from agent_orchestra.attempt_documents import (
    CLI_ATTEMPT_HEAD_FIELDS,
    CLI_ATTEMPT_RENAMES,
    CLI_ATTEMPT_TAIL_FIELDS,
    project_attempt,
)
from agent_orchestra.audit import build_audit_document, is_known_temporary
from agent_orchestra.evidence import (
    EvidencePathError,
    WorkerError,
    read_json_object,
    resolve_evidence_path,
    reviewer_dispatch_path,
)
from agent_orchestra.execution_context import (
    WorkerContext,
)
from agent_orchestra.invocations import (
    InvocationEvidenceError,
    InvocationEvidenceStore,
    InvocationIdentity,
    InvocationRecord,
    derive_task_status,
)
from agent_orchestra.issue_review import (
    IssueReviewError,
    publish_issue_feedback,
    resume_issue_review,
    run_issue_review,
)
from agent_orchestra.issue_sources import IssueSourceError, fetch_issue, write_snapshot
from agent_orchestra.manifests import (
    ManifestError,
    canonical_evidence_type,
    evidence_ordinal,
    evidence_path,
    validate_packaged_manifests,
)
from agent_orchestra.messages import validate_review_response
from agent_orchestra.models import (
    HUMAN_ACTION_STATES,
    IssueJob,
    ProviderAction,
    Run,
    RunState,
)
from agent_orchestra.queued_review import (
    run_queued_review,
)
from agent_orchestra.retention import (
    RetentionError,
    apply_prune_plan,
    build_prune_plan,
    parse_duration,
    plan_document,
)
from agent_orchestra.reviewer_paths import reviewer_evidence_paths
from agent_orchestra.reviewer_plan import (
    ReviewerPlanError,
    build_reviewer_execution_plan,
    select_reviewer_set,
)
from agent_orchestra.schemas import REVIEWER_BATCH_RESULT_ADAPTER, SchemaValidationError
from agent_orchestra.settings import Settings, SettingsError, load_settings
from agent_orchestra.skill_install import SkillInstallError, install_skills
from agent_orchestra.store import (
    ConcurrentUpdateError,
    JobStore,
    PersistedEnumError,
    RunNotFoundError,
    UnreadableJob,
)
from agent_orchestra.worker import (
    resume_review,
    run_queued_reviewer_set,
)
from agent_orchestra.worktrees import WorktreeStatus, worktree_status

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

DEFAULT_DATABASE = Path.home() / '.local/state/agent-orchestra/state.db'
DEFAULT_RUNS_DIRECTORY = Path.home() / '.local/state/agent-orchestra/runs'
CLI_SCHEMA_VERSION = 22
HASH_CHUNK_SIZE = 1024 * 1024
STATE_DATABASE_INSIDE_WORKTREE = 'state database must be outside the worktree'


def _runtime_argument(
    registry: RuntimeRegistry,
    role: RuntimeRole | None = None,
    *,
    allow_all: bool = False,
) -> Callable[[str], str]:
    """Build an argparse converter with stable registry lookup errors."""

    def resolve(value: str) -> str:
        if allow_all and value == 'all':
            return value
        try:
            registry.require(value, role)
        except RuntimeRegistryError as error:
            raise argparse.ArgumentTypeError(str(error)) from error
        return value

    return resolve


def _skill_home_argument(
    registry: RuntimeRegistry,
) -> Callable[[str], tuple[str, Path]]:
    """Build an argparse converter for one registry-backed home override."""

    def resolve(value: str) -> tuple[str, Path]:
        runtime, separator, path = value.partition('=')
        if not separator or not path:
            message = 'expected RUNTIME=PATH'
            raise argparse.ArgumentTypeError(message)
        try:
            registry.require(runtime)
        except RuntimeRegistryError as error:
            raise argparse.ArgumentTypeError(str(error)) from error
        return runtime, Path(path)

    return resolve


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


def _add_intake_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    evidence: argparse.ArgumentParser,
    runtimes: RuntimeRegistry,
) -> None:
    """Register the commands that bring work into the system."""

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

    enqueue_issue = commands.add_parser(
        'enqueue-issue',
        help='capture one GitHub or GitLab issue for review',
        parents=[evidence],
    )
    enqueue_issue.add_argument('issue_url')

    review_issue = commands.add_parser(
        'review-issue',
        help='review a captured issue for implementation readiness',
        parents=[evidence],
    )
    review_issue.add_argument('job_id')
    review_issue.add_argument(
        '--objective', default='Review this issue for implementation readiness.'
    )
    review_issue.add_argument('--timeout', type=int, default=1800)
    review_issue.add_argument(
        '--reviewer-agent',
        type=_runtime_argument(runtimes, RuntimeRole.ISSUE_REVIEWER),
        choices=runtimes.identifiers(RuntimeRole.ISSUE_REVIEWER),
        default=runtimes.default(RuntimeRole.ISSUE_REVIEWER).identifier,
    )
    review_issue.add_argument('--reviewer-model')
    review_issue.set_defaults(reviewer_command=())

    publish_feedback = commands.add_parser(
        'post-issue-feedback',
        help='publish reviewed feedback to an issue provider',
        parents=[evidence],
    )
    publish_feedback.add_argument('job_id')
    publish_feedback.add_argument(
        '--authorize', action='store_true', help='authorize this provider write'
    )


def _add_query_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    evidence: argparse.ArgumentParser,
) -> None:
    """Register the read-only commands over stored jobs and evidence."""

    jobs = commands.add_parser('jobs', help='list stored jobs')
    jobs.add_argument(
        '--state',
        action='append',
        default=[],
        metavar='STATE',
        help='include jobs in this durable state; repeat to select more states',
    )

    cancel = commands.add_parser('cancel', help='cancel one unrunnable source-code job')
    cancel.add_argument('job_id')
    cancel.add_argument('--reason', required=True)
    jobs.add_argument(
        '--attention',
        action='store_true',
        help='include jobs in states that require human action',
    )

    job = commands.add_parser('job', help='show one stored job', parents=[evidence])
    job.add_argument('job_id')

    tasks = commands.add_parser(
        'tasks', help='show one job task history', parents=[evidence]
    )
    tasks.add_argument('job_id')

    task = commands.add_parser(
        'task', help='show one task and its attempts', parents=[evidence]
    )
    task.add_argument('task_id')

    audit = commands.add_parser(
        'audit', help='audit one job and its durable evidence', parents=[evidence]
    )
    audit.add_argument('job_id')
    audit.add_argument('--verify', action='store_true')


def _add_execution_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    evidence: argparse.ArgumentParser,
    runtimes: RuntimeRegistry,
    effective: Settings,
) -> None:
    """Register the commands that run or resume bounded review work."""

    run = commands.add_parser(
        'run', help='run a bounded review-remediation loop', parents=[evidence]
    )
    run.add_argument('job_id')
    run.add_argument('--objective', required=True)
    run.add_argument('--timeout', type=int, default=1800)
    run.add_argument('--developer-timeout', type=int, default=1800)
    run.add_argument('--max-iterations', type=int, default=3)
    run.add_argument(
        '--developer-agent',
        type=_runtime_argument(runtimes, RuntimeRole.DEVELOPER),
        choices=runtimes.identifiers(RuntimeRole.DEVELOPER),
        default=runtimes.default(RuntimeRole.DEVELOPER).identifier,
    )
    run.add_argument('--developer-model')
    run.add_argument(
        '--reviewer-agent',
        type=_runtime_argument(runtimes, RuntimeRole.REVIEWER),
        choices=runtimes.identifiers(RuntimeRole.REVIEWER),
        default=runtimes.default(RuntimeRole.REVIEWER).identifier,
    )
    run.add_argument('--reviewer-model')
    run.add_argument(
        '--reviewer-set',
        choices=tuple(item.identifier for item in effective.reviewer_sets),
        help='run every required reviewer in this configured reviewer set',
    )
    run.set_defaults(settings=effective)
    run.set_defaults(reviewer_command=())

    resume = commands.add_parser(
        'resume', help='resume one recoverable job', parents=[evidence]
    )
    resume.add_argument('job_id')


def _add_administration_commands(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    evidence: argparse.ArgumentParser,
    runtimes: RuntimeRegistry,
) -> None:
    """Register the commands that manage settings, retention, and skills."""

    config = commands.add_parser('config', help='show effective global settings')
    config_commands = config.add_subparsers(dest='config_command', required=True)
    config_commands.add_parser(
        'show', help='show effective settings', parents=[evidence]
    )

    prune = commands.add_parser(
        'prune', help='preview or apply evidence retention', parents=[evidence]
    )
    prune.add_argument('--older-than')
    prune.add_argument('--orphans', action='store_true')
    prune.add_argument('--apply', action='store_true')
    prune.add_argument('--delete-database-records', action='store_true')

    skills = commands.add_parser('skills', help='manage bundled agent skills')
    skill_commands = skills.add_subparsers(dest='skill_command', required=True)
    install = skill_commands.add_parser(
        'install', help='install skills for supported local agent runtimes'
    )
    install.add_argument(
        '--agent',
        type=_runtime_argument(runtimes, allow_all=True),
        choices=(*runtimes.identifiers(), 'all'),
        default='all',
    )
    install.add_argument('--skill', action='append', required=True)
    install.add_argument('--source', type=Path)
    install.add_argument(
        '--skill-home',
        action='append',
        default=[],
        type=_skill_home_argument(runtimes),
        metavar='RUNTIME=PATH',
        help='override one registered runtime skill root; repeat as needed',
    )


def build_parser(
    settings: Settings | None = None,
    runtime_registry: RuntimeRegistry | None = None,
) -> argparse.ArgumentParser:
    """Build the CLI argument parser."""

    runtimes = runtime_registry or DEFAULT_RUNTIME_REGISTRY
    effective = settings or load_settings(
        default_database=DEFAULT_DATABASE,
        default_runs_directory=DEFAULT_RUNS_DIRECTORY,
        runtime_registry=runtimes,
    )
    runs_default = cast('Path', effective.runs_directory.value)
    parser = argparse.ArgumentParser(prog='agent-orchestra')
    parser.set_defaults(runtime_registry=runtimes)
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {_distribution_version()}',
    )
    parser.add_argument('--database', type=Path, default=effective.database.value)
    evidence = argparse.ArgumentParser(add_help=False)
    evidence.add_argument('--runs-directory', type=Path, default=runs_default)
    commands = parser.add_subparsers(dest='command', required=True)

    _add_intake_commands(commands, evidence, runtimes)
    _add_query_commands(commands, evidence)
    _add_execution_commands(commands, evidence, runtimes, effective)
    _add_administration_commands(commands, evidence, runtimes)
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
    args: argparse.Namespace, store: JobStore
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


def _enqueue_locals(args: argparse.Namespace, store: JobStore) -> int:
    """Enqueue changed child repos and write one versioned JSON result."""

    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        _write_document(
            {
                'directory': str(directory),
                'jobs': [],
                'summary': {'enqueued': 0, 'clean': 0, 'failed': 0},
                'failures': [],
            },
            error={
                'code': 'directory_not_found',
                'message': f'directory not found: {directory}',
            },
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
    _write_document(
        {
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
        }
    )
    return 2 if not runs and failures else 0


def _enqueue_issue(args: argparse.Namespace, store: JobStore) -> int:
    """Capture one immutable provider issue revision as a queued job."""

    try:
        snapshot = fetch_issue(args.issue_url)
        job = IssueJob.create(
            provider=snapshot.locator.provider,
            host=snapshot.locator.host,
            remote_url=snapshot.locator.url,
            namespace=snapshot.locator.namespace,
            project=snapshot.locator.project,
            issue_number=snapshot.locator.number,
            title=snapshot.title,
            author=snapshot.author,
            source_updated_at=snapshot.updated_at,
            source_digest=snapshot.digest,
        )
        root = args.runs_directory.expanduser().resolve()
        snapshot_path = resolve_evidence_path(
            root, job.id, *Path(evidence_path('issue_snapshot')).parts
        )
        write_snapshot(root, job.id, snapshot_path, snapshot)
        store.initialize()
        store.add_issue(job)
    except (EvidencePathError, IssueSourceError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    print(job.id)
    return 0


def _review_issue(args: argparse.Namespace, store: JobStore) -> int:
    """Run one read-only issue-readiness review."""

    if args.timeout <= 0:
        print('error: timeout must be positive', file=sys.stderr)
        return 2
    try:
        job = store.get_issue(args.job_id)
        finished = run_issue_review(
            job,
            store,
            args.runs_directory,
            objective=args.objective,
            agent=args.reviewer_agent,
            model=args.reviewer_model,
            timeout=args.timeout,
            command=tuple(args.reviewer_command),
            registry=args.runtime_registry,
        )
    except (
        RunNotFoundError,
        IssueReviewError,
        IssueReviewerError,
        SchemaValidationError,
        InvocationEvidenceError,
        OSError,
    ) as error:
        code, message = _issue_review_error(error)
        _write_job_error(code, message, job_id=args.job_id)
        return 2
    _write_document(_issue_job_summary(finished))
    return 0


def _post_issue_feedback(args: argparse.Namespace, store: JobStore) -> int:
    """Publish one reviewed feedback artifact after explicit authorization."""

    if not args.authorize:
        print('error: --authorize is required for provider writes', file=sys.stderr)
        return 2
    try:
        job = store.get_issue(args.job_id)
        action = publish_issue_feedback(job, store, args.runs_directory)
    except RunNotFoundError as error:
        _write_job_error('job_not_found', f'job not found: {error}', job_id=args.job_id)
        return 2
    except (IssueReviewError, OSError) as error:
        _write_job_error('issue_feedback_failed', str(error), job_id=args.job_id)
        return 2
    _write_document(
        {
            'job_id': action.job_id,
            'iteration': action.iteration,
            'action': action.action,
            'provider_id': action.provider_id,
            'remote_url': action.remote_url,
            'created_at': action.created_at.astimezone(UTC)
            .isoformat()
            .replace('+00:00', 'Z'),
        }
    )
    return 0


def _job_summary(run: Run) -> dict[str, object]:
    """Return one job using the public job vocabulary."""

    return {
        'job_id': str(run.id),
        'scenario': str(run.scenario),
        'repository_path': str(run.repo_path),
        'worktree_path': str(run.worktree_path),
        'worktree_status': str(worktree_status(run.worktree_path)),
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


def _provider_action_summary(action: ProviderAction) -> dict[str, object]:
    """Return one durable provider action using public field names."""

    return {
        'iteration': action.iteration,
        'action': action.action,
        'provider_id': action.provider_id,
        'remote_url': action.remote_url,
        'created_at': action.created_at.astimezone(UTC)
        .isoformat()
        .replace('+00:00', 'Z'),
    }


def _issue_job_summary(
    job: IssueJob, actions: tuple[ProviderAction, ...] = ()
) -> dict[str, object]:
    """Return one issue-review job using the public job vocabulary."""

    return {
        'job_id': job.id,
        'scenario': 'issue_review',
        'state': str(job.state),
        'provider': job.provider,
        'host': job.host,
        'remote_url': job.remote_url,
        'namespace': job.namespace,
        'project': job.project,
        'issue_number': job.issue_number,
        'title': job.title,
        'author': job.author,
        'source_updated_at': job.source_updated_at,
        'source_digest': job.source_digest,
        'iteration': job.iteration,
        'provider_actions': [_provider_action_summary(action) for action in actions],
        'created_at': job.created_at.astimezone(UTC).isoformat().replace('+00:00', 'Z'),
        'updated_at': job.updated_at.astimezone(UTC).isoformat().replace('+00:00', 'Z'),
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

    document = project_attempt(
        record, CLI_ATTEMPT_HEAD_FIELDS, renames=CLI_ATTEMPT_RENAMES
    )
    document['legacy'] = False
    document['streams'] = {
        'stdout': _stream_document(
            record.stdout_path, include_content=include_stream_content
        ),
        'stderr': _stream_document(
            record.stderr_path, include_content=include_stream_content
        ),
    }
    document.update(
        project_attempt(record, CLI_ATTEMPT_TAIL_FIELDS, renames=CLI_ATTEMPT_RENAMES)
    )
    return document


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
        document: dict[str, object] = {
            'task_id': task_id,
            'job_id': latest.run_id,
            'role': latest.role,
            'iteration': latest.iteration,
            'status': str(derive_task_status(ordered)),
            'conclusion': latest.conclusion,
            'attempt': latest.attempt,
            'attempts': [
                _attempt_document(record, include_stream_content=include_stream_content)
                for record in ordered
            ],
        }
        if latest.reviewer_id is not None:
            document['reviewer_id'] = latest.reviewer_id
        documents.append(document)
    return documents


def _job_directory(job_id: str, runs_directory: Path) -> Path | None:
    """Resolve one contained job evidence directory when it exists."""

    root = runs_directory.expanduser().resolve()
    try:
        job_directory = resolve_evidence_path(root, job_id)
    except EvidencePathError as error:
        raise InvocationEvidenceError(str(error)) from error
    if not job_directory.is_dir():
        return None
    return job_directory


def _required_job_directory(job_id: str, runs_directory: Path) -> Path:
    """Resolve one contained job evidence directory or reject missing evidence."""

    job_directory = _job_directory(job_id, runs_directory)
    if job_directory is None:
        raise InvocationEvidenceError(f'evidence not found for job: {job_id}')
    return job_directory


def _job_tasks(
    job_id: str,
    runs_directory: Path,
    *,
    include_stream_content: bool,
    allow_missing: bool = False,
) -> list[dict[str, object]]:
    """Read task evidence for one job."""

    job_directory = _job_directory(job_id, runs_directory)
    if job_directory is None:
        if allow_missing:
            return []
        raise InvocationEvidenceError(f'evidence not found for job: {job_id}')
    return _task_documents(
        InvocationEvidenceStore(job_directory).read_all(job_id),
        include_stream_content=include_stream_content,
    )


def _review_batch_documents(
    job: Run,
    store: JobStore,
    runs_directory: Path,
    *,
    allow_missing: bool = False,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Read validated aggregate reviewer-batch evidence for one job."""

    job_id = str(job.id)
    job_directory = _job_directory(job_id, runs_directory)
    if job_directory is None:
        if allow_missing:
            return [], []
        raise InvocationEvidenceError(f'evidence not found for job: {job_id}')
    batch_directory = job_directory / 'review-batches'
    if not batch_directory.exists():
        return [], []
    if batch_directory.is_symlink() or not batch_directory.is_dir():
        message = 'review batch evidence directory is unsafe'
        raise InvocationEvidenceError(message)
    documents: list[dict[str, object]] = []
    try:
        entries = sorted(batch_directory.iterdir())
        for path in entries:
            relative = path.relative_to(job_directory).as_posix()
            if path.is_symlink() or not path.is_file():
                message = 'review batch evidence path is unsafe'
                raise InvocationEvidenceError(message)
            temporary_parts = path.name.removeprefix('.').rsplit('.', 2)
            temporary_target = (
                (Path(relative).parent / temporary_parts[0]).as_posix()
                if len(temporary_parts) == 3 and temporary_parts[2] == 'tmp'
                else None
            )
            if (
                is_known_temporary(relative)
                and temporary_target is not None
                and canonical_evidence_type(temporary_target) == 'review_batch_result'
            ):
                continue
            expected = evidence_path('review_batch_result', ordinal=len(documents) + 1)
            if relative != expected:
                raise InvocationEvidenceError(
                    f'unexpected review batch evidence path: {relative}'
                )
            parsed = REVIEWER_BATCH_RESULT_ADAPTER.validate_json(
                path.read_text(encoding='utf-8')
            )
            if parsed.run_id != job_id or parsed.iteration != len(documents) + 1:
                raise InvocationEvidenceError(
                    f'review batch evidence does not match job: {relative}'
                )
            document = parsed.model_dump(mode='json', exclude={'run_id'})
            document['job_id'] = parsed.run_id
            document['path'] = relative
            documents.append(document)
    except (OSError, ValidationError) as error:
        raise InvocationEvidenceError(
            f'invalid review batch evidence: {error}'
        ) from error
    findings: list[dict[str, object]] = []
    if documents:
        audit = build_audit_document(
            job,
            store.list_transitions(job_id),
            (),
            runs_directory,
            verify=True,
        )
        findings = cast('list[dict[str, object]]', audit['findings'])
        aggregate_artifact_paths = {
            str(document['artifact_path'])
            for document in documents
            if document.get('schema_version') == 3
        }
        correlation_findings = [
            finding
            for finding in findings
            if str(finding.get('path', '')).startswith('review-batches/')
            or finding.get('path') in aggregate_artifact_paths
        ]
        if correlation_findings:
            first = correlation_findings[0]
            raise InvocationEvidenceError(
                'invalid review batch correlation: '
                f'{first.get("code")}: {first.get("message")}'
            )
    return documents, findings


def _review_result_documents(
    job: Run,
    job_directory: Path,
    batches: list[dict[str, object]],
    audit_findings: list[dict[str, object]],
) -> dict[tuple[int, str], dict[str, object]]:
    """Project correlated per-reviewer results from validated batch evidence."""

    documents: dict[tuple[int, str], dict[str, object]] = {}
    try:
        relevant_paths: set[str] = set()
        for batch in batches:
            if batch.get('schema_version') != 3:
                continue
            iteration = int(str(batch['iteration']))
            for member in cast('list[dict[str, object]]', batch['reviewers']):
                result_reference = member.get('result_path')
                if result_reference is None:
                    continue
                result_ordinal = evidence_ordinal(
                    'review_result', str(result_reference)
                )
                if result_ordinal is None or result_ordinal < 2:
                    message = 'reviewer result path has no valid sequence'
                    raise InvocationEvidenceError(message)
                paths = reviewer_evidence_paths(
                    sequence=result_ordinal - 1,
                    iteration=iteration,
                    reviewer_id=str(member['reviewer_id']),
                    attempt=1,
                )
                relevant_paths.update((paths.request, paths.result, paths.artifact))
        invalid = next(
            (
                finding
                for finding in audit_findings
                if finding.get('path') in relevant_paths
            ),
            None,
        )
        if invalid is not None:
            raise InvocationEvidenceError(
                'invalid reviewer result correlation: '
                f'{invalid.get("code")}: {invalid.get("message")}'
            )
        for batch in batches:
            if batch.get('schema_version') != 3:
                continue
            iteration = int(str(batch['iteration']))
            reviewers = cast('list[dict[str, object]]', batch['reviewers'])
            for member in reviewers:
                result_reference = member.get('result_path')
                if result_reference is None:
                    continue
                reviewer_id = str(member['reviewer_id'])
                result_path = reviewer_dispatch_path(
                    job_directory, str(result_reference)
                )
                result = read_json_object(result_path)
                sequence = int(result.get('sequence', 0)) - 1
                paths = reviewer_evidence_paths(
                    sequence=sequence,
                    iteration=iteration,
                    reviewer_id=reviewer_id,
                    attempt=1,
                )
                if str(result_reference) != paths.result:
                    message = 'reviewer result path does not match its batch member'
                    raise InvocationEvidenceError(message)
                request = read_json_object(
                    reviewer_dispatch_path(job_directory, paths.request)
                )
                artifact_path = reviewer_dispatch_path(job_directory, paths.artifact)
                result_for_validation = result.copy()
                result_payload = cast(
                    'dict[str, object]', result_for_validation['payload']
                ).copy()
                result_payload['artifact_path'] = str(artifact_path)
                result_for_validation['payload'] = result_payload
                verdict = validate_review_response(
                    result_for_validation, request=request, artifact_path=artifact_path
                )
                scope = result.get('scope', {})
                if (
                    result.get('run_id') != str(job.id)
                    or result.get('iteration') != iteration
                    or verdict != member['outcome']
                    or scope.get('worktree_path') != str(job.worktree_path)
                    or scope.get('base_sha') != job.base_sha
                    or scope.get('head_sha') != job.head_sha
                    or scope.get('diff_digest') != batch['diff_digest']
                    or artifact_path.relative_to(job_directory).as_posix()
                    != paths.artifact
                ):
                    message = 'reviewer result does not match its batch or job scope'
                    raise InvocationEvidenceError(message)
                payload = cast('dict[str, object]', result['payload']).copy()
                payload['artifact_path'] = paths.artifact
                documents[(iteration, reviewer_id)] = {
                    'message_id': result['message_id'],
                    'path': paths.result,
                    **payload,
                }
    except (AttributeError, KeyError, TypeError, ValueError, WorkerError) as error:
        raise InvocationEvidenceError(
            f'invalid reviewer result evidence: {error}'
        ) from error
    return documents


def _attach_reviewer_results(
    tasks: list[dict[str, object]],
    results: dict[tuple[int, str], dict[str, object]],
) -> None:
    """Attach a completed canonical result to each matching reviewer task."""

    for task in tasks:
        reviewer_id = task.get('reviewer_id')
        if reviewer_id is None:
            continue
        result = results.get((int(str(task['iteration'])), str(reviewer_id)))
        if result is not None:
            task['review_result'] = result


def _write_document(
    payload: Mapping[str, object] | None = None, *, error: object = None
) -> None:
    """Write one versioned CLI document."""

    # Every public document is the schema version, the command's own payload in
    # its declared order, then error. Building it here is what keeps a new
    # command from omitting either bracket of that contract.
    print(
        json.dumps(
            {'schema_version': CLI_SCHEMA_VERSION, **(payload or {}), 'error': error},
            indent=2,
        )
    )


def _write_job_error(
    code: str,
    message: str,
    *,
    job_id: str | None = None,
    task_id: str | None = None,
) -> None:
    """Write a versioned error for a public job or task query."""

    payload: dict[str, object] = {}
    if job_id is not None:
        payload['job_id'] = job_id
    if task_id is not None:
        payload['task_id'] = task_id
    _write_document(payload, error={'code': code, 'message': message})


def _issue_review_error(error: BaseException) -> tuple[str, str]:
    """Return the stable public code and message for an issue-review failure."""

    message = str(error)
    code = 'issue_review_failed'
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, RunNotFoundError):
            code = 'job_not_found'
            message = f'job not found: {current}'
            break
        if isinstance(current, IssueReviewerError):
            code = (
                'issue_review_timed_out'
                if current.timed_out
                else 'issue_review_interrupted'
                if current.interrupted
                else 'issue_reviewer_failed'
            )
            break
        if isinstance(current, SchemaValidationError):
            code = 'issue_review_result_invalid'
            break
        if isinstance(current, InvocationEvidenceError):
            code = 'invalid_evidence'
            break
        current = current.__cause__
    return code, message


def _run_error(error: BaseException) -> tuple[str, str]:
    """Return the stable public code and message for a run failure."""

    if isinstance(error, RunNotFoundError):
        return 'job_not_found', f'job not found: {error}'
    if isinstance(error, RuntimeRegistryError):
        return error.code, str(error)
    if isinstance(error, WorkerError):
        return error.code or 'worker_error', str(error)
    if isinstance(error, ReviewerPlanError):
        return 'reviewer_plan_invalid', str(error)
    return 'run_failed', str(error)


def _persisted_enum_error(error: PersistedEnumError) -> dict[str, str]:
    """Return the stable public error object for an unreadable job row."""

    return {'code': error.code, 'message': str(error)}


def _unreadable_job_summary(job: UnreadableJob) -> dict[str, object]:
    """Keep an unreadable row visible in the jobs listing."""

    return {
        'job_id': job.job_id,
        'created_at': job.created_at,
        'error': _persisted_enum_error(job.error),
    }


def _jobs(args: argparse.Namespace, store: JobStore) -> int:
    """List stored jobs without reading mutable workflow state."""

    known_states = {state.value: state for state in RunState}
    invalid = next((value for value in args.state if value not in known_states), None)
    if invalid is not None:
        _write_job_error(
            'invalid_job_state',
            f'unknown durable job state: {invalid}',
        )
        return 2
    explicit_states = {known_states[value] for value in args.state}
    selected_states = set(explicit_states)
    if args.attention:
        selected_states.update(HUMAN_ACTION_STATES)
    if not args.database.is_file():
        _write_job_error(
            'state_database_not_found',
            f'state database not found: {args.database}',
        )
        return 2
    summaries = [
        _unreadable_job_summary(job)
        if isinstance(job, UnreadableJob)
        else _job_summary(job)
        for job in store.list_runs_with_errors()
    ]
    summaries.extend(
        _unreadable_job_summary(job)
        if isinstance(job, UnreadableJob)
        else _issue_job_summary(job, store.list_issue_actions(job.id))
        for job in store.list_issues_with_errors()
    )
    if selected_states:
        summaries = [
            summary
            for summary in summaries
            if 'error' in summary or RunState(str(summary['state'])) in selected_states
        ]
    if args.attention:
        summaries = [
            summary
            for summary in summaries
            if (
                summary.get('worktree_status', WorktreeStatus.AVAILABLE)
                == WorktreeStatus.AVAILABLE
                or (
                    'error' not in summary
                    and RunState(str(summary['state'])) in explicit_states
                )
            )
        ]
    summaries.sort(key=lambda item: str(item['created_at']), reverse=True)
    unreadable = [summary for summary in summaries if 'error' in summary]
    _write_document(
        {'jobs': summaries}, error=unreadable[0]['error'] if unreadable else None
    )
    return 2 if unreadable else 0


def _cancel(args: argparse.Namespace, store: JobStore) -> int:  # noqa: PLR0911
    """Cancel one source-code job whose recorded worktree is unrunnable."""

    if not args.database.is_file():
        _write_job_error(
            'state_database_not_found',
            f'state database not found: {args.database}',
            job_id=args.job_id,
        )
        return 2
    try:
        store.initialize()
        run = store.get(args.job_id)
    except RunNotFoundError:
        try:
            store.get_issue(args.job_id)
        except RunNotFoundError:
            _write_job_error(
                'job_not_found', f'job not found: {args.job_id}', job_id=args.job_id
            )
        except PersistedEnumError as error:
            _write_job_error(error.code, str(error), job_id=args.job_id)
        else:
            _write_job_error(
                'job_not_cancellable',
                'cancellation applies to source-code jobs; this is an issue-review job',
                job_id=args.job_id,
            )
        return 2
    except PersistedEnumError as error:
        _write_job_error(error.code, str(error), job_id=args.job_id)
        return 2
    if worktree_status(run.worktree_path) is WorktreeStatus.AVAILABLE:
        _write_job_error(
            'job_not_cancellable', 'job worktree is available', job_id=args.job_id
        )
        return 2
    try:
        cancelled = store.cancel(args.job_id, args.reason)
    except PersistedEnumError as error:
        _write_job_error(error.code, str(error), job_id=args.job_id)
        return 2
    except (ConcurrentUpdateError, ValueError) as error:
        _write_job_error('job_not_cancellable', str(error), job_id=args.job_id)
        return 2
    _write_document(
        {
            'job_id': str(cancelled.id),
            'state': str(cancelled.state),
            'reason': args.reason.strip(),
        }
    )
    return 0


def _selected_job(
    args: argparse.Namespace,
    store: JobStore,
    *,
    include_stream_content: bool,
) -> tuple[Run | IssueJob, list[dict[str, object]]] | None:
    """Resolve one job and its task history, reporting stable query errors."""

    if not args.database.is_file():
        _write_job_error(
            'state_database_not_found',
            f'state database not found: {args.database}',
            job_id=args.job_id,
        )
        return None
    try:
        try:
            run: Run | IssueJob = store.get(args.job_id)
        except RunNotFoundError:
            run = store.get_issue(args.job_id)
        tasks = _job_tasks(
            str(run.id),
            args.runs_directory,
            include_stream_content=include_stream_content,
            allow_missing=(
                isinstance(run, Run)
                and run.state is RunState.QUEUED
                and run.iteration == 0
            ),
        )
    except RunNotFoundError as error:
        _write_job_error('job_not_found', f'job not found: {error}', job_id=args.job_id)
        return None
    except PersistedEnumError as error:
        _write_job_error(error.code, str(error), job_id=args.job_id)
        return None
    except (InvocationEvidenceError, OSError) as error:
        _write_job_error('invalid_evidence', str(error), job_id=args.job_id)
        return None
    return run, tasks


def _job(args: argparse.Namespace, store: JobStore) -> int:
    """Show one job and all currently non-terminal tasks."""

    if args.database.is_file():
        try:
            issue = store.get_issue(args.job_id)
        except RunNotFoundError:
            pass
        except PersistedEnumError as error:
            _write_job_error(error.code, str(error), job_id=args.job_id)
            return 2
        else:
            document = _issue_job_summary(issue, store.list_issue_actions(issue.id))
            try:
                tasks = _job_tasks(
                    issue.id,
                    args.runs_directory,
                    include_stream_content=False,
                )
            except (InvocationEvidenceError, OSError) as error:
                _write_job_error('invalid_evidence', str(error), job_id=args.job_id)
                return 2
            document['current'] = [
                {
                    'task_id': task['task_id'],
                    'role': task['role'],
                    'attempt': task['attempt'],
                    'status': task['status'],
                    'conclusion': task['conclusion'],
                    **(
                        {'reviewer_id': task['reviewer_id']}
                        if 'reviewer_id' in task
                        else {}
                    ),
                }
                for task in tasks
                if task['status'] in {'pending', 'running'}
            ]
            _write_document({'job': document})
            return 0

    selected = _selected_job(args, store, include_stream_content=False)
    if selected is None:
        return 2
    run, tasks = selected
    document = (
        _job_summary(run)
        if isinstance(run, Run)
        else _issue_job_summary(run, store.list_issue_actions(run.id))
    )
    try:
        review_batches, _ = (
            _review_batch_documents(
                run,
                store,
                args.runs_directory,
                allow_missing=run.state is RunState.QUEUED,
            )
            if isinstance(run, Run)
            else ([], [])
        )
    except (InvocationEvidenceError, OSError) as error:
        _write_job_error('invalid_evidence', str(error), job_id=str(run.id))
        return 2
    document['review_batches'] = review_batches
    document['current'] = [
        {
            'task_id': task['task_id'],
            'role': task['role'],
            'attempt': task['attempt'],
            'status': task['status'],
            'conclusion': task['conclusion'],
            **({'reviewer_id': task['reviewer_id']} if 'reviewer_id' in task else {}),
        }
        for task in tasks
        if task['status'] in {'pending', 'running'}
    ]
    _write_document({'job': document})
    return 0


def _tasks(args: argparse.Namespace, store: JobStore) -> int:
    """Show the complete durable task history for one job."""

    selected = _selected_job(args, store, include_stream_content=True)
    if selected is None:
        return 2
    run, tasks = selected
    try:
        review_batches, audit_findings = (
            _review_batch_documents(
                run,
                store,
                args.runs_directory,
                allow_missing=run.state is RunState.QUEUED,
            )
            if isinstance(run, Run)
            else ([], [])
        )
        if isinstance(run, Run) and review_batches:
            job_directory = _required_job_directory(str(run.id), args.runs_directory)
            _attach_reviewer_results(
                tasks,
                _review_result_documents(
                    run,
                    job_directory,
                    review_batches,
                    audit_findings,
                ),
            )
    except (InvocationEvidenceError, OSError) as error:
        _write_job_error('invalid_evidence', str(error), job_id=str(run.id))
        return 2
    _write_document(
        {
            'job_id': str(run.id),
            'provider_actions': []
            if isinstance(run, Run)
            else [
                _provider_action_summary(action)
                for action in store.list_issue_actions(run.id)
            ],
            'tasks': tasks,
            'review_batches': review_batches,
        }
    )
    return 0


def _task(args: argparse.Namespace, store: JobStore) -> int:
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
    run, tasks = selected
    matching = [task for task in tasks if task['task_id'] == args.task_id]
    if not matching:
        _write_job_error(
            'task_not_found',
            f'task not found: {args.task_id}',
            job_id=args.job_id,
            task_id=args.task_id,
        )
        return 2
    review_batches: list[dict[str, object]] = []
    matching_batches: list[dict[str, object]] = []
    try:
        if isinstance(run, Run) and matching[0]['role'] == RuntimeRole.REVIEWER.value:
            review_batches, audit_findings = _review_batch_documents(
                run, store, args.runs_directory
            )
            matching_batches = [
                batch
                for batch in review_batches
                if batch['iteration'] == matching[0]['iteration']
            ]
            if matching_batches:
                job_directory = _required_job_directory(
                    str(run.id), args.runs_directory
                )
                reviewer_id = str(matching[0]['reviewer_id'])
                selected_batch = matching_batches[0].copy()
                selected_batch['reviewers'] = [
                    member
                    for member in cast(
                        'list[dict[str, object]]', matching_batches[0]['reviewers']
                    )
                    if member['reviewer_id'] == reviewer_id
                ]
                _attach_reviewer_results(
                    matching,
                    _review_result_documents(
                        run,
                        job_directory,
                        [selected_batch],
                        audit_findings,
                    ),
                )
    except (InvocationEvidenceError, OSError) as error:
        _write_job_error(
            'invalid_evidence',
            str(error),
            job_id=str(run.id),
            task_id=args.task_id,
        )
        return 2
    if (
        isinstance(run, Run)
        and matching[0]['role'] == RuntimeRole.REVIEWER.value
        and matching_batches
    ):
        matching[0]['review_batch'] = matching_batches[0]
    _write_document({'task': matching[0]})
    return 0


def _audit(args: argparse.Namespace, store: JobStore) -> int:
    """Reconstruct and optionally verify one job's durable local history."""

    if not args.database.is_file():
        _write_job_error(
            'state_database_not_found',
            f'state database not found: {args.database}',
            job_id=args.job_id,
        )
        return 2
    try:
        try:
            job: Run | IssueJob = store.get(args.job_id)
        except RunNotFoundError:
            job = store.get_issue(args.job_id)
        actions = () if isinstance(job, Run) else store.list_issue_actions(job.id)
        document = build_audit_document(
            job,
            store.list_transitions(str(job.id)),
            actions,
            args.runs_directory,
            verify=args.verify,
        )
    except RunNotFoundError as error:
        _write_job_error('job_not_found', f'job not found: {error}', job_id=args.job_id)
        return 2
    except PersistedEnumError as error:
        _write_job_error(error.code, str(error), job_id=args.job_id)
        return 2
    except (EvidencePathError, OSError) as error:
        _write_job_error('invalid_evidence', str(error), job_id=args.job_id)
        return 2
    print(json.dumps(document, indent=2))
    return 0


def _run(args: argparse.Namespace, store: JobStore) -> int:
    """Consume one queued local run through its bounded agent loop."""

    if not args.database.is_file():
        _write_job_error(
            'state_database_not_found',
            f'state database not found: {args.database}',
            job_id=args.job_id,
        )
        return 2
    if args.reviewer_set and args.reviewer_command:
        print(
            'error: --reviewer-set cannot be combined with a custom reviewer command',
            file=sys.stderr,
        )
        return 2
    if args.reviewer_set and (
        args.reviewer_model
        or args.reviewer_agent
        != args.runtime_registry.default(RuntimeRole.REVIEWER).identifier
    ):
        print(
            'error: --reviewer-set cannot be combined with single-reviewer options',
            file=sys.stderr,
        )
        return 2
    if args.reviewer_command and (
        args.reviewer_model
        or args.reviewer_agent
        != args.runtime_registry.default(RuntimeRole.REVIEWER).identifier
        or args.developer_model
        or args.developer_agent
        != args.runtime_registry.default(RuntimeRole.DEVELOPER).identifier
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
        if args.reviewer_set:
            reviewer_plan = build_reviewer_execution_plan(
                select_reviewer_set(args.settings, args.reviewer_set),
                registry=args.runtime_registry,
                executable=Path(sys.executable),
                timeout_seconds=args.timeout,
            )
            reviewer_command = []
            reviewer_identity = InvocationIdentity(
                vendor='unknown', model=None, runtime='reviewer-set'
            )
        elif args.reviewer_command:
            reviewer_plan = None
            reviewer_command = args.reviewer_command
            reviewer_identity = InvocationIdentity(
                vendor='unknown', model=None, runtime='custom-command'
            )
        else:
            reviewer_plan = None
            reviewer_runtime = args.runtime_registry.require(
                args.reviewer_agent, RuntimeRole.REVIEWER
            )
            reviewer_command = [
                sys.executable,
                '-m',
                reviewer_runtime.module,
            ]
            if args.reviewer_model:
                reviewer_command.extend(['--model', args.reviewer_model])
            reviewer_identity = InvocationIdentity(
                vendor=reviewer_runtime.vendor,
                model=args.reviewer_model,
                runtime=args.reviewer_agent,
            )
        developer_command: list[str] = []
        developer_runtime = args.runtime_registry.require(
            args.developer_agent, RuntimeRole.DEVELOPER
        )
        if not args.reviewer_command:
            developer_command = [
                sys.executable,
                '-m',
                developer_runtime.module,
                '--role',
                'developer',
            ]
            if args.developer_model:
                developer_command.extend(['--model', args.developer_model])
        developer_identity = InvocationIdentity(
            vendor=developer_runtime.vendor,
            model=args.developer_model,
            runtime=args.developer_agent,
        )
        if reviewer_plan is not None:
            result = run_queued_reviewer_set(
                context=WorkerContext(
                    store=store,
                    runs_directory=args.runs_directory,
                    digest_worktree=_working_tree_digest,
                    registry=args.runtime_registry,
                ),
                run=run,
                objective=args.objective,
                reviewer_plan=reviewer_plan,
                developer_command=developer_command,
                developer_timeout_seconds=args.developer_timeout,
                max_iterations=args.max_iterations,
                developer_identity=developer_identity,
            )
        else:
            result = run_queued_review(
                context=WorkerContext(
                    store=store,
                    runs_directory=args.runs_directory,
                    digest_worktree=_working_tree_digest,
                    registry=args.runtime_registry,
                ),
                run=run,
                objective=args.objective,
                reviewer_command=reviewer_command,
                developer_command=developer_command,
                timeout_seconds=args.timeout,
                developer_timeout_seconds=args.developer_timeout,
                max_iterations=args.max_iterations,
                reviewer_identity=reviewer_identity,
                developer_identity=developer_identity,
            )
    except (
        OSError,
        ReviewerPlanError,
        RunNotFoundError,
        RuntimeRegistryError,
        WorkerError,
    ) as error:
        code, message = _run_error(error)
        _write_job_error(code, message, job_id=args.job_id)
        return 2
    _write_document({'job_id': str(result.id), 'state': result.state})
    return 0


def _write_resume_document(
    job_id: str,
    *,
    state: RunState | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    """Write one versioned resume result to standard output."""

    _write_document(
        {'job_id': job_id, 'state': state},
        error={'code': error_code, 'message': error_message}
        if error_code is not None
        else None,
    )


def _resume(args: argparse.Namespace, store: JobStore) -> int:
    """Resume a recoverable run using its durable execution context."""

    if not args.database.is_file():
        _write_resume_document(
            args.job_id,
            error_code='state_database_not_found',
            error_message=f'state database not found: {args.database}',
        )
        return 2
    result: Run | IssueJob
    try:
        try:
            run = store.get(args.job_id)
        except RunNotFoundError:
            issue = store.get_issue(args.job_id)
            result = resume_issue_review(
                issue,
                store,
                args.runs_directory,
                timeout=1800,
                registry=args.runtime_registry,
            )
        else:
            _require_external_database(args.database, run.worktree_path)
            result = resume_review(
                context=WorkerContext(
                    store=store,
                    runs_directory=args.runs_directory,
                    digest_worktree=_working_tree_digest,
                    registry=args.runtime_registry,
                ),
                run=run,
            )
    except RunNotFoundError as error:
        _write_resume_document(
            args.job_id,
            error_code='job_not_found',
            error_message=f'job not found: {error}',
        )
        return 2
    except PersistedEnumError as error:
        _write_resume_document(
            args.job_id,
            error_code=error.code,
            error_message=str(error),
        )
        return 2
    except ConcurrentUpdateError as error:
        _write_resume_document(
            args.job_id,
            error_code='concurrent_update',
            error_message=f'job changed concurrently: {error}',
        )
        return 2
    except (IssueReviewError, InvocationEvidenceError, OSError, WorkerError) as error:
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
        args.runtime_registry.identifiers() if args.agent == 'all' else (args.agent,)
    )
    skill_names = tuple(dict.fromkeys(args.skill))
    skill_homes: dict[str, Path] = {}
    for runtime, path in args.skill_home:
        if runtime in skill_homes:
            print(
                f'error: skill home specified twice for {runtime}',
                file=sys.stderr,
            )
            return 2
        skill_homes[runtime] = path
    try:
        results = install_skills(
            skill_names,
            agents,
            source_root=args.source,
            skill_homes=skill_homes,
            runtime_registry=args.runtime_registry,
        )
    except (SkillInstallError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    for result in results:
        status = 'installed' if result.installed else 'already installed'
        print(f'{status} {result.skill} for {result.agent}: {result.destination}')
    return 0


def _config_show(
    args: argparse.Namespace, settings: Settings, arguments: list[str]
) -> int:
    """Print deterministic effective settings and their precedence sources."""

    database_source = (
        'command_line' if '--database' in arguments else settings.database.source
    )
    runs_source = (
        'command_line'
        if '--runs-directory' in arguments
        else settings.runs_directory.source
    )
    _write_document(
        {
            'config_file': str(settings.path),
            'settings': {
                'storage.database': {
                    'value': str(args.database),
                    'source': database_source,
                },
                'storage.runs_directory': {
                    'value': str(args.runs_directory),
                    'source': runs_source,
                },
                'retention.job_evidence_days': {
                    'value': settings.job_evidence_days.value,
                    'source': settings.job_evidence_days.source,
                },
                'reviewer_sets': {
                    'value': [
                        {
                            'id': reviewer_set.identifier,
                            'members': [
                                {
                                    'id': member.identifier,
                                    'runtime': member.runtime,
                                    'vendor': member.vendor,
                                    'model': member.model,
                                    'required': True,
                                }
                                for member in reviewer_set.members
                            ],
                        }
                        for reviewer_set in settings.reviewer_sets
                    ],
                    'source': 'file' if settings.reviewer_sets else 'built_in',
                    'status': 'full_workflow',
                },
            },
        }
    )
    return 0


def _prune(args: argparse.Namespace, store: JobStore, settings: Settings) -> int:
    """Preview or apply one explicit persistent-evidence retention plan."""

    try:
        configured_days = cast('int', settings.job_evidence_days.value)
        days = parse_duration(args.older_than) if args.older_than else configured_days
        plan = build_prune_plan(
            store,
            args.database,
            args.runs_directory,
            older_than_days=days,
            include_orphans=args.orphans,
            delete_database_records=args.delete_database_records,
        )
        outcomes = apply_prune_plan(plan) if args.apply else ()
    except (OSError, RetentionError) as error:
        _write_document(error={'code': 'prune_unsafe', 'message': str(error)})
        return 2
    print(
        json.dumps(plan_document(plan, applied=args.apply, outcomes=outcomes), indent=2)
    )
    return 2 if any(item['status'] == 'failed' for item in outcomes) else 0


def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0911
    """Run the command-line interface."""

    arguments = list(argv) if argv is not None else sys.argv[1:]
    try:
        validate_packaged_manifests()
    except ManifestError as error:
        _write_document(error={'code': error.code, 'message': str(error)})
        return 2
    reviewer_command: list[str] = []
    command_name = next(
        (name for name in ('run', 'review-issue') if name in arguments), None
    )
    if command_name is not None and '--' in arguments:
        separator = arguments.index('--')
        reviewer_command = arguments[separator + 1 :]
        arguments = arguments[:separator]
    try:
        settings = load_settings(
            default_database=DEFAULT_DATABASE,
            default_runs_directory=DEFAULT_RUNS_DIRECTORY,
        )
    except SettingsError as error:
        _write_document(error={'code': 'invalid_settings', 'message': str(error)})
        return 2
    args = build_parser(settings).parse_args(arguments)
    if args.command in {'run', 'review-issue'}:
        args.reviewer_command = reviewer_command
    store = JobStore(args.database)

    if args.command == 'init':
        store.initialize()
        print(f'initialized {args.database}')
        return 0
    if args.command == 'enqueue-local':
        return _enqueue_local(args, store)
    if args.command == 'enqueue-locals':
        return _enqueue_locals(args, store)
    if args.command == 'enqueue-issue':
        return _enqueue_issue(args, store)
    if args.command == 'review-issue':
        return _review_issue(args, store)
    if args.command == 'post-issue-feedback':
        return _post_issue_feedback(args, store)
    if args.command == 'jobs':
        return _jobs(args, store)
    if args.command == 'cancel':
        return _cancel(args, store)
    if args.command == 'job':
        return _job(args, store)
    if args.command == 'tasks':
        return _tasks(args, store)
    if args.command == 'task':
        return _task(args, store)
    if args.command == 'audit':
        return _audit(args, store)
    if args.command == 'config' and args.config_command == 'show':
        return _config_show(args, settings, arguments)
    if args.command == 'prune':
        return _prune(args, store, settings)
    if args.command == 'run':
        return _run(args, store)
    if args.command == 'resume':
        return _resume(args, store)
    if args.command == 'skills' and args.skill_command == 'install':
        return _install_skills(args)

    raise AssertionError(f'unhandled command: {args.command}')


if __name__ == '__main__':
    raise SystemExit(main())
