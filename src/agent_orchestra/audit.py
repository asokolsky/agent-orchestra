"""Read-only reconstruction and verification of durable job evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import ValidationError

from agent_orchestra.evidence import (
    EVIDENCE_TYPES,
    HASH_CHUNK_SIZE,
    resolve_evidence_path,
)
from agent_orchestra.invocations import InvocationEvidenceError, read_records
from agent_orchestra.models import (
    IssueJob,
    JobTransition,
    ProviderAction,
    Run,
    RunState,
)
from agent_orchestra.schemas import (
    DeveloperHandoffMessageSchema,
    IssueReviewRequestSchema,
    IssueReviewResultSchema,
    IssueSourceSchema,
    RemediationRequestMessageSchema,
    ReviewRequestMessageSchema,
    ReviewResultMessageSchema,
)
from agent_orchestra.worker import WorkerError, read_message_chain

if TYPE_CHECKING:
    from collections.abc import Iterable

VerificationResult = Literal['verified', 'failed', 'unverifiable', 'incomplete']


@dataclass(frozen=True, slots=True)
class AuditFinding:
    """One stable, independently reportable audit failure."""

    code: str
    message: str
    path: str | None = None


def _timestamp(value: datetime) -> str:
    """Render a persisted timestamp in the public UTC spelling."""

    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def _job_document(job: Run | IssueJob) -> dict[str, object]:
    """Return scenario-specific job identity without mutable external reads."""

    common: dict[str, object] = {
        'job_id': str(job.id),
        'scenario': str(job.scenario) if isinstance(job, Run) else 'issue_review',
        'state': str(job.state),
        'iteration': job.iteration,
        'created_at': _timestamp(job.created_at),
        'updated_at': _timestamp(job.updated_at),
    }
    if isinstance(job, Run):
        common.update(
            {
                'repository_path': str(job.repo_path),
                'worktree_path': str(job.worktree_path),
                'base_sha': job.base_sha,
                'head_sha': job.head_sha,
                'diff_digest': job.diff_digest,
                'remote_url': job.remote_url,
                'supersedes_job_id': job.supersedes_run_id,
            }
        )
    else:
        common.update(
            {
                'provider': job.provider,
                'host': job.host,
                'namespace': job.namespace,
                'project': job.project,
                'issue_number': job.issue_number,
                'remote_url': job.remote_url,
                'source_updated_at': job.source_updated_at,
                'source_digest': job.source_digest,
            }
        )
    return common


def _transition_document(transition: JobTransition) -> dict[str, object]:
    """Return one transition with its immutable correlation scope."""

    return {
        'job_id': transition.job_id,
        'scenario': str(transition.scenario),
        'from_state': str(transition.from_state) if transition.from_state else None,
        'to_state': str(transition.to_state),
        'scope_digest': transition.scope_digest,
        'occurred_at': _timestamp(transition.occurred_at),
    }


def _action_document(action: ProviderAction) -> dict[str, object]:
    """Return one persisted provider publication identity."""

    return {
        'job_id': action.job_id,
        'iteration': action.iteration,
        'action': action.action,
        'provider_id': action.provider_id,
        'remote_url': action.remote_url,
        'created_at': _timestamp(action.created_at),
    }


def _derived_operations(
    transitions: Iterable[JobTransition],
) -> list[dict[str, object]]:
    """Derive authorization, commit, and publication events from state history."""

    kinds = {
        'awaiting_commit_authorization': 'commit_authorization_required',
        'committed': 'commit',
        'awaiting_publish_authorization': 'publish_authorization_required',
        'published': 'publication',
    }
    return [
        {
            'kind': kinds[str(transition.to_state)],
            **_transition_document(transition),
        }
        for transition in transitions
        if str(transition.to_state) in kinds
    ]


def _finding(code: str, message: str, path: str | None = None) -> AuditFinding:
    """Construct one audit finding."""

    return AuditFinding(code=code, message=message, path=path)


def _read_index(
    root: Path, job_id: str
) -> tuple[list[dict[str, object]], str | None, list[AuditFinding]]:
    """Read and structurally validate one integrity index without changing it."""

    path = resolve_evidence_path(root, job_id, '.integrity.json')
    if not path.is_file():
        return (
            [],
            None,
            [_finding('integrity_index_missing', 'integrity index is missing')],
        )
    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        return (
            [],
            None,
            [_finding('integrity_index_malformed', str(error), '.integrity.json')],
        )
    if (
        not isinstance(document, dict)
        or set(document) != {'schema_version', 'job_id', 'backfilled_at', 'entries'}
        or document.get('schema_version') != 1
        or document.get('job_id') != job_id
        or (
            document.get('backfilled_at') is not None
            and not isinstance(document.get('backfilled_at'), str)
        )
        or not isinstance(document.get('entries'), list)
    ):
        return (
            [],
            None,
            [
                _finding(
                    'integrity_index_malformed',
                    'integrity index has invalid fields',
                    '.integrity.json',
                )
            ],
        )
    findings: list[AuditFinding] = []
    entries: list[dict[str, object]] = []
    paths: set[str] = set()
    required = {'job_id', 'evidence_type', 'path', 'size', 'sha256', 'finalized_at'}
    for item in document['entries']:
        if not isinstance(item, dict) or set(item) != required:
            findings.append(
                _finding(
                    'integrity_index_malformed',
                    'integrity entry has invalid fields',
                    '.integrity.json',
                )
            )
            continue
        relative = item.get('path')
        evidence_type = item.get('evidence_type')
        size = item.get('size')
        digest = item.get('sha256')
        if (
            item.get('job_id') != job_id
            or not isinstance(relative, str)
            or not relative
            or not isinstance(evidence_type, str)
            or evidence_type not in EVIDENCE_TYPES
            or type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 71
            or not digest.startswith('sha256:')
            or not isinstance(item.get('finalized_at'), str)
        ):
            findings.append(
                _finding(
                    'integrity_index_malformed',
                    'integrity entry has invalid values',
                    '.integrity.json',
                )
            )
            continue
        if relative in paths:
            findings.append(
                _finding(
                    'duplicate_evidence_identity',
                    'integrity index contains a duplicate path',
                    relative,
                )
            )
            continue
        try:
            resolve_evidence_path(root, job_id, *Path(relative).parts)
        except ValueError as error:
            findings.append(_finding('evidence_path_escape', str(error), relative))
            continue
        paths.add(relative)
        entries.append(dict(item))
    backfilled_at = document['backfilled_at']
    if backfilled_at is not None:
        findings.append(
            _finding(
                'integrity_index_backfilled',
                'integrity index was created around pre-existing evidence',
                '.integrity.json',
            )
        )
    return entries, backfilled_at, findings


def _verify_entry(
    root: Path, job_id: str, entry: dict[str, object]
) -> tuple[dict[str, object], AuditFinding | None]:
    """Verify one indexed file by contained byte size and SHA-256 digest."""

    relative = str(entry['path'])
    document = dict(entry)
    try:
        path = resolve_evidence_path(root, job_id, *Path(relative).parts)
    except ValueError as error:
        document['status'] = 'escaped'
        return document, _finding('evidence_path_escape', str(error), relative)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        document['status'] = 'missing'
        return document, _finding(
            'evidence_missing', 'evidence file is missing', relative
        )
    except OSError as error:
        document['status'] = 'escaped' if path.is_symlink() else 'unreadable'
        code = 'evidence_path_escape' if path.is_symlink() else 'evidence_unreadable'
        return document, _finding(code, str(error), relative)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, 'rb') as file:
            while chunk := file.read(HASH_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        document['status'] = 'unreadable'
        return document, _finding('evidence_unreadable', str(error), relative)
    actual = f'sha256:{digest.hexdigest()}'
    if size != entry['size'] or actual != entry['sha256']:
        document['status'] = 'modified'
        document['actual_size'] = size
        document['actual_sha256'] = actual
        return document, _finding(
            'evidence_modified', 'evidence file was modified', relative
        )
    document['status'] = 'verified'
    return document, None


_MESSAGE_SCHEMAS = {
    'review_request': ReviewRequestMessageSchema,
    'review_result': ReviewResultMessageSchema,
    'remediation_request': RemediationRequestMessageSchema,
    'developer_handoff': DeveloperHandoffMessageSchema,
}


def _validate_canonical_json(
    root: Path,
    job: Run | IssueJob,
    entries: Iterable[dict[str, object]],
    transitions: Iterable[JobTransition],
) -> tuple[list[dict[str, object]], list[AuditFinding]]:
    """Validate indexed canonical JSON and return safe history summaries."""

    findings: list[AuditFinding] = []
    history: list[dict[str, object]] = []
    issue_request_digests: dict[int, str] = {}
    issue_snapshot_digests: dict[int, str] = {}
    root_issue_digest: str | None = None
    reviewing_digests: dict[int, str | None] = {}
    iteration = 0
    for transition in transitions:
        if str(transition.to_state) != 'reviewing':
            continue
        if transition.from_state is RunState.INTERRUPTED:
            continue
        iteration += 1
        reviewing_digests[iteration] = transition.scope_digest
    for entry in sorted(entries, key=lambda item: str(item['path'])):
        evidence_type = str(entry['evidence_type'])
        schema: type[Any] | None = None
        if evidence_type in _MESSAGE_SCHEMAS:
            schema = _MESSAGE_SCHEMAS[evidence_type]
        elif evidence_type == 'issue_snapshot':
            schema = IssueSourceSchema
        elif evidence_type == 'issue_review_request':
            schema = IssueReviewRequestSchema
        elif evidence_type == 'issue_review_result':
            schema = IssueReviewResultSchema
        if schema is None:
            continue
        relative = str(entry['path'])
        try:
            path = resolve_evidence_path(root, str(job.id), *Path(relative).parts)
            raw = json.loads(path.read_text(encoding='utf-8'))
            parsed = schema.model_validate(raw)
        except (OSError, ValueError, json.JSONDecodeError, ValidationError) as error:
            findings.append(_finding('invalid_canonical_json', str(error), relative))
            continue
        document = parsed.model_dump(mode='json')
        candidate_job_id = document.get('run_id', document.get('job_id'))
        if candidate_job_id is not None and candidate_job_id != str(job.id):
            findings.append(
                _finding('job_id_mismatch', 'evidence job ID differs', relative)
            )
        path_iteration: int | None = None
        parts = Path(relative).parts
        if len(parts) >= 3 and parts[0] == 'iterations':
            try:
                path_iteration = int(parts[1])
            except ValueError:
                findings.append(
                    _finding(
                        'iteration_mismatch',
                        'iteration evidence path has an invalid ordinal',
                        relative,
                    )
                )
        if evidence_type in _MESSAGE_SCHEMAS and isinstance(job, Run):
            scope = document['scope']
            expected_digest = reviewing_digests.get(document['iteration'])
            if (
                scope['worktree_path'] != str(job.worktree_path)
                or scope['base_sha'] != job.base_sha
                or scope['head_sha'] != job.head_sha
                or (
                    document['iteration'] not in reviewing_digests
                    or (
                        expected_digest is not None
                        and scope['diff_digest'] != expected_digest
                    )
                )
            ):
                findings.append(
                    _finding(
                        'scope_digest_mismatch',
                        'source message scope does not match the selected job',
                        relative,
                    )
                )
            if document['iteration'] > job.iteration:
                findings.append(
                    _finding(
                        'iteration_mismatch',
                        'source message iteration exceeds the selected job',
                        relative,
                    )
                )
        source = (
            document
            if evidence_type == 'issue_snapshot'
            else document.get('source')
            if evidence_type == 'issue_review_request'
            else None
        )
        if isinstance(job, IssueJob) and isinstance(source, dict):
            expected_source = {
                'provider': job.provider,
                'host': job.host,
                'url': job.remote_url,
                'namespace': job.namespace,
                'project': job.project,
                'issue_number': job.issue_number,
            }
            if any(source.get(key) != value for key, value in expected_source.items()):
                findings.append(
                    _finding(
                        'source_identity_mismatch',
                        'issue evidence identifies a different provider source',
                        relative,
                    )
                )
        if evidence_type == 'issue_snapshot':
            if path_iteration is None:
                root_issue_digest = document['source_digest']
            else:
                issue_snapshot_digests[path_iteration] = document['source_digest']
        elif evidence_type == 'issue_review_request':
            if document['iteration'] != path_iteration:
                findings.append(
                    _finding(
                        'iteration_mismatch',
                        'issue request iteration differs from its path',
                        relative,
                    )
                )
            if (
                path_iteration is not None
                and issue_snapshot_digests.get(path_iteration)
                != document['source']['source_digest']
            ):
                findings.append(
                    _finding(
                        'source_digest_mismatch',
                        'issue request does not match its iteration snapshot',
                        relative,
                    )
                )
            issue_request_digests[document['iteration']] = document['source'][
                'source_digest'
            ]
        elif evidence_type == 'issue_review_result':
            if path_iteration is None:
                findings.append(
                    _finding(
                        'iteration_mismatch',
                        'issue result path has no iteration ordinal',
                        relative,
                    )
                )
            elif issue_request_digests.get(path_iteration) != document['source_digest']:
                findings.append(
                    _finding(
                        'source_digest_mismatch',
                        'issue result does not match its request source digest',
                        relative,
                    )
                )
        history.append(
            {
                'path': relative,
                'evidence_type': evidence_type,
                'iteration': document.get('iteration', path_iteration),
                'message_id': document.get('message_id'),
                'verdict': document.get('verdict')
                or (document.get('payload') or {}).get('verdict'),
                'findings': document.get('findings')
                or (document.get('payload') or {}).get('findings', []),
                'dispositions': (document.get('payload') or {}).get('dispositions', []),
                'validation': (document.get('payload') or {}).get('validation', []),
            }
        )
    if isinstance(job, IssueJob):
        if issue_snapshot_digests:
            first_iteration = min(issue_snapshot_digests)
            latest_iteration = max(issue_snapshot_digests)
            if root_issue_digest != issue_snapshot_digests[first_iteration]:
                findings.append(
                    _finding(
                        'source_digest_mismatch',
                        'root issue snapshot does not match the first iteration',
                        'issue.json',
                    )
                )
            if issue_snapshot_digests[latest_iteration] != job.source_digest:
                findings.append(
                    _finding(
                        'source_digest_mismatch',
                        'latest issue snapshot does not match the selected job',
                        f'iterations/{latest_iteration:06d}/issue.json',
                    )
                )
        elif root_issue_digest is not None and root_issue_digest != job.source_digest:
            findings.append(
                _finding(
                    'source_digest_mismatch',
                    'root issue snapshot does not match the selected job',
                    'issue.json',
                )
            )
    return history, findings


def _validate_source_message_chain(root: Path, job: Run) -> list[AuditFinding]:
    """Apply the workflow's exact source-message correlation rules."""

    job_directory = resolve_evidence_path(root, str(job.id))
    messages = resolve_evidence_path(root, str(job.id), 'messages')
    if not messages.is_dir():
        return []
    try:
        read_message_chain(job_directory, str(job.id))
    except (OSError, ValueError, WorkerError) as error:
        return [
            _finding(
                'message_correlation_failure',
                str(error),
                'messages',
            )
        ]
    return []


