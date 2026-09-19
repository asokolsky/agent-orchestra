"""Run a read-only Claude Code review from an orchestration request."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
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
from agent_orchestra.adapter.process import run_streaming_process
from agent_orchestra.evidence import (
    EvidenceType,
    JobEvidence,
)
from agent_orchestra.manifests import adapter_arguments
from agent_orchestra.models import Finding, Review, Severity, Verdict
from agent_orchestra.reports import render_review
from agent_orchestra.runtime_metadata import (
    PROVIDER_BUDGET_EXHAUSTED,
    PROVIDER_EXECUTION_FAILED,
    STRUCTURED_OUTPUT_EXHAUSTED,
    TURN_LIMIT_EXHAUSTED,
    child_process_environment,
    reviewer_process_environment,
    write_runtime_metadata,
)
from agent_orchestra.schemas import (
    DEVELOPER_RESULT_SCHEMA,
    ISSUE_REVIEW_RESULT_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    SchemaValidationError,
    validate_review_result,
)
from agent_orchestra.skill_install import skill_destination
from agent_orchestra.usage import ModelUsage, RuntimeUsage, UsageStatus, UsageValues


class ClaudeCodeReviewerError(AdapterError):
    """Raised when Claude Code cannot produce a valid structured review."""


CLAUDE_CODE_NOT_FOUND = 'claude executable not found'
CLAUDE_CODE_TIMEOUT = 'claude-code review timed out'
MISSING_STRUCTURED_OUTPUT = 'claude-code response has no structured_output object'
CLAUDE_STRUCTURED_OUTPUT_EXHAUSTED = 'error_max_structured_output_retries'
CLAUDE_TURN_LIMIT_EXHAUSTED = 'error_max_turns'
CLAUDE_BUDGET_EXHAUSTED = 'error_max_budget_usd'
CLAUDE_EXECUTION_FAILED = 'error_during_execution'
CLAUDE_FAILURES = {
    CLAUDE_STRUCTURED_OUTPUT_EXHAUSTED: (
        STRUCTURED_OUTPUT_EXHAUSTED,
        'claude-code exhausted structured-output retries',
    ),
    CLAUDE_TURN_LIMIT_EXHAUSTED: (
        TURN_LIMIT_EXHAUSTED,
        'claude-code exhausted its turn limit',
    ),
    CLAUDE_BUDGET_EXHAUSTED: (
        PROVIDER_BUDGET_EXHAUSTED,
        'claude-code exhausted its budget limit',
    ),
    CLAUDE_EXECUTION_FAILED: (
        PROVIDER_EXECUTION_FAILED,
        'claude-code failed during execution',
    ),
}
REVIEWER_SKILL_MISSING = (
    'agent-orchestra-reviewer skill is not installed; run '
    '`agent-orchestra skills install --agent claude-code '
    '--skill agent-orchestra-reviewer`'
)
ARTIFACT_OUTSIDE_RUN = 'review artifact path must be inside the request run directory'
REQUEST_OUTSIDE_MESSAGES = 'review request must be inside a run messages directory'
MISSING_REQUEST_PATHS = 'review request is missing required paths'
DEVELOPER_SKILL_MISSING = (
    'agent-orchestra-developer skill is not installed; run '
    '`agent-orchestra skills install --agent claude-code '
    '--skill agent-orchestra-developer`'
)
CLAUDE_CODE_DEVELOPER_TIMEOUT = 'claude-code development timed out'
SKILL_STAGING_FAILED = 'cannot stage Claude role skill'
CLAUDE_SKILL_PLUGIN = 'agent-orchestra-runtime'
CLAUDE_REVIEWER_SKILL = f'{CLAUDE_SKILL_PLUGIN}:agent-orchestra-reviewer'
CLAUDE_DEVELOPER_SKILL = f'{CLAUDE_SKILL_PLUGIN}:agent-orchestra-developer'


def _developer_settings() -> str:
    """Return isolated settings that confine developer writes to the worktree."""

    return json.dumps(
        {
            'sandbox': {
                'enabled': True,
                'failIfUnavailable': True,
                'allowUnsandboxedCommands': False,
            }
        },
        separators=(',', ':'),
    )


def _stage_skill(source: Path, temporary_directory: Path) -> Path:
    """Create an invocation-local plugin containing exactly one installed skill."""

    for path in source.rglob('*'):
        if path.is_symlink():
            raise OSError(f'installed skill contains a symbolic link: {path}')
    plugin = temporary_directory / 'agent-orchestra-skill-plugin'
    manifest = plugin / '.claude-plugin' / 'plugin.json'
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                'name': CLAUDE_SKILL_PLUGIN,
                'version': '1.0.0',
                'description': 'Invocation-local Agent Orchestra role skill.',
            },
            separators=(',', ':'),
        )
        + '\n',
        encoding='utf-8',
    )
    destination = plugin / 'skills' / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    return plugin


def _reviewer_settings(worktree: Path, temporary_directory: Path) -> str:
    """Return isolated settings that make the reviewer sandbox read-only."""

    return json.dumps(
        {
            'sandbox': {
                'enabled': True,
                'failIfUnavailable': True,
                'allowUnsandboxedCommands': False,
                'filesystem': {
                    'allowWrite': [str(temporary_directory.resolve())],
                    'denyWrite': [str(worktree.resolve())],
                },
            }
        },
        separators=(',', ':'),
    )


def _read_object(path: Path) -> dict[str, Any]:
    """Read one UTF-8 JSON object."""

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ClaudeCodeReviewerError(f'invalid JSON at {path}: {error}') from error
    if not isinstance(document, dict):
        raise ClaudeCodeReviewerError(f'expected a JSON object at {path}')
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
            JobEvidence.for_directory(job_directory).finalize_write(
                temporary, path, evidence_type
            )
    finally:
        temporary.unlink(missing_ok=True)


def _prompt(request: dict[str, Any], temporary_directory: Path) -> str:
    """Build the complete non-interactive Claude Code assignment."""

    return f"""Use the Skill tool with name `{CLAUDE_REVIEWER_SKILL}` and perform the assigned review.

