"""Orchestrate read-only review iterations for captured provider issues."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never, cast
from uuid import uuid4

from pydantic import ValidationError

from agent_orchestra.adapter.base import IssueReviewerAdapter, IssueReviewExecution
from agent_orchestra.adapter.issue_reviewer import IssueReviewerError
from agent_orchestra.adapter.registry import (
    DEFAULT_RUNTIME_REGISTRY,
    RuntimeRegistry,
    RuntimeRegistryError,
    RuntimeRole,
)
from agent_orchestra.evidence import (
    EvidencePathError,
    EvidenceType,
    JobEvidence,
    evidence_root_for_job,
    resolve_evidence_path,
)
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptStatus,
    InvocationEvidenceError,
    InvocationEvidenceStore,
    InvocationRecord,
    timestamp,
    transition_attempt,
)
from agent_orchestra.issue_sources import (
    IssueLocator,
    IssueSourceError,
    fetch_issue,
    publish_feedback,
    write_snapshot,
)
from agent_orchestra.manifests import evidence_path
from agent_orchestra.models import IssueJob, ProviderAction, RunState
from agent_orchestra.schemas import (
    IssueReviewRequestSchema,
    IssueSourceSchema,
    SchemaValidationError,
    validate_issue_review_result,
)

if TYPE_CHECKING:
    from agent_orchestra.store import JobStore


class IssueReviewError(RuntimeError):
    """Raised when an issue review cannot be completed safely."""


def _reject(message: str) -> Never:
    """Raise one issue-review validation failure."""

    raise IssueReviewError(message)


def _write_json(
    job_directory: Path,
    path: Path,
    document: dict[str, Any],
    evidence_type: EvidenceType,
) -> None:
    """Write a JSON object atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        JobEvidence.for_directory(job_directory).finalize_write(
            temporary, path, evidence_type
        )
    finally:
        temporary.unlink(missing_ok=True)


