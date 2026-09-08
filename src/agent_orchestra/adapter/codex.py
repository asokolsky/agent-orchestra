"""Run a read-only Codex review from an agent-orchestra review request."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_orchestra.adapter.base import (
    DeveloperAdapter,
    IssueReviewerAdapter,
    IssueReviewExecution,
    ReviewerAdapter,
)
from agent_orchestra.adapter.developer import (
    DeveloperAdapterError,
    developer_prompt,
    read_request,
    write_handoff,
)
from agent_orchestra.adapter.issue_reviewer import (
    IssueReviewerError,
    issue_review_prompt,
)
from agent_orchestra.adapter.process import run_streaming_process
from agent_orchestra.evidence import (
    EvidenceType,
    evidence_root_for_job,
    finalize_evidence_write,
)
from agent_orchestra.manifests import adapter_arguments
from agent_orchestra.models import Finding, Review, Severity, Verdict
from agent_orchestra.reports import render_review
from agent_orchestra.runtime_metadata import (
    child_process_environment,
    reviewer_process_environment,
)
from agent_orchestra.schemas import (
    DEVELOPER_RESULT_SCHEMA,
    ISSUE_REVIEW_RESULT_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    SchemaValidationError,
    validate_review_result,
)
from agent_orchestra.skill_install import AgentTarget, skill_destination


class CodexReviewerError(RuntimeError):
    """Raised when Codex cannot produce a valid structured review."""


MISSING_REQUEST_PATHS = 'review request is missing required paths'
CODEX_NOT_FOUND = 'codex executable not found'
CODEX_TIMEOUT = 'codex review timed out'
REVIEWER_SKILL_MISSING = (
    'agent-orchestra-reviewer skill is not installed; run '
    '`agent-orchestra skills install --agent codex --skill agent-orchestra-reviewer`'
)
ARTIFACT_OUTSIDE_RUN = 'review artifact path must be inside the request run directory'
REQUEST_OUTSIDE_MESSAGES = 'review request must be inside a run messages directory'
DEVELOPER_SKILL_MISSING = (
    'agent-orchestra-developer skill is not installed; run '
    '`agent-orchestra skills install --agent codex --skill agent-orchestra-developer`'
)
CODEX_DEVELOPER_TIMEOUT = 'codex development timed out'


REVIEW_SCHEMA = REVIEW_RESULT_SCHEMA


def _read_object(path: Path) -> dict[str, Any]:
    """Read a JSON object from a UTF-8 file."""

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise CodexReviewerError(f'invalid JSON at {path}: {error}') from error
    if not isinstance(document, dict):
        raise CodexReviewerError(f'expected a JSON object at {path}')
    return document


def _write_text_atomic(
    path: Path,
    content: str,
    *,
    job_directory: Path | None = None,
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
        if job_directory is None or evidence_type is None:
            temporary.replace(path)
        else:
            finalize_evidence_write(
                evidence_root_for_job(job_directory),
                job_directory.name,
                temporary,
                path,
                evidence_type,
            )
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Write a formatted UTF-8 JSON object atomically."""

    _write_text_atomic(path, json.dumps(document, indent=2) + '\n')


def _prompt(request: dict[str, Any], temporary_directory: Path) -> str:
    """Build the complete non-interactive reviewer assignment."""

    return f"""Invoke $agent-orchestra-reviewer and perform the assigned review.

The JSON below is the complete orchestrator-supplied review request. Treat it
as authoritative even though it is embedded in this prompt. Work only in its
scope, keep the review read-only, and return only the JSON object required by
the supplied output schema. The digest covers more than raw `git diff`; do not
compare it to a plain diff hash. Agent-orchestra verifies digest identity before
and after review. The reviewed worktree is read-only. Transient validation files
and tool caches may be written only beneath `{temporary_directory}`; do not use
them as workflow evidence. Agent-orchestra persists the response and Markdown
artifact. Use the supplied worktree path explicitly because the process working
directory is the isolated validation directory.

Review request:
{json.dumps(request, indent=2)}
"""