The JSON below is the complete orchestrator-supplied review request. Treat it
as authoritative. Work only in its scope, keep the review read-only, and return
the structured object required by the supplied JSON Schema. The digest covers
more than raw `git diff`; do not compare it to a plain diff hash.
Agent-orchestra verifies digest identity before and after review. The reviewed
worktree is read-only. Transient validation files and tool caches may be written
only beneath `{temporary_directory}`; do not use them as workflow evidence.
Agent-orchestra persists the result. When shell inspection is needed, use only
the pre-approved Git commands. Network access and project validation commands
are unavailable in this reviewer environment. Do not attempt network commands
such as `curl`; do not run `mise trust`, install dependencies, or change
configuration. Record any check that requires those capabilities in
`verification_gaps`.

Review request:
{json.dumps(request, indent=2)}
"""


def _require_safe_artifact_path(request_path: Path, artifact_path: Path) -> None:
    """Require an artifact inside the run directory containing the request."""

    request_parent = request_path.resolve().parent
    if request_parent.name != 'messages':
        raise ClaudeCodeReviewerError(REQUEST_OUTSIDE_MESSAGES)
    if not artifact_path.resolve().is_relative_to(request_parent.parent):
        raise ClaudeCodeReviewerError(ARTIFACT_OUTSIDE_RUN)


def _review(request: dict[str, Any], result: dict[str, Any]) -> Review:
    """Convert a validated result into the typed review model."""

    return Review(
        run_id=str(request['run_id']),
        iteration=int(request['iteration']),
        diff_digest=str(request['scope']['diff_digest']),
        verdict=Verdict(result['verdict']),
        summary=result['summary'],
        findings=tuple(
            Finding(
                finding_id=item['finding_id'],
                severity=Severity(item['severity']),
                title=item['title'],
                explanation=item['explanation'],
                acceptance_criterion=item['acceptance_criterion'],
                path=item['path'],
                line=item['line'],
            )
            for item in result['findings']
        ),
        validation=tuple(result['validation']),
        verification_gaps=tuple(result['verification_gaps']),
    )


def _effective_models(output: dict[str, Any]) -> tuple[str, ...]:
    """Return model identities reported by Claude Code's JSON result."""

    usage = output.get('modelUsage')
    if not isinstance(usage, dict):
        return ()
    return tuple(model for model in usage if isinstance(model, str) and model)