def _tasks(
    root: Path, job_id: str
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[AuditFinding]]:
    """Read attempt history and identify live, non-finalized process streams."""

    try:
        job_directory = resolve_evidence_path(root, job_id)
        records = read_records(job_directory, job_id) if job_directory.is_dir() else ()
    except (InvocationEvidenceError, OSError, ValueError) as error:
        return [], [], [_finding('invalid_invocation_evidence', str(error))]
    grouped: dict[str, list[dict[str, Any]]] = {}
    in_progress: list[dict[str, object]] = []
    findings: list[AuditFinding] = []
    for record in records:
        try:
            stdout_relative = Path(record.stdout_path).relative_to(job_directory)
            stderr_relative = Path(record.stderr_path).relative_to(job_directory)
        except ValueError:
            findings.append(
                _finding(
                    'evidence_path_escape',
                    'attempt stream path escapes the selected job',
                )
            )
            continue
        attempt = asdict(record)
        attempt['effective_models'] = list(record.effective_models)
        attempt['streams'] = {
            'stdout': {'path': stdout_relative.as_posix()},
            'stderr': {'path': stderr_relative.as_posix()},
        }
        attempt.pop('stdout_path')
        attempt.pop('stderr_path')
        grouped.setdefault(record.task_id, []).append(attempt)
        if not record.task_id.startswith(f'{job_id}:'):
            findings.append(
                _finding(
                    'task_id_mismatch',
                    'attempt task ID belongs to a different job',
                )
            )
        if not record.task_id.endswith(f'-{record.role}'):
            findings.append(
                _finding(
                    'role_mismatch',
                    'attempt role does not match its task ID',
                )
            )
        expected_invocation_id = f'{record.task_id}:attempt-{record.attempt:04d}'
        if record.invocation_id != expected_invocation_id:
            findings.append(
                _finding(
                    'attempt_id_mismatch',
                    'attempt ID does not match its task and ordinal',
                )
            )
        if record.status != 'completed':
            for stream, path_value in (
                ('process_stdout', stdout_relative),
                ('process_stderr', stderr_relative),
            ):
                in_progress.append(
                    {
                        'job_id': job_id,
                        'evidence_type': stream,
                        'path': path_value.as_posix(),
                        'size': None,
                        'sha256': None,
                        'finalized_at': None,
                        'status': 'in_progress',
                    }
                )
    tasks: list[dict[str, object]] = []
    for task_id, attempts in sorted(grouped.items()):
        attempts.sort(key=lambda item: int(item['attempt']))
        latest = attempts[-1]
        tasks.append(
            {
                'task_id': task_id,
                'job_id': job_id,
                'role': latest['role'],
                'iteration': latest['iteration'],
                'status': latest['status'],
                'conclusion': latest['conclusion'],
                'attempts': attempts,
            }
        )
    return tasks, in_progress, findings


