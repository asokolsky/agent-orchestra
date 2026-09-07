"""Orchestrate read-only review iterations for captured provider issues."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never
from uuid import uuid4

from pydantic import ValidationError

from agent_orchestra.adapter.base import IssueReviewExecution
from agent_orchestra.adapter.claude_code import ClaudeCodeIssueReviewerAdapter
from agent_orchestra.adapter.codex import CodexIssueReviewerAdapter
from agent_orchestra.adapter.issue_reviewer import IssueReviewerError
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptStatus,
    InvocationEvidenceError,
    InvocationRecord,
    read_records,
    timestamp,
    transition_attempt,
    write_record,
)
from agent_orchestra.issue_sources import (
    IssueLocator,
    IssueSourceError,
    fetch_issue,
    publish_feedback,
    write_snapshot,
)
from agent_orchestra.models import IssueJob, ProviderAction, RunState
from agent_orchestra.schemas import (
    IssueReviewRequestSchema,
    IssueSourceSchema,
    SchemaValidationError,
    validate_issue_review_result,
)

if TYPE_CHECKING:
    from agent_orchestra.store import RunStore


class IssueReviewError(RuntimeError):
    """Raised when an issue review cannot be completed safely."""


def _reject(message: str) -> Never:
    """Raise one issue-review validation failure."""

    raise IssueReviewError(message)


def _write_json(path: Path, document: dict[str, Any]) -> None:
    """Write a JSON object atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_text(path: Path, content: str) -> None:
    """Write UTF-8 text atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _evidence_path(root: Path, *parts: str) -> Path:
    """Return a contained evidence path with no symlinked path component."""

    candidate = root.joinpath(*parts)
    current = root
    for part in candidate.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            message = f'evidence path contains a symlink: {current}'
            raise IssueReviewError(message)
    if not candidate.resolve().is_relative_to(root):
        message = 'evidence path escapes the runs directory'
        raise IssueReviewError(message)
    return candidate


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
    model: str | None,
    attempt: int,
) -> tuple[Path, InvocationRecord]:
    """Persist a running issue-review attempt and return its record location."""

    stem = f'{iteration:06d}-issue-reviewer-attempt-{attempt:04d}'
    stdout_path = _evidence_path(job_directory, 'logs', f'{stem}.stdout.log')
    stderr_path = _evidence_path(job_directory, 'logs', f'{stem}.stderr.log')
    _write_text(stdout_path, '')
    _write_text(stderr_path, '')
    task_id = f'{job.id}:{iteration:06d}-issue_reviewer'
    pending = InvocationRecord(
        schema_version=4,
        run_id=job.id,
        task_id=task_id,
        invocation_id=f'{task_id}:attempt-{attempt:04d}',
        role='issue_reviewer',
        agent_vendor='openai' if agent == 'codex' else 'anthropic',
        requested_model=model,
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
    write_record(record_path, pending)
    running = transition_attempt(pending, AttemptStatus.RUNNING)
    write_record(record_path, running)
    return record_path, running


def _finish_invocation(
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
    _write_text(Path(record.stdout_path), stdout)
    _write_text(Path(record.stderr_path), stderr)
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
    write_record(record_path, completed)


def run_issue_review(
    job: IssueJob,
    store: RunStore,
    runs_directory: Path,
    *,
    objective: str,
    agent: str,
    model: str | None,
    timeout: int,
    command: tuple[str, ...] = (),
) -> IssueJob:
    """Run one review bound to the latest immutable issue revision."""

    root = runs_directory.expanduser().resolve()
    job_directory = _evidence_path(root, job.id)
    if job_directory.is_symlink() or not job_directory.resolve().is_relative_to(root):
        message = 'job directory escapes the runs directory'
        raise IssueReviewError(message)
    captured_path = _evidence_path(job_directory, 'issue.json')
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
        job_directory, 'iterations', f'{job.iteration:06d}', 'result.json'
    )
    current_task_id = f'{job.id}:{job.iteration:06d}-issue_reviewer'
    current_attempts = [
        record
        for record in read_records(job_directory, job.id)
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
            'iterations',
            f'{job.iteration:06d}',
            'request.json',
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
    iteration_directory = _evidence_path(
        job_directory, 'iterations', f'{iteration:06d}'
    )
    source_path = _evidence_path(iteration_directory, 'issue.json')
    if retry:
        existing_source = IssueSourceSchema.model_validate(_read_json(source_path))
        if existing_source.source_digest != current.digest:
            message = 'stored issue snapshot does not match the retry source'
            raise IssueReviewError(message)
    else:
        write_snapshot(source_path, current)
    request_path = _evidence_path(iteration_directory, 'request.json')
    if retry:
        request = IssueReviewRequestSchema.model_validate(
            _read_json(request_path)
        ).model_dump(mode='json')
    else:
        prior = None
        for prior_iteration in range(job.iteration, 0, -1):
            prior_path = _evidence_path(
                job_directory,
                'iterations',
                f'{prior_iteration:06d}',
                'result.json',
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
    result_path = _evidence_path(iteration_directory, 'result.json')
    if result_path.exists() or result_path.is_symlink():
        message = 'issue review result path already exists'
        raise IssueReviewError(message)
    candidate_path = _evidence_path(
        iteration_directory, f'.candidate-result-{uuid4()}.json'
    )
    if not retry:
        _write_json(request_path, request)
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
        for record in read_records(job_directory, job.id)
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
            agent=agent,
            model=model,
            attempt=attempt,
        )
        execution = (
            _custom_review(command, request_path, candidate_path, timeout)
            if command
            else (
                CodexIssueReviewerAdapter(model).execute(request, timeout=timeout)
                if agent == 'codex'
                else ClaudeCodeIssueReviewerAdapter(model).execute(
                    request, timeout=timeout
                )
            )
        )
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
            _evidence_path(iteration_directory, 'feedback.md'),
            _render_feedback(result_document),
        )
        _finish_invocation(record_path, invocation, execution=execution)
        invocation_finished = True
        _write_json(result_path, result_document)
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
                record_path, invocation, execution=execution, error=error
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


def _publish_issue_feedback_locked(
    job: IssueJob, store: RunStore, runs_directory: Path
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
    job_directory = _evidence_path(root, job.id)
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
    job: IssueJob, store: RunStore, runs_directory: Path
) -> ProviderAction:
    """Serialize and publish accepted feedback once for an issue iteration."""

    root = runs_directory.expanduser().resolve()
    lock_path = _evidence_path(
        root, job.id, 'iterations', f'{job.iteration:06d}', '.publish.lock'
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.is_symlink():
        message = 'publication lock path is a symlink'
        raise IssueReviewError(message)
    with lock_path.open('a+', encoding='utf-8') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _publish_issue_feedback_locked(job, store, runs_directory)