def _token_count(value: object) -> int | None:
    """Return one usable token count without accepting booleans or negatives."""

    return value if type(value) is int and value >= 0 else None


def _cost(value: object) -> float | None:
    """Return one finite nonnegative reported cost."""

    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    normalized = float(value)
    return normalized if normalized >= 0 and math.isfinite(normalized) else None


def _usage_values(document: dict[str, Any], *, camel_case: bool) -> UsageValues | None:
    """Normalize the cross-runtime values from one Claude usage scope."""

    input_name = 'inputTokens' if camel_case else 'input_tokens'
    output_name = 'outputTokens' if camel_case else 'output_tokens'
    creation_name = (
        'cacheCreationInputTokens' if camel_case else 'cache_creation_input_tokens'
    )
    read_name = 'cacheReadInputTokens' if camel_case else 'cache_read_input_tokens'
    cost_name = 'costUSD' if camel_case else 'total_cost_usd'
    values = UsageValues(
        input_tokens=_token_count(document.get(input_name)),
        output_tokens=_token_count(document.get(output_name)),
        cache_creation_input_tokens=_token_count(document.get(creation_name)),
        cache_read_input_tokens=_token_count(document.get(read_name)),
        total_cost_usd=_cost(document.get(cost_name)),
    )
    return (
        values if any(value is not None for value in asdict(values).values()) else None
    )


def _runtime_usage(output: dict[str, Any]) -> RuntimeUsage | None:
    """Normalize usable aggregate and per-model values from a Claude envelope."""

    aggregate_document = output.get('usage')
    totals = (
        _usage_values(aggregate_document, camel_case=False)
        if isinstance(aggregate_document, dict)
        else None
    )
    total_cost = _cost(output.get('total_cost_usd'))
    if total_cost is not None:
        totals = UsageValues(
            input_tokens=totals.input_tokens if totals is not None else None,
            output_tokens=totals.output_tokens if totals is not None else None,
            cache_creation_input_tokens=(
                totals.cache_creation_input_tokens if totals is not None else None
            ),
            cache_read_input_tokens=(
                totals.cache_read_input_tokens if totals is not None else None
            ),
            total_cost_usd=total_cost,
        )
    models: list[ModelUsage] = []
    model_document = output.get('modelUsage')
    if isinstance(model_document, dict):
        for model, values_document in model_document.items():
            if (
                not isinstance(model, str)
                or not model
                or not isinstance(values_document, dict)
            ):
                continue
            values = _usage_values(values_document, camel_case=True)
            if values is not None:
                models.append(ModelUsage(model=model, values=values))
    turn_count = _token_count(output.get('num_turns'))
    if turn_count is None and totals is None and not models:
        return None
    return RuntimeUsage(turn_count=turn_count, totals=totals, models=tuple(models))


def _claude_failure(output: dict[str, Any]) -> tuple[str | None, str | None]:
    """Classify one documented Claude result failure with safe diagnostics."""

    subtype = output.get('subtype')
    if not isinstance(subtype, str):
        return None, None
    classified = CLAUDE_FAILURES.get(subtype)
    if classified is None:
        return None, None
    failure_code, message = classified
    errors = output.get('errors')
    details = (
        tuple(item for item in errors if isinstance(item, str) and item)
        if isinstance(errors, list)
        else ()
    )
    if details:
        message = f'{message}: {"; ".join(details)}'
    diagnostics: list[str] = []
    turns = output.get('num_turns')
    if type(turns) is int and turns >= 0:
        diagnostics.append(f'num_turns={turns}')
    cost = output.get('total_cost_usd')
    if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
        diagnostics.append(f'total_cost_usd={cost}')
    denials = output.get('permission_denials')
    if isinstance(denials, list):
        tools = tuple(
            item['tool_name']
            for item in denials
            if isinstance(item, dict)
            and isinstance(item.get('tool_name'), str)
            and item['tool_name']
        )
        if tools:
            diagnostics.append(f'permission_denials={",".join(dict.fromkeys(tools))}')
    if diagnostics:
        message = f'{message}; {"; ".join(diagnostics)}'
    return failure_code, message