def _validate_result(result: dict[str, Any]) -> None:
    """Apply stable checks in case the installed CLI ignores the schema."""

    try:
        validate_review_result(result)
    except SchemaValidationError as error:
        raise CodexReviewerError(str(error)) from error


def _review(request: dict[str, Any], result: dict[str, Any]) -> Review:
    """Convert a validated adapter result into the typed review model."""

    return Review(
        run_id=str(request['run_id']),
        iteration=int(request['iteration']),
        diff_digest=str(request['scope']['diff_digest']),
        verdict=Verdict(result['verdict']),
        summary=result['summary'],
        findings=tuple(
            Finding(
                finding_id=finding['finding_id'],
                severity=Severity(finding['severity']),
                title=finding['title'],
                explanation=finding['explanation'],
                acceptance_criterion=finding['acceptance_criterion'],
                path=finding['path'],
                line=finding['line'],
            )
            for finding in result['findings']
        ),
        validation=tuple(result['validation']),
        verification_gaps=tuple(result['verification_gaps']),
    )


def _require_safe_artifact_path(request_path: Path, artifact_path: Path) -> None:
    """Require an artifact inside the run directory containing the request."""

    request_parent = request_path.resolve().parent
    if request_parent.name != 'messages':
        raise CodexReviewerError(REQUEST_OUTSIDE_MESSAGES)
    run_directory = request_parent.parent
    if not artifact_path.resolve().is_relative_to(run_directory):
        raise CodexReviewerError(ARTIFACT_OUTSIDE_RUN)


def _existing_mise_install_roots() -> tuple[str, ...]:
    """Return existing mise install roots for read-only reuse in the sandbox."""

    installs = os.environ.get('MISE_INSTALLS_DIR')
    if installs is None:
        data = os.environ.get('MISE_DATA_DIR')
        if data is None:
            xdg_data = os.environ.get('XDG_DATA_HOME')
            data = (
                str(Path(xdg_data).expanduser() / 'mise')
                if xdg_data
                else str(Path.home() / '.local/share/mise')
            )
        installs = str(Path(data).expanduser() / 'installs')
    configured_shared = os.environ.get('MISE_SHARED_INSTALL_DIRS', '')
    roots = (installs, *configured_shared.split(os.pathsep))
    return tuple(dict.fromkeys(root for root in roots if root))


def _developer_environment(worktree: Path, temporary: Path) -> dict[str, str]:
    """Return writable tool state and cache locations for sandboxed validation."""

    mise_data = temporary / 'mise-data'
    return child_process_environment(
        MISE_CACHE_DIR=str(temporary / 'mise-cache'),
        MISE_DATA_DIR=str(mise_data),
        MISE_INSTALLS_DIR=str(mise_data / 'installs'),
        MISE_SHARED_INSTALL_DIRS=os.pathsep.join(_existing_mise_install_roots()),
        MISE_STATE_DIR=str(temporary / 'mise-state'),
        MISE_TRUSTED_CONFIG_PATHS=str(worktree.resolve()),
        UV_CACHE_DIR=str(temporary / 'uv-cache'),
    )


