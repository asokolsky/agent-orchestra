"""Run canonical agent roles through a pinned, isolated OpenCode CLI."""

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
from agent_orchestra.adapter.errors import AdapterError
from agent_orchestra.adapter.issue_reviewer import (
    IssueReviewerError,
    issue_review_prompt,
)
from agent_orchestra.adapter.opencode_events import OpenCodeEventError, parse_events
from agent_orchestra.adapter.opencode_isolation import (
    isolated_environment,
    require_opencode,
    require_supported_version,
    sandbox_command,
)
from agent_orchestra.adapter.process import run_streaming_process
from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.evidence import JobEvidence
from agent_orchestra.manifests import adapter_arguments
from agent_orchestra.models import Finding, Review, Severity, Verdict
from agent_orchestra.reports import render_review
from agent_orchestra.runtime_metadata import write_runtime_metadata
from agent_orchestra.schemas import (
    DEVELOPER_RESULT_SCHEMA,
    ISSUE_REVIEW_RESULT_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    SchemaValidationError,
    validate_issue_review_result,
    validate_review_result,
)
from agent_orchestra.skill_install import skill_destination
from agent_orchestra.usage import RuntimeUsage, UsageStatus


class OpenCodeAdapterError(AdapterError):
    """Report an OpenCode role failure before canonical state changes."""

    def __init__(
        self,
        message: str,
        *,
        stdout: str = '',
        stderr: str = '',
        exit_code: int | None = None,
        timed_out: bool = False,
    ) -> None:
        """Retain available raw process evidence with the failure."""

        super().__init__(message, timed_out=timed_out)
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


def _stage_skill(name: str, scratch: Path) -> None:
    """Stage exactly the installed canonical skill in the isolated config home."""

    source = skill_destination('opencode', name)
    if not (source / 'SKILL.md').is_file():
        raise OpenCodeAdapterError(
            f'{name} is not installed; run '
            f'`agent-orchestra skills install --agent opencode --skill {name}`'
        )
    for path in source.rglob('*'):
        if path.is_symlink():
            raise OpenCodeAdapterError(f'installed skill contains a symlink: {path}')
    target = scratch / 'config' / 'opencode' / 'skills' / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)


def _run(
    role: RuntimeRole,
    prompt: str,
    directory: Path,
    timeout: int,
    *,
    model: str | None,
    variant: str | None,
    scratch: Path,
    external_read_paths: tuple[Path, ...] = (),
    review_base_sha: str | None = None,
) -> tuple[dict[str, Any], str, str, tuple[str, ...], RuntimeUsage]:
    """Run one bounded session and reject failed or partial machine output."""

    if timeout <= 0:
        msg = 'OpenCode timeout must be positive'
        raise OpenCodeAdapterError(msg, timed_out=True)
    if model is not None and (
        '/' not in model or model.startswith('/') or model.endswith('/')
    ):
        msg = 'OpenCode model must be provider/model'
        raise OpenCodeAdapterError(msg)
    executable = require_opencode()
    environment = isolated_environment(
        scratch,
        directory,
        role,
        external_read_paths=external_read_paths,
        review_base_sha=review_base_sha,
    )
    require_supported_version(executable, scratch, directory)
    command = [
        *sandbox_command(
            executable,
            scratch,
            writable_worktree=directory if role is RuntimeRole.DEVELOPER else None,
        ),
        *adapter_arguments('opencode', role),
    ]
    if model is not None:
        command.extend(['--model', model])
    if variant is not None:
        command.extend(['--variant', variant])
    try:
        completed = run_streaming_process(
            command, cwd=directory, env=environment, input=prompt, timeout=timeout
        )
    except subprocess.TimeoutExpired as error:
        msg = 'OpenCode timed out'
        raise OpenCodeAdapterError(
            msg,
            stdout=str(error.stdout or ''),
            stderr=str(error.stderr or ''),
            timed_out=True,
        ) from error
    if completed.returncode != 0:
        raise OpenCodeAdapterError(
            f'OpenCode exited with code {completed.returncode}',
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
        )
    try:
        parsed = parse_events(completed.stdout)
    except OpenCodeEventError as error:
        raise OpenCodeAdapterError(
            str(error),
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
        ) from error
    write_runtime_metadata(parsed.effective_models, usage=parsed.usage)
    return (
        parsed.value,
        completed.stdout,
        completed.stderr,
        parsed.effective_models,
        parsed.usage,
    )