def _output_with_runtime_metadata(stdout: str) -> dict[str, Any] | None:
    """Parse a Claude Code envelope and report any model identities it contains."""

    try:
        output = json.loads(stdout)
    except json.JSONDecodeError, TypeError:
        return None
    if not isinstance(output, dict):
        return None
    failure_code, failure_message = _claude_failure(output)
    write_runtime_metadata(
        _effective_models(output),
        failure_code=failure_code,
        failure_message=failure_message,
        usage=_runtime_usage(output),
    )
    return output


def _execute_claude_code_reviewer(
    request_path: Path, response_path: Path, *, model: str | None = None
) -> None:
    """Invoke Claude Code and persist a correlated canonical review response."""

    request = _read_object(request_path)
    try:
        worktree = Path(request['scope']['worktree_path'])
        artifact_path = Path(request['payload']['artifact_path'])
        timeout_seconds = int(request['payload']['timeout_seconds'])
    except (KeyError, TypeError, ValueError) as error:
        raise ClaudeCodeReviewerError(MISSING_REQUEST_PATHS) from error
    _require_safe_artifact_path(request_path, artifact_path)
    if timeout_seconds <= 0:
        raise ClaudeCodeReviewerError(CLAUDE_CODE_TIMEOUT, timed_out=True)
    executable = shutil.which('claude')
    if executable is None:
        raise ClaudeCodeReviewerError(CLAUDE_CODE_NOT_FOUND)
    skill = skill_destination('claude-code', 'agent-orchestra-reviewer')
    if not (skill / 'SKILL.md').is_file():
        raise ClaudeCodeReviewerError(REVIEWER_SKILL_MISSING)

    response_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix='.claude-review-', dir=response_path.parent
    ) as temporary_directory:
        temporary = Path(temporary_directory)
        try:
            plugin = _stage_skill(skill, temporary)
        except OSError as error:
            raise ClaudeCodeReviewerError(f'{SKILL_STAGING_FAILED}: {error}') from error
        command = [
            executable,
            *adapter_arguments(
                'claude-code',
                'reviewer',
                settings=_reviewer_settings(worktree, temporary),
                schema=json.dumps(REVIEW_RESULT_SCHEMA, separators=(',', ':')),
            ),
            '--plugin-dir',
            str(plugin),
            '--allowedTools',
            'Read',
            'Glob',
            'Grep',
            f'Skill({CLAUDE_REVIEWER_SKILL})',
            'Bash(git status --short)',
            'Bash(git rev-parse HEAD)',
            'Bash(git diff --no-ext-diff --binary HEAD)',
            'Bash(git ls-files --others --exclude-standard)',
        ]
        if model:
            command.extend(['--model', model])
        try:
            completed = run_streaming_process(
                command,
                cwd=worktree,
                env=reviewer_process_environment(
                    temporary, CLAUDE_CODE_SUBPROCESS_ENV_SCRUB='1'
                ),
                input=_prompt(request, temporary),
                timeout=max(1, timeout_seconds - 5),
            )
        except subprocess.TimeoutExpired as error:
            raise ClaudeCodeReviewerError(
                CLAUDE_CODE_TIMEOUT, timed_out=True
            ) from error
    output = _output_with_runtime_metadata(completed.stdout)
    if completed.returncode != 0:
        if output is not None:
            _, failure_message = _claude_failure(output)
            if failure_message is not None:
                raise ClaudeCodeReviewerError(failure_message)
        diagnostic = completed.stderr.strip() or completed.stdout.strip()
        raise ClaudeCodeReviewerError(
            f'claude-code failed with code {completed.returncode}: {diagnostic}'
        )
    if output is None:
        raise ClaudeCodeReviewerError(MISSING_STRUCTURED_OUTPUT)
    try:
        result = output['structured_output']
    except (KeyError, TypeError) as error:
        raise ClaudeCodeReviewerError(MISSING_STRUCTURED_OUTPUT) from error
    if not isinstance(result, dict):
        raise ClaudeCodeReviewerError(MISSING_STRUCTURED_OUTPUT)
    try:
        validate_review_result(result)
    except SchemaValidationError as error:
        raise ClaudeCodeReviewerError(str(error)) from error

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
    _write_text_atomic(response_path, json.dumps(response, indent=2) + '\n')