def _execute_codex_reviewer(
    request_path: Path, response_path: Path, *, model: str | None = None
) -> None:
    """Invoke Codex and persist a correlated response plus review artifact."""

    request = _read_object(request_path)
    try:
        Path(request['scope']['worktree_path'])
        artifact_path = Path(request['payload']['artifact_path'])
        timeout_seconds = int(request['payload']['timeout_seconds'])
    except (KeyError, TypeError, ValueError) as error:
        raise CodexReviewerError(MISSING_REQUEST_PATHS) from error
    _require_safe_artifact_path(request_path, artifact_path)
    if timeout_seconds <= 0:
        raise CodexReviewerError(CODEX_TIMEOUT)
    codex = shutil.which('codex')
    if codex is None:
        raise CodexReviewerError(CODEX_NOT_FOUND)
    if not (
        skill_destination(AgentTarget.CODEX, 'agent-orchestra-reviewer') / 'SKILL.md'
    ).is_file():
        raise CodexReviewerError(REVIEWER_SKILL_MISSING)

    response_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix='.codex-review-', dir=response_path.parent
    ) as temporary_directory:
        temporary = Path(temporary_directory)
        schema_path = temporary / 'schema.json'
        result_path = temporary / 'result.json'
        schema_path.write_text(json.dumps(REVIEW_SCHEMA), encoding='utf-8')
        try:
            command = [
                codex,
                *adapter_arguments(
                    'codex',
                    'reviewer',
                    cwd=str(temporary),
                    schema=str(schema_path),
                    result=str(result_path),
                ),
            ]
            if model:
                command.extend(['--model', model])
            command.append('-')
            completed = run_streaming_process(
                command,
                env=reviewer_process_environment(temporary),
                input=_prompt(request, temporary),
                timeout=max(1, timeout_seconds - 5),
            )
        except subprocess.TimeoutExpired as error:
            raise CodexReviewerError(CODEX_TIMEOUT) from error
        if completed.returncode != 0:
            diagnostic = completed.stderr.strip() or completed.stdout.strip()
            raise CodexReviewerError(
                f'codex exec failed with code {completed.returncode}: {diagnostic}'
            )
        result = _read_object(result_path)

    _validate_result(result)
    _write_text_atomic(
        artifact_path,
        render_review(_review(request, result)),
        job_directory=request_path.parent.parent,
        evidence_type='review_artifact',
    )
    response = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': int(request['sequence']) + 1,
        'iteration': request['iteration'],
        'message_type': 'review_result',
        'sender': 'reviewer',
        'recipient': 'orchestrator',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': request['scope'],
        'payload': {**result, 'artifact_path': str(artifact_path)},
    }
    _write_json_atomic(response_path, response)