def _read_reviewer_request(path: Path) -> dict[str, Any]:
    """Read one reviewer request without trusting malformed JSON."""

    try:
        request = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OpenCodeAdapterError(f'invalid reviewer request: {error}') from error
    if not isinstance(request, dict):
        msg = 'reviewer request must be a JSON object'
        raise OpenCodeAdapterError(msg)
    return request


def _review(request: dict[str, Any], result: dict[str, Any]) -> Review:
    """Build the human review artifact from a validated canonical result."""

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


def _write_review_artifact(path: Path, content: str, job_directory: Path) -> None:
    """Atomically finalize a verified review artifact in the run evidence log."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        JobEvidence.for_directory(job_directory).finalize_write(
            temporary, path, 'review_artifact'
        )
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class OpenCodeReviewerAdapter(ReviewerAdapter):
    """Review one exact diff with a read-only OpenCode process."""

    model: str | None = None
    effort: str | None = None

    def execute(self, request_path: Path, response_path: Path) -> None:
        """Persist a schema-validated and correlated review response."""

        request = _read_reviewer_request(request_path)
        try:
            directory = Path(request['scope']['worktree_path']).resolve()
            artifact = Path(request['payload']['artifact_path']).resolve()
            timeout = int(request['payload']['timeout_seconds'])
        except (KeyError, TypeError, ValueError) as error:
            msg = 'review request is missing paths'
            raise OpenCodeAdapterError(msg) from error
        if timeout <= 0:
            msg = 'OpenCode review timed out'
            raise OpenCodeAdapterError(msg, timed_out=True)
        job_directory = request_path.resolve().parent.parent
        if (
            request_path.resolve().parent.name != 'messages'
            or not artifact.is_relative_to(job_directory)
        ):
            msg = 'review artifact must be inside the run directory'
            raise OpenCodeAdapterError(msg)
        response_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix='agent-orchestra-opencode-review-'
        ) as directory_name:
            scratch = Path(directory_name).resolve()
            _stage_skill('agent-orchestra-reviewer', scratch)
            prompt = (
                'Use the agent-orchestra-reviewer skill to review this exact request. '
                'Keep the worktree read-only. Return only one JSON object matching '
                'the canonical review result schema. Use git status --short, '
                'git rev-parse HEAD, git ls-files --others --exclude-standard, '
                'and git diff --no-ext-diff --binary '
                + str(request['scope']['base_sha'])
                + ' to identify and inspect the complete changed file set. '
                'Read each untracked file listed by git.\n\n'
                'Result JSON Schema:\n'
                + json.dumps(REVIEW_RESULT_SCHEMA, indent=2)
                + '\n\nReview request:\n'
                + json.dumps(request, indent=2)
            )
            result, _, _, _, _ = _run(
                RuntimeRole.REVIEWER,
                prompt,
                directory,
                max(1, timeout - 5),
                model=self.model,
                variant=self.effort,
                scratch=scratch,
                review_base_sha=str(request['scope']['base_sha']),
            )
        try:
            validate_review_result(result)
        except SchemaValidationError as error:
            raise OpenCodeAdapterError(str(error)) from error
        _write_review_artifact(
            artifact, render_review(_review(request, result)), job_directory
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
            'payload': {**result, 'artifact_path': str(artifact)},
        }
        temporary = response_path.with_name(f'.{response_path.name}.{uuid4()}.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as file:
                json.dump(response, file, indent=2)
                file.write('\n')
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(response_path)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class OpenCodeDeveloperAdapter(DeveloperAdapter):
    """Produce a canonical remediation handoff under a scoped write boundary."""

    model: str | None = None
    effort: str | None = None

    def execute(self, request_path: Path, response_path: Path) -> None:
        """Run one isolated remediation assignment and persist its result."""

        request = read_request(request_path)
        try:
            directory = Path(request['scope']['worktree_path']).resolve()
            timeout = int(request['payload']['timeout_seconds'])
        except (KeyError, TypeError, ValueError) as error:
            msg = 'developer request is missing paths'
            raise DeveloperAdapterError(msg) from error
        if timeout <= 0:
            msg = 'OpenCode development timed out'
            raise DeveloperAdapterError(msg, timed_out=True)
        try:
            review_paths = tuple(
                Path(request['payload'][key]).resolve()
                for key in ('review_result_path', 'review_artifact_path')
            )
        except (KeyError, TypeError, ValueError) as error:
            msg = 'developer request is missing review paths'
            raise DeveloperAdapterError(msg) from error
        job_directory = request_path.resolve().parent.parent
        if any(not path.is_relative_to(job_directory) for path in review_paths):
            msg = 'developer review paths must be inside the run directory'
            raise DeveloperAdapterError(msg)
        try:
            review_result, review_artifact = (
                path.read_text(encoding='utf-8') for path in review_paths
            )
        except (OSError, UnicodeError) as error:
            msg = 'developer review evidence is unreadable'
            raise DeveloperAdapterError(msg) from error
        with tempfile.TemporaryDirectory(prefix='.opencode-developer-') as name:
            scratch = Path(name).resolve()
            _stage_skill('agent-orchestra-developer', scratch)
            result, _, _, _, _ = _run(
                RuntimeRole.DEVELOPER,
                developer_prompt(request, 'the agent-orchestra-developer skill')
                + '\nResult JSON Schema:\n'
                + json.dumps(DEVELOPER_RESULT_SCHEMA, indent=2)
                + '\n\nCanonical review result:\n'
                + review_result
                + '\n\nReview artifact:\n'
                + review_artifact,
                directory,
                max(1, timeout - 5),
                model=self.model,
                variant=self.effort,
                scratch=scratch,
                external_read_paths=review_paths,
            )
        write_handoff(response_path, request, result)


@dataclass(frozen=True, slots=True)
class OpenCodeIssueReviewerAdapter(IssueReviewerAdapter):
    """Review issue readiness with no filesystem or external tool permissions."""

    model: str | None = None
    effort: str | None = None

    def execute(self, request: dict[str, Any], *, timeout: int) -> IssueReviewExecution:
        """Return a validated issue review and raw process evidence."""

        with tempfile.TemporaryDirectory(prefix='.opencode-issue-review-') as name:
            root = Path(name).resolve()
            scratch = root / 'scratch'
            directory = root / 'cwd'
            scratch.mkdir()
            directory.mkdir()
            try:
                result, stdout, stderr, models, usage = _run(
                    RuntimeRole.ISSUE_REVIEWER,
                    issue_review_prompt(request)
                    + '\nResult JSON Schema:\n'
                    + json.dumps(ISSUE_REVIEW_RESULT_SCHEMA, indent=2),
                    directory,
                    timeout,
                    model=self.model,
                    variant=self.effort,
                    scratch=scratch,
                )
                validate_issue_review_result(result)
            except AdapterError as error:
                raise IssueReviewerError(
                    str(error),
                    stdout=error.stdout
                    if isinstance(error, OpenCodeAdapterError)
                    else '',
                    stderr=error.stderr
                    if isinstance(error, OpenCodeAdapterError)
                    else '',
                    exit_code=(
                        error.exit_code
                        if isinstance(error, OpenCodeAdapterError)
                        else None
                    ),
                    timed_out=error.timed_out,
                ) from error
            except SchemaValidationError as error:
                raise IssueReviewerError(
                    str(error),
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=0,
                    effective_models=models,
                    usage_status=UsageStatus.REPORTED,
                    usage=usage,
                ) from error
            return IssueReviewExecution(
                result=result,
                stdout=stdout,
                stderr=stderr,
                exit_code=0,
                effective_models=models,
                usage_status=UsageStatus.REPORTED,
                usage=usage,
            )


def main(argv: list[str] | None = None) -> int:
    """Dispatch one command-line reviewer or developer role adapter."""

    parser = argparse.ArgumentParser(prog='agent-orchestra-opencode-reviewer')
    parser.add_argument(
        '--role',
        type=RuntimeRole,
        choices=tuple(RuntimeRole),
        default=RuntimeRole.REVIEWER,
    )
    parser.add_argument('--model')
    parser.add_argument('--effort')
    parser.add_argument('request', type=Path)
    parser.add_argument('response', type=Path)
    parsed = parser.parse_args(argv)
    try:
        if parsed.role is RuntimeRole.REVIEWER:
            OpenCodeReviewerAdapter(parsed.model, parsed.effort).execute(
                parsed.request, parsed.response
            )
        elif parsed.role is RuntimeRole.DEVELOPER:
            OpenCodeDeveloperAdapter(parsed.model, parsed.effort).execute(
                parsed.request, parsed.response
            )
        else:
            parser.error(f'unsupported adapter role: {parsed.role}')
    except (AdapterError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        if isinstance(error, AdapterError) and error.timed_out:
            write_runtime_metadata((), timed_out=True)
        return 2
    return 0


def developer_main(argv: list[str] | None = None) -> int:
    """Dispatch the OpenCode developer entry point."""

    arguments = list(argv) if argv is not None else sys.argv[1:]
    return main(['--role', 'developer', *arguments])


if __name__ == '__main__':
    raise SystemExit(main())