def _write_text(
    job_directory: Path,
    path: Path,
    content: str,
    *,
    evidence_type: EvidenceType | None = None,
) -> None:
    """Write UTF-8 text atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if evidence_type is not None:
            JobEvidence.for_directory(job_directory).finalize_write(
                temporary, path, evidence_type
            )
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _evidence_path(job_directory: Path, *parts: str) -> Path:
    """Resolve one path through the shared job evidence boundary."""

    try:
        return resolve_evidence_path(
            evidence_root_for_job(job_directory), job_directory.name, *parts
        )
    except EvidencePathError as error:
        raise IssueReviewError(str(error)) from error


def _job_directory(root: Path, job_id: str) -> Path:
    """Resolve one issue job while preserving its public error surface."""

    try:
        return resolve_evidence_path(root, job_id)
    except EvidencePathError as error:
        raise IssueReviewError(str(error)) from error


def _record_finalized_path(
    job_directory: Path, path: Path, evidence_type: EvidenceType
) -> None:
    """Record a finalized issue-review artifact in its owning job index."""

    JobEvidence.for_directory(job_directory).record_finalized(path, evidence_type)


def _read_json(path: Path) -> dict[str, Any]:
    """Read a UTF-8 JSON object or fail closed."""

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        message = f'invalid issue evidence: {path}'
        raise IssueReviewError(message) from error
    if not isinstance(document, dict):
        message = f'invalid issue evidence: {path}'
        raise IssueReviewError(message)
    return document


def _render_feedback(result: dict[str, Any]) -> str:
    """Render canonical issue-review JSON as human-readable Markdown."""

    lines = [
        '# Issue readiness review',
        '',
        f'**Verdict:** {result["verdict"]}',
        '',
        '## Summary',
        '',
        str(result['summary']),
        '',
        '## Findings',
        '',
    ]
    findings = result['findings']
    if not findings:
        lines.append('No findings.')
    for finding in findings:
        location = finding['section'] or 'General'
        lines.extend(
            [
                f'### {finding["finding_id"]}: {finding["title"]}',
                '',
                f'- Dimension: {finding["dimension"]}',
                f'- Severity: {finding["severity"]}',
                f'- Section or field: {location}',
                '',
                str(finding['explanation']),
                '',
                f'Suggested change: {finding["suggested_change"]}',
                '',
            ]
        )
    lines.extend(['## Validation', ''])
    validation = result['validation']
    lines.extend(
        (f'- {item}' for item in validation)
        if validation
        else ['No validation reported.']
    )
    lines.extend(['', '## Verification gaps', ''])
    verification_gaps = result['verification_gaps']
    lines.extend(
        (f'- {item}' for item in verification_gaps)
        if verification_gaps
        else ['No verification gaps reported.']
    )
    return '\n'.join(lines).rstrip() + '\n'


def _custom_review(
    command: tuple[str, ...], request_path: Path, candidate_path: Path, timeout: int
) -> IssueReviewExecution:
    """Invoke a testable custom reviewer command through canonical file paths."""

    try:
        completed = subprocess.run(
            [*command, str(request_path), str(candidate_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        message = 'custom issue review timed out'
        raise IssueReviewerError(
            message,
            stdout=str(error.stdout or ''),
            stderr=str(error.stderr or ''),
            timed_out=True,
        ) from error
    except OSError as error:
        raise IssueReviewerError(str(error)) from error
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or completed.stdout.strip()
        message = f'issue reviewer failed: {diagnostic}'
        raise IssueReviewerError(
            message,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
        )
    return IssueReviewExecution(
        _read_json(candidate_path),
        completed.stdout,
        completed.stderr,
        completed.returncode,
    )


def _start_invocation(
    job_directory: Path,
    job: IssueJob,
    iteration: int,
    *,
    agent: str,
    vendor: str,
    model: str | None,
    attempt: int,
) -> tuple[Path, InvocationRecord]:
    """Persist a running issue-review attempt and return its record location."""

    stem = f'{iteration:06d}-issue-reviewer-attempt-{attempt:04d}'
    stdout_path = _evidence_path(job_directory, 'logs', f'{stem}.stdout.log')
    stderr_path = _evidence_path(job_directory, 'logs', f'{stem}.stderr.log')
    _write_text(job_directory, stdout_path, '')
    _write_text(job_directory, stderr_path, '')
    task_id = f'{job.id}:{iteration:06d}-issue_reviewer'
    pending = InvocationRecord(
        schema_version=4,
        run_id=job.id,
        task_id=task_id,
        invocation_id=f'{task_id}:attempt-{attempt:04d}',
        role='issue_reviewer',
        agent_vendor=vendor,
        requested_model=model if agent != 'custom' else None,
        effective_models=(),
        effective_model_status='unavailable',
        runtime=agent,
        iteration=iteration,
        started_at=timestamp(),
        finished_at=None,
        exit_code=None,
        timed_out=False,
        interrupted=False,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        attempt=attempt,
        status='pending',
        conclusion=None,
    )
    record_path = _evidence_path(job_directory, 'invocations', f'{stem}.json')
    InvocationEvidenceStore(job_directory).write(record_path, pending)
    running = transition_attempt(pending, AttemptStatus.RUNNING)
    InvocationEvidenceStore(job_directory).write(record_path, running)
    return record_path, running


def _finish_invocation(
    job_directory: Path,
    record_path: Path,
    record: InvocationRecord,
    *,
    execution: IssueReviewExecution | None = None,
    error: BaseException | None = None,
) -> None:
    """Persist terminal issue-review output and attempt state."""

    reviewer_error = error if isinstance(error, IssueReviewerError) else None
    stdout = (
        execution.stdout
        if execution is not None
        else reviewer_error.stdout
        if reviewer_error
        else ''
    )
    stderr = (
        execution.stderr
        if execution is not None
        else reviewer_error.stderr
        if reviewer_error
        else ''
    )
    if error is not None and not stderr:
        stderr = f'{type(error).__name__}: {error}\n'
    _write_text(job_directory, Path(record.stdout_path), stdout)
    _write_text(job_directory, Path(record.stderr_path), stderr)
    conclusion = (
        AttemptConclusion.TIMED_OUT
        if reviewer_error is not None and reviewer_error.timed_out
        else AttemptConclusion.INTERRUPTED
        if reviewer_error is not None and reviewer_error.interrupted
        else AttemptConclusion.SUCCEEDED
        if error is None
        else AttemptConclusion.FAILED
    )
    completed = transition_attempt(
        record,
        AttemptStatus.COMPLETED,
        conclusion=conclusion,
        finished_at=timestamp(),
        response_received_at=timestamp() if execution is not None else None,
        validation_started_at=timestamp() if execution is not None else None,
    )
    completed = replace(
        completed,
        exit_code=(
            execution.exit_code
            if execution is not None
            else reviewer_error.exit_code
            if reviewer_error is not None
            else None
        ),
        effective_models=(execution.effective_models if execution is not None else ()),
        effective_model_status=(
            'reported'
            if execution is not None and execution.effective_models
            else 'unavailable'
        ),
    )
    InvocationEvidenceStore(job_directory).write(record_path, completed)
    _record_finalized_path(job_directory, Path(record.stdout_path), 'process_stdout')
    _record_finalized_path(job_directory, Path(record.stderr_path), 'process_stderr')


def run_issue_review(
    job: IssueJob,
    store: JobStore,
    runs_directory: Path,
    *,
    objective: str,
    agent: str,
    model: str | None,
    timeout: int,
    command: tuple[str, ...] = (),
    registry: RuntimeRegistry = DEFAULT_RUNTIME_REGISTRY,
) -> IssueJob:
    """Run one review bound to the latest immutable issue revision."""

    adapter: IssueReviewerAdapter | None = None
    try:
        runtime = (
            None if command else registry.require(agent, RuntimeRole.ISSUE_REVIEWER)
        )
        if runtime is not None:
            adapter = cast(
                'IssueReviewerAdapter',
                registry.adapter(agent, RuntimeRole.ISSUE_REVIEWER, model),
            )
    except RuntimeRegistryError as error:
        raise IssueReviewError(str(error)) from error
    root = runs_directory.expanduser().resolve()
    job_directory = _job_directory(root, job.id)
    JobEvidence(root, job.id).recover_index()
    InvocationEvidenceStore(job_directory).recover_completed(job.id)
    captured_path = _evidence_path(
        job_directory, *Path(evidence_path('issue_snapshot')).parts
    )
    captured = _read_json(captured_path)
    try:
        captured_source = IssueSourceSchema.model_validate(captured)
        current = fetch_issue(job.remote_url)
    except (IssueSourceError, ValidationError) as error:
        raise IssueReviewError(str(error)) from error
    if job.state is RunState.QUEUED and (
        current.digest != captured_source.source_digest
        or current.updated_at != captured_source.updated_at
    ):
        message = 'issue changed after capture; start a new review iteration'
        raise IssueReviewError(message)
    current_result_path = _evidence_path(
        job_directory,
        *Path(evidence_path('issue_review_result', ordinal=job.iteration)).parts,
    )
    current_task_id = f'{job.id}:{job.iteration:06d}-issue_reviewer'
    current_attempts = [
        record
        for record in InvocationEvidenceStore(job_directory).read_all(job.id)
        if record.task_id == current_task_id
    ]
    latest_attempt = current_attempts[-1] if current_attempts else None
    if (
        job.state in {RunState.FAILED, RunState.REVIEWING}
        and job.iteration > 0
        and current_result_path.exists()
        and current.digest == job.source_digest
    ):
        recovery_request_path = _evidence_path(
            job_directory,
            *Path(evidence_path('issue_review_request', ordinal=job.iteration)).parts,
        )
        recovery_request = IssueReviewRequestSchema.model_validate(
            _read_json(recovery_request_path)
        )
        recovered_result = validate_issue_review_result(_read_json(current_result_path))
        successful_attempts = [
            record
            for record in current_attempts
            if record.status == 'completed' and record.conclusion == 'succeeded'
        ]
        if (
            recovery_request.job_id != job.id
            or recovery_request.iteration != job.iteration
            or recovery_request.source.source_digest != current.digest
            or recovered_result.source_digest != current.digest
            or len(successful_attempts) != 1
        ):
            message = (
                'stored issue review result is not correlated to one successful attempt'
            )
            raise IssueReviewError(message)
        feedback_path = _evidence_path(
            job_directory,
            'iterations',
            f'{job.iteration:06d}',
            'feedback.md',
        )
        if not feedback_path.is_file():
            message = 'stored issue review feedback is incomplete'
            raise IssueReviewError(message)
        recovered_state = (
            RunState.APPROVED
            if recovered_result.verdict == 'ready'
            else RunState.CHANGES_REQUESTED
            if recovered_result.verdict == 'changes_requested'
            else RunState.FAILED
        )
        recovered = replace(job, state=recovered_state, updated_at=datetime.now(UTC))
        store.update_issue(recovered, job.state)
        return recovered
    retry = (
        job.state in {RunState.FAILED, RunState.REVIEWING}
        and job.iteration > 0
        and current.digest == job.source_digest
        and not current_result_path.exists()
        and not current_result_path.is_symlink()
        and (latest_attempt is None or latest_attempt.status == 'completed')
    )
    if (
        job.state is RunState.REVIEWING
        and current.digest == job.source_digest
        and latest_attempt is not None
        and latest_attempt.status != 'completed'
    ):
        message = 'issue review activation is uncertain'
        raise IssueReviewError(message)
    if job.iteration > 0 and current.digest == job.source_digest and not retry:
        message = 'issue has not changed since the previous review'
        raise IssueReviewError(message)
    iteration = job.iteration if retry else job.iteration + 1
    source_path = _evidence_path(
        job_directory, *Path(evidence_path('issue_snapshot', ordinal=iteration)).parts
    )
    if retry:
        existing_source = IssueSourceSchema.model_validate(_read_json(source_path))
        if existing_source.source_digest != current.digest:
            message = 'stored issue snapshot does not match the retry source'
            raise IssueReviewError(message)
    else:
        write_snapshot(root, job.id, source_path, current)
    request_path = _evidence_path(
        job_directory,
        *Path(evidence_path('issue_review_request', ordinal=iteration)).parts,
    )
    if retry:
        request = IssueReviewRequestSchema.model_validate(
            _read_json(request_path)
        ).model_dump(mode='json')
    else:
        prior = None
        for prior_iteration in range(job.iteration, 0, -1):
            prior_path = _evidence_path(
                job_directory,
                *Path(
                    evidence_path('issue_review_result', ordinal=prior_iteration)
                ).parts,
            )
            if prior_path.exists() or prior_path.is_symlink():
                prior = _read_json(prior_path)
                break
        request = IssueReviewRequestSchema.model_validate(
            {
                'schema_version': 1,
                'job_id': job.id,
                'iteration': iteration,
                'objective': objective,
                'allowed_actions': ['read_issue_snapshot', 'write_review_evidence'],
                'source': current.document(),
                'prior_review': prior,
            }
        ).model_dump(mode='json')
    result_path = _evidence_path(
        job_directory,
        *Path(evidence_path('issue_review_result', ordinal=iteration)).parts,
    )
    if result_path.exists() or result_path.is_symlink():
        message = 'issue review result path already exists'
        raise IssueReviewError(message)
    candidate_path = _evidence_path(
        job_directory,
        'iterations',
        f'{iteration:06d}',
        f'.candidate-result-{uuid4()}.json',
    )
    if not retry:
        _write_json(job_directory, request_path, request, 'issue_review_request')
    reviewing = replace(
        job,
        state=RunState.REVIEWING,
        title=current.title,
        author=current.author,
        source_updated_at=current.updated_at,
        source_digest=current.digest,
        iteration=iteration,
        updated_at=datetime.now(UTC),
    )
    task_id = f'{job.id}:{iteration:06d}-issue_reviewer'
    attempts = [
        record.attempt
        for record in InvocationEvidenceStore(job_directory).read_all(job.id)
        if record.task_id == task_id
    ]
    attempt = max(attempts, default=0) + 1
    store.update_issue(reviewing, job.state)
    record_path: Path | None = None
    invocation: InvocationRecord | None = None
    execution: IssueReviewExecution | None = None
    invocation_finished = False
    try:
        record_path, invocation = _start_invocation(
            job_directory,
            job,
            iteration,
            agent='custom' if command else agent,
            vendor='custom' if runtime is None else runtime.vendor,
            model=model,
            attempt=attempt,
        )
        if command:
            execution = _custom_review(command, request_path, candidate_path, timeout)
        else:
            assert adapter is not None
            execution = adapter.execute(request, timeout=timeout)
        raw_result = execution.result
        result = validate_issue_review_result(raw_result)
        if result.source_digest != current.digest:
            message = 'issue review result source digest does not match request'
            _reject(message)
        latest = fetch_issue(job.remote_url)
        if latest.digest != current.digest or latest.updated_at != current.updated_at:
            message = 'issue changed during review; result rejected'
            _reject(message)
        result_document = result.model_dump(mode='json')
        _write_text(
            job_directory,
            _evidence_path(
                job_directory, 'iterations', f'{iteration:06d}', 'feedback.md'
            ),
            _render_feedback(result_document),
            evidence_type='issue_feedback',
        )
        _finish_invocation(job_directory, record_path, invocation, execution=execution)
        invocation_finished = True
        _write_json(job_directory, result_path, result_document, 'issue_review_result')
    except (
        IssueReviewError,
        IssueReviewerError,
        IssueSourceError,
        SchemaValidationError,
        InvocationEvidenceError,
        OSError,
    ) as error:
        if (
            record_path is not None
            and invocation is not None
            and not invocation_finished
        ):
            _finish_invocation(
                job_directory,
                record_path,
                invocation,
                execution=execution,
                error=error,
            )
        failed = replace(reviewing, state=RunState.FAILED, updated_at=datetime.now(UTC))
        store.update_issue(failed, RunState.REVIEWING)
        if isinstance(error, IssueReviewError):
            raise
        raise IssueReviewError(str(error)) from error
    finally:
        candidate_path.unlink(missing_ok=True)
    state = (
        RunState.APPROVED
        if result.verdict == 'ready'
        else RunState.CHANGES_REQUESTED
        if result.verdict == 'changes_requested'
        else RunState.FAILED
    )
    finished = replace(reviewing, state=state, updated_at=datetime.now(UTC))
    store.update_issue(finished, RunState.REVIEWING)
    return finished


def resume_issue_review(
    job: IssueJob,
    store: JobStore,
    runs_directory: Path,
    *,
    timeout: int,
    registry: RuntimeRegistry = DEFAULT_RUNTIME_REGISTRY,
) -> IssueJob:
    """Resume one built-in issue-review attempt from durable evidence."""

    if job.iteration < 1 or job.state not in {RunState.FAILED, RunState.REVIEWING}:
        message = f'issue-review job is not resumable from {job.state}'
        raise IssueReviewError(message)
    root = runs_directory.expanduser().resolve()
    job_directory = _job_directory(root, job.id)
    request_path = _evidence_path(
        job_directory,
        *Path(evidence_path('issue_review_request', ordinal=job.iteration)).parts,
    )
    try:
        request = IssueReviewRequestSchema.model_validate(_read_json(request_path))
    except ValidationError as error:
        message = 'invalid persisted issue-review request'
        raise IssueReviewError(message) from error
    task_id = f'{job.id}:{job.iteration:06d}-issue_reviewer'
    attempts = [
        record
        for record in InvocationEvidenceStore(job_directory).read_all(job.id)
        if record.task_id == task_id
    ]
    if not attempts:
        message = 'issue-review resume has no durable attempt evidence'
        raise IssueReviewError(message)
    latest = attempts[-1]
    if latest.status != 'completed':
        message = 'issue review activation is uncertain'
        raise IssueReviewError(message)
    try:
        registry.require(latest.runtime, RuntimeRole.ISSUE_REVIEWER)
    except RuntimeRegistryError as error:
        message = 'custom issue reviewers must be retried with review-issue'
        raise IssueReviewError(f'{message}: {error}') from error
    return run_issue_review(
        job,
        store,
        runs_directory,
        objective=request.objective,
        agent=latest.runtime,
        model=latest.requested_model,
        timeout=timeout,
        registry=registry,
    )


def _publish_issue_feedback_locked(
    job: IssueJob, store: JobStore, runs_directory: Path
) -> ProviderAction:
    """Publish accepted feedback while holding its per-iteration lock."""

    if job.iteration < 1 or job.state not in {
        RunState.APPROVED,
        RunState.CHANGES_REQUESTED,
    }:
        message = f'job is not ready to publish feedback from {job.state}'
        raise IssueReviewError(message)
    existing = store.get_issue_action(job.id, job.iteration, 'publish_feedback')
    if existing is not None:
        return existing
    root = runs_directory.expanduser().resolve()
    job_directory = _job_directory(root, job.id)
    feedback_path = _evidence_path(
        job_directory, 'iterations', f'{job.iteration:06d}', 'feedback.md'
    )
    try:
        body = feedback_path.read_text(encoding='utf-8')
        current = fetch_issue(job.remote_url)
    except (OSError, IssueSourceError) as error:
        raise IssueReviewError(str(error)) from error
    if (
        current.digest != job.source_digest
        or current.updated_at != job.source_updated_at
    ):
        message = 'issue changed after review; feedback publication rejected'
        raise IssueReviewError(message)
    marker = f'<!-- agent-orchestra:{job.id}:{job.iteration}:{job.source_digest} -->'
    locator = IssueLocator(
        job.provider,
        job.host,
        job.namespace,
        job.project,
        job.issue_number,
        job.remote_url,
    )
    try:
        published = publish_feedback(locator, body, idempotency_marker=marker)
    except IssueSourceError as error:
        raise IssueReviewError(str(error)) from error
    action = ProviderAction(
        job_id=job.id,
        iteration=job.iteration,
        action='publish_feedback',
        provider_id=published.provider_id,
        remote_url=published.url,
    )
    store.add_issue_action(action)
    return store.get_issue_action(job.id, job.iteration, 'publish_feedback') or action


def publish_issue_feedback(
    job: IssueJob, store: JobStore, runs_directory: Path
) -> ProviderAction:
    """Serialize and publish accepted feedback once for an issue iteration."""

    root = runs_directory.expanduser().resolve()
    job_directory = _job_directory(root, job.id)
    lock_path = _evidence_path(
        job_directory, 'iterations', f'{job.iteration:06d}', '.publish.lock'
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        message = 'publication lock path is a symlink'
        raise IssueReviewError(message)
    with lock_path.open('a+', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _publish_issue_feedback_locked(job, store, runs_directory)