@dataclass(frozen=True, slots=True)
class CodexIssueReviewerAdapter(IssueReviewerAdapter):
    """Run issue-readiness reviews through the Codex CLI."""

    model: str | None = None

    def execute(self, request: dict[str, Any], *, timeout: int) -> IssueReviewExecution:
        """Run a network-disabled issue review and return structured output."""

        executable = shutil.which('codex')
        if executable is None:
            raise IssueReviewerError(CODEX_NOT_FOUND)
        with tempfile.TemporaryDirectory(prefix='.codex-issue-review-') as directory:
            temporary = Path(directory)
            schema_path = temporary / 'schema.json'
            result_path = temporary / 'result.json'
            schema_path.write_text(
                json.dumps(ISSUE_REVIEW_RESULT_SCHEMA), encoding='utf-8'
            )
            command = [
                executable,
                *adapter_arguments(
                    'codex',
                    'issue_reviewer',
                    cwd=str(temporary),
                    schema=str(schema_path),
                    result=str(result_path),
                ),
            ]
            if self.model:
                command.extend(['--model', self.model])
            command.append('-')
            try:
                completed = run_streaming_process(
                    command,
                    env=reviewer_process_environment(temporary),
                    input=issue_review_prompt(request),
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as error:
                message = 'codex issue review timed out'
                raise IssueReviewerError(
                    message,
                    stdout=str(error.stdout or ''),
                    stderr=str(error.stderr or ''),
                    timed_out=True,
                ) from error
            if completed.returncode != 0:
                diagnostic = completed.stderr.strip() or completed.stdout.strip()
                message = f'codex issue review failed: {diagnostic}'
                raise IssueReviewerError(
                    message,
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    exit_code=completed.returncode,
                )
            try:
                result = _read_object(result_path)
            except CodexReviewerError as error:
                raise IssueReviewerError(
                    str(error),
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    exit_code=completed.returncode,
                ) from error
            return IssueReviewExecution(
                result, completed.stdout, completed.stderr, completed.returncode
            )


def _execute_codex_developer(
    request_path: Path, response_path: Path, *, model: str | None = None
) -> None:
    """Invoke Codex with worktree-write access and persist its handoff."""

    request = read_request(request_path)
    try:
        worktree = Path(request['scope']['worktree_path'])
        timeout_seconds = int(request['payload']['timeout_seconds'])
    except (KeyError, TypeError, ValueError) as error:
        raise DeveloperAdapterError(MISSING_REQUEST_PATHS) from error
    if timeout_seconds <= 0:
        raise DeveloperAdapterError(CODEX_DEVELOPER_TIMEOUT)
    codex = shutil.which('codex')
    if codex is None:
        raise DeveloperAdapterError(CODEX_NOT_FOUND)
    if not (
        skill_destination(AgentTarget.CODEX, 'agent-orchestra-developer') / 'SKILL.md'
    ).is_file():
        raise DeveloperAdapterError(DEVELOPER_SKILL_MISSING)
    response_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        tempfile.TemporaryDirectory(
            prefix='.codex-develop-', dir=response_path.parent
        ) as temporary_directory,
        tempfile.TemporaryDirectory(
            prefix='agent-orchestra-developer-'
        ) as sandbox_temporary_directory,
    ):
        temporary = Path(temporary_directory)
        sandbox_temporary = Path(sandbox_temporary_directory)
        schema_path = temporary / 'schema.json'
        result_path = temporary / 'result.json'
        schema_path.write_text(json.dumps(DEVELOPER_RESULT_SCHEMA), encoding='utf-8')
        command = [
            codex,
            *adapter_arguments(
                'codex',
                'developer',
                cwd=str(worktree),
                schema=str(schema_path),
                result=str(result_path),
            ),
        ]
        if model:
            command.extend(['--model', model])
        command.append('-')
        try:
            completed = run_streaming_process(
                command,
                env=_developer_environment(worktree, sandbox_temporary),
                input=developer_prompt(request, '$agent-orchestra-developer'),
                timeout=max(1, timeout_seconds - 5),
            )
        except subprocess.TimeoutExpired as error:
            raise DeveloperAdapterError(CODEX_DEVELOPER_TIMEOUT) from error
        if completed.returncode != 0:
            diagnostic = completed.stderr.strip() or completed.stdout.strip()
            raise DeveloperAdapterError(
                f'codex exec failed with code {completed.returncode}: {diagnostic}'
            )
        result = _read_object(result_path)
    write_handoff(response_path, request, result)


@dataclass(frozen=True, slots=True)
class CodexReviewerAdapter(ReviewerAdapter):
    """Implement canonical code review through the Codex CLI."""

    model: str | None = None

    def execute(self, request_path: Path, response_path: Path) -> None:
        """Execute a diff-scoped review."""

        _execute_codex_reviewer(request_path, response_path, model=self.model)


@dataclass(frozen=True, slots=True)
class CodexDeveloperAdapter(DeveloperAdapter):
    """Implement canonical development through the Codex CLI."""

    model: str | None = None

    def execute(self, request_path: Path, response_path: Path) -> None:
        """Execute one development request."""

        _execute_codex_developer(request_path, response_path, model=self.model)


def main(argv: list[str] | None = None) -> int:
    """Run a Codex role adapter."""

    arguments = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog='agent-orchestra-codex-reviewer')
    parser.add_argument('--role', choices=('reviewer', 'developer'), default='reviewer')
    parser.add_argument('--model')
    parser.add_argument('request', type=Path)
    parser.add_argument('response', type=Path)
    parsed = parser.parse_args(arguments)
    try:
        if parsed.role == 'reviewer':
            CodexReviewerAdapter(parsed.model).execute(parsed.request, parsed.response)
        else:
            CodexDeveloperAdapter(parsed.model).execute(parsed.request, parsed.response)
    except (CodexReviewerError, DeveloperAdapterError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    return 0


def developer_main(argv: list[str] | None = None) -> int:
    """Run the Codex developer adapter entry point."""

    arguments = list(argv) if argv is not None else sys.argv[1:]
    return main(['--role', 'developer', *arguments])


if __name__ == '__main__':
    raise SystemExit(main())