def _inventory_unindexed(
    root: Path,
    job_id: str,
    indexed_paths: set[str],
    in_progress_paths: set[str],
    *,
    index_usable: bool,
) -> tuple[list[dict[str, object]], list[AuditFinding]]:
    """Inventory contained files omitted from the finalized evidence index."""

    job_directory = resolve_evidence_path(root, job_id)
    if not job_directory.is_dir():
        return [], []
    ignored = {'.integrity.json', '.integrity.lock'}
    evidence: list[dict[str, object]] = []
    findings: list[AuditFinding] = []

    def visit(directory: Path) -> None:
        """Walk one real directory without following symlinks."""

        try:
            entries = sorted(os.scandir(directory), key=lambda item: item.name)
        except OSError as error:
            findings.append(_finding('evidence_unreadable', str(error)))
            return
        for item in entries:
            path = Path(item.path)
            relative = path.relative_to(job_directory).as_posix()
            if relative in ignored:
                continue
            if item.is_symlink():
                findings.append(
                    _finding(
                        'evidence_path_escape',
                        'unindexed evidence path is a symlink',
                        relative,
                    )
                )
                continue
            if item.is_dir(follow_symlinks=False):
                visit(path)
                continue
            if not item.is_file(follow_symlinks=False):
                findings.append(
                    _finding(
                        'evidence_unreadable',
                        'unindexed evidence is not a regular file',
                        relative,
                    )
                )
                continue
            if relative in indexed_paths or relative in in_progress_paths:
                continue
            temporary = _is_known_temporary(relative)
            status = (
                'in_progress'
                if temporary and index_usable
                else 'unindexed'
                if index_usable
                else 'unverifiable'
            )
            evidence.append(
                {
                    'job_id': job_id,
                    'evidence_type': None,
                    'path': relative,
                    'size': item.stat(follow_symlinks=False).st_size,
                    'sha256': None,
                    'finalized_at': None,
                    'status': status,
                }
            )
            if index_usable and not temporary:
                findings.append(
                    _finding(
                        'unindexed_evidence',
                        'finalized or stale evidence is absent from the integrity index',
                        relative,
                    )
                )

    visit(job_directory)
    return evidence, findings