@dataclass(frozen=True, slots=True)
class ClaudeCodeIssueReviewerAdapter(IssueReviewerAdapter):
    """Run issue-readiness reviews through the Claude Code CLI."""

    model: str | None = None

    def execute(self, request: dict[str, Any], *, timeout: int) -> IssueReviewExecution:
        """Run an isolated issue review and return structured output."""

        executable = shutil.which('claude')
        if executable is None:
            raise IssueReviewerError(CLAUDE_CODE_NOT_FOUND)
        with tempfile.TemporaryDirectory(prefix='.claude-issue-review-') as directory:
            temporary = Path(directory)
            command = [
                executable,
                *adapter_arguments(
                    'claude-code',
                    'issue_reviewer',
                    settings=_developer_settings(),
                    schema=json.dumps(
                        ISSUE_REVIEW_RESULT_SCHEMA, separators=(',', ':')
                    ),
                ),
            ]
            if self.model:
                command.extend(['--model', self.model])
            try:
                completed = run_streaming_process(
                    command,
                    cwd=temporary,
                    env=reviewer_process_environment(
                        temporary, CLAUDE_CODE_SUBPROCESS_ENV_SCRUB='1'
                    ),
                    input=issue_review_prompt(request),
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired as error:
                message = 'claude-code issue review timed out'
                raise IssueReviewerError(
                    message,
                    stdout=str(error.stdout or ''),
                    stderr=str(error.stderr or ''),
                    timed_out=True,
                ) from error
        output = _output_with_runtime_metadata(completed.stdout)
        usage = _runtime_usage(output) if output is not None else None
        models = _effective_models(output) if output is not None else ()
        if completed.returncode != 0:
            diagnostic = completed.stderr.strip() or completed.stdout.strip()
            message = f'claude-code issue review failed: {diagnostic}'
            raise IssueReviewerError(
                message,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.returncode,
                effective_models=models,
                usage_status=(
                    UsageStatus.REPORTED
                    if usage is not None
                    else UsageStatus.UNAVAILABLE
                ),
                usage=usage,
            )
        if output is None or not isinstance(output.get('structured_output'), dict):
            message = 'claude-code returned no structured issue review'
            raise IssueReviewerError(
                message,
                stdout=completed.stdout,
                stderr=completed.stderr,
                exit_code=completed.returncode,
                effective_models=models,
                usage_status=(
                    UsageStatus.REPORTED
                    if usage is not None
                    else UsageStatus.UNAVAILABLE
                ),
                usage=usage,
            )
        result: dict[str, Any] = output['structured_output']
        return IssueReviewExecution(
            result=result,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            effective_models=models,
            usage_status=(
                UsageStatus.REPORTED if usage is not None else UsageStatus.UNAVAILABLE
            ),
            usage=usage,
        )


def _execute_claude_code_developer(
    request_path: Path, response_path: Path, *, model: str | None = None
) -> None:
    """Invoke Claude Code with edit access and persist its canonical handoff."""

    request = read_request(request_path)
    try:
        worktree = Path(request['scope']['worktree_path'])
        timeout_seconds = int(request['payload']['timeout_seconds'])
    except (KeyError, TypeError, ValueError) as error:
        raise DeveloperAdapterError(MISSING_REQUEST_PATHS) from error
    executable = shutil.which('claude')
    if executable is None:
        raise DeveloperAdapterError(CLAUDE_CODE_NOT_FOUND)
    skill = skill_destination('claude-code', 'agent-orchestra-developer')
    if not (skill / 'SKILL.md').is_file():
        raise DeveloperAdapterError(DEVELOPER_SKILL_MISSING)
    with tempfile.TemporaryDirectory(prefix='.claude-developer-') as directory:
        temporary = Path(directory)
        try:
            plugin = _stage_skill(skill, temporary)
        except OSError as error:
            raise DeveloperAdapterError(f'{SKILL_STAGING_FAILED}: {error}') from error
        command = [
            executable,
            *adapter_arguments(
                'claude-code',
                'developer',
                settings=_developer_settings(),
                schema=json.dumps(DEVELOPER_RESULT_SCHEMA, separators=(',', ':')),
            ),
            '--plugin-dir',
            str(plugin),
            '--allowedTools',
            'Read',
            'Glob',
            'Grep',
            'Edit',
            'Write',
            'Bash',
            f'Skill({CLAUDE_DEVELOPER_SKILL})',
        ]
        if model:
            command.extend(['--model', model])
        try:
            completed = run_streaming_process(
                command,
                cwd=worktree,
                env=child_process_environment(CLAUDE_CODE_SUBPROCESS_ENV_SCRUB='1'),
                input=developer_prompt(
                    request,
                    f'the Skill tool with name `{CLAUDE_DEVELOPER_SKILL}`',
                ),
                timeout=max(1, timeout_seconds - 5),
            )
        except subprocess.TimeoutExpired as error:
            raise DeveloperAdapterError(
                CLAUDE_CODE_DEVELOPER_TIMEOUT, timed_out=True
            ) from error
    output = _output_with_runtime_metadata(completed.stdout)
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or completed.stdout.strip()
        raise DeveloperAdapterError(
            f'claude-code failed with code {completed.returncode}: {diagnostic}'
        )
    if output is None:
        raise DeveloperAdapterError(MISSING_STRUCTURED_OUTPUT)
    try:
        result = output['structured_output']
    except (KeyError, TypeError) as error:
        raise DeveloperAdapterError(MISSING_STRUCTURED_OUTPUT) from error
    if not isinstance(result, dict):
        raise DeveloperAdapterError(MISSING_STRUCTURED_OUTPUT)
    write_handoff(response_path, request, result)


@dataclass(frozen=True, slots=True)
class ClaudeCodeReviewerAdapter(ReviewerAdapter):
    """Implement canonical code review through the Claude Code CLI."""

    model: str | None = None

    def execute(self, request_path: Path, response_path: Path) -> None:
        """Execute a diff-scoped review."""

        _execute_claude_code_reviewer(request_path, response_path, model=self.model)


@dataclass(frozen=True, slots=True)
class ClaudeCodeDeveloperAdapter(DeveloperAdapter):
    """Implement canonical development through the Claude Code CLI."""

    model: str | None = None

    def execute(self, request_path: Path, response_path: Path) -> None:
        """Execute one development request."""

        _execute_claude_code_developer(request_path, response_path, model=self.model)


def main(argv: list[str] | None = None) -> int:
    """Run a Claude Code role adapter."""

    parser = argparse.ArgumentParser(prog='agent-orchestra-claude-code-reviewer')
    parser.add_argument('--role', choices=('reviewer', 'developer'), default='reviewer')
    parser.add_argument('--model')
    parser.add_argument('request', type=Path)
    parser.add_argument('response', type=Path)
    parsed = parser.parse_args(argv)
    try:
        if parsed.role == 'reviewer':
            ClaudeCodeReviewerAdapter(parsed.model).execute(
                parsed.request, parsed.response
            )
        else:
            ClaudeCodeDeveloperAdapter(parsed.model).execute(
                parsed.request, parsed.response
            )
    except (AdapterError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        if isinstance(error, AdapterError) and error.timed_out:
            # The orchestrator sees only a non-zero exit, so report the reason
            # through the sidecar. Nothing has written it yet on this path: the
            # adapter records models only after its child returns normally.
            write_runtime_metadata((), timed_out=True)
        return 2
    return 0


def developer_main(argv: list[str] | None = None) -> int:
    """Run the Claude Code developer adapter entry point."""

    arguments = list(argv) if argv is not None else sys.argv[1:]
    return main(['--role', 'developer', *arguments])


if __name__ == '__main__':
    raise SystemExit(main())