def _is_known_temporary(relative: str) -> bool:
    """Return whether a path matches a temporary file emitted by current writers."""

    name = Path(relative).name
    uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
    return (
        relative == '.integrity.pending.json'
        or name in {'.review-result.json', '.developer-handoff.json'}
        or re.fullmatch(rf'\..+\.{uuid_pattern}\.tmp', name) is not None
        or re.fullmatch(rf'\.candidate-result-{uuid_pattern}\.json', name) is not None
    )


def _result(
    findings: Iterable[AuditFinding], *, in_progress: bool
) -> VerificationResult:
    """Apply the public result precedence to aggregated findings."""

    codes = {finding.code for finding in findings}
    failed = {
        'duplicate_evidence_identity',
        'evidence_missing',
        'evidence_modified',
        'evidence_path_escape',
        'evidence_unreadable',
        'invalid_canonical_json',
        'invalid_invocation_evidence',
        'iteration_mismatch',
        'job_id_mismatch',
        'message_correlation_failure',
        'message_sequence_mismatch',
        'source_digest_mismatch',
        'source_identity_mismatch',
        'task_id_mismatch',
        'attempt_id_mismatch',
        'role_mismatch',
        'scope_digest_mismatch',
        'unindexed_evidence',
    }
    unverifiable = {
        'integrity_index_missing',
        'integrity_index_malformed',
        'integrity_index_backfilled',
        'transition_digest_missing',
    }
    if codes & failed:
        return 'failed'
    if codes & unverifiable:
        return 'unverifiable'
    if in_progress:
        return 'incomplete'
    return 'verified'


def build_audit_document(
    job: Run | IssueJob,
    transitions: tuple[JobTransition, ...],
    actions: tuple[ProviderAction, ...],
    runs_directory: Path,
    *,
    verify: bool,
) -> dict[str, object]:
    """Build one deterministic audit document without mutating durable state."""

    root = runs_directory.expanduser().resolve()
    job_id = str(job.id)
    findings: list[AuditFinding] = []
    entries, backfilled_at, index_findings = _read_index(root, job_id)
    findings.extend(index_findings)
    evidence: list[dict[str, object]] = []
    for entry in entries:
        if verify:
            verified, finding = _verify_entry(root, job_id, entry)
            evidence.append(verified)
            if finding is not None:
                findings.append(finding)
        else:
            evidence.append({**entry, 'status': 'not_verified'})
    tasks, in_progress, task_findings = _tasks(root, job_id)
    findings.extend(task_findings)
    evidence.extend(in_progress)
    inventory, inventory_findings = _inventory_unindexed(
        root,
        job_id,
        {str(entry['path']) for entry in entries},
        {str(entry['path']) for entry in in_progress},
        index_usable=not any(
            finding.code in {'integrity_index_missing', 'integrity_index_malformed'}
            for finding in index_findings
        ),
    )
    evidence.extend(inventory)
    if verify:
        findings.extend(inventory_findings)
    history, canonical_findings = _validate_canonical_json(
        root, job, entries, transitions
    )
    if verify:
        findings.extend(canonical_findings)
        if isinstance(job, Run):
            findings.extend(_validate_source_message_chain(root, job))
        findings.extend(
            _finding(
                'transition_digest_missing',
                'transition has no immutable scope digest',
            )
            for transition in transitions
            if transition.scope_digest is None
        )
    document: dict[str, object] = {
        'schema_version': 10,
        'job': _job_document(job),
        'transitions': [_transition_document(item) for item in transitions],
        'operations': _derived_operations(transitions),
        'tasks': tasks,
        'evidence': sorted(evidence, key=lambda item: str(item['path'])),
        'integrity': {'schema_version': 1, 'backfilled_at': backfilled_at},
        'history': history,
        'provider_actions': [_action_document(item) for item in actions],
        'findings': [asdict(item) for item in findings],
        'error': None,
    }
    if verify:
        document['result'] = _result(
            findings,
            in_progress=any(item['status'] == 'in_progress' for item in evidence),
        )
    return document
