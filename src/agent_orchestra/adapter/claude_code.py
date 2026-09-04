"""Run a read-only Claude Code review from an orchestration request."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_orchestra.adapter.developer import (
    DeveloperAdapterError,
    developer_prompt,
    read_request,
    write_handoff,
)
from agent_orchestra.adapter.process import run_streaming_process
from agent_orchestra.models import Finding, Review, Severity, Verdict
from agent_orchestra.reports import render_review
from agent_orchestra.runtime_metadata import (
    child_process_environment,
    write_runtime_metadata,
)
from agent_orchestra.schemas import (
    DEVELOPER_RESULT_SCHEMA,
    REVIEW_RESULT_SCHEMA,
    SchemaValidationError,
    validate_review_result,
)
from agent_orchestra.skill_install import AgentTarget, skill_destination


class ClaudeCodeReviewerError(RuntimeError):
    """Raised when Claude Code cannot produce a valid structured review."""


CLAUDE_CODE_NOT_FOUND = 'claude executable not found'
CLAUDE_CODE_TIMEOUT = 'claude-code review timed out'
MISSING_STRUCTURED_OUTPUT = 'claude-code response has no structured_output object'
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


def _reviewer_settings(worktree: Path) -> str:
    """Return isolated settings that make the reviewer sandbox read-only."""

    return json.dumps(
        {
            'sandbox': {
                'enabled': True,
                'failIfUnavailable': True,
                'allowUnsandboxedCommands': False,
                'filesystem': {'denyWrite': [str(worktree.resolve())]},
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


def _write_text_atomic(path: Path, content: str) -> None:
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


def _prompt(request: dict[str, Any]) -> str:
    """Build the complete non-interactive Claude Code assignment."""

    return f"""Invoke /agent-orchestra-reviewer and perform the assigned review.

The JSON below is the complete orchestrator-supplied review request. Treat it
as authoritative. Work only in its scope, keep the review read-only, and return
the structured object required by the supplied JSON Schema. The digest covers
more than raw `git diff`; do not compare it to a plain diff hash.
Agent-orchestra verifies digest identity before and after review. Do not write
files; agent-orchestra persists the result. When shell inspection is needed,
use only these exact pre-approved commands: `git status --short`, `git rev-parse
HEAD`, `git diff --no-ext-diff --binary HEAD`, and
`git ls-files --others --exclude-standard`.

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


def _output_with_runtime_metadata(stdout: str) -> dict[str, Any] | None:
    """Parse a Claude Code envelope and report any model identities it contains."""

    try:
        output = json.loads(stdout)
    except json.JSONDecodeError, TypeError:
        return None
    if not isinstance(output, dict):
        return None
    write_runtime_metadata(_effective_models(output))
    return output


def run_claude_code_reviewer(
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
        raise ClaudeCodeReviewerError(CLAUDE_CODE_TIMEOUT)
    executable = shutil.which('claude')
    if executable is None:
        raise ClaudeCodeReviewerError(CLAUDE_CODE_NOT_FOUND)
    if not (
        skill_destination(AgentTarget.CLAUDE_CODE, 'agent-orchestra-reviewer')
        / 'SKILL.md'
    ).is_file():
        raise ClaudeCodeReviewerError(REVIEWER_SKILL_MISSING)

    command = [
        executable,
        '--print',
        '--no-session-persistence',
        '--setting-sources',
        '',
        '--settings',
        _reviewer_settings(worktree),
        '--strict-mcp-config',
        '--mcp-config',
        '{"mcpServers":{}}',
        '--output-format',
        'json',
        '--json-schema',
        json.dumps(REVIEW_RESULT_SCHEMA, separators=(',', ':')),
        '--permission-mode',
        'dontAsk',
        '--tools',
        'Read,Glob,Grep,Bash,Skill',
        '--allowedTools',
        'Read',
        'Glob',
        'Grep',
        'Skill(agent-orchestra-reviewer)',
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
            env=child_process_environment(CLAUDE_CODE_SUBPROCESS_ENV_SCRUB='1'),
            input=_prompt(request),
            timeout=max(1, timeout_seconds - 5),
        )
    except subprocess.TimeoutExpired as error:
        raise ClaudeCodeReviewerError(CLAUDE_CODE_TIMEOUT) from error
    output = _output_with_runtime_metadata(completed.stdout)
    if completed.returncode != 0:
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

    _write_text_atomic(artifact_path, render_review(_review(request, result)))
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


def run_claude_code_developer(
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
    if not (
        skill_destination(AgentTarget.CLAUDE_CODE, 'agent-orchestra-developer')
        / 'SKILL.md'
    ).is_file():
        raise DeveloperAdapterError(DEVELOPER_SKILL_MISSING)
    command = [
        executable,
        '--print',
        '--no-session-persistence',
        '--setting-sources',
        '',
        '--settings',
        _developer_settings(),
        '--strict-mcp-config',
        '--mcp-config',
        '{"mcpServers":{}}',
        '--output-format',
        'json',
        '--json-schema',
        json.dumps(DEVELOPER_RESULT_SCHEMA, separators=(',', ':')),
        '--permission-mode',
        'acceptEdits',
        '--tools',
        'Read,Glob,Grep,Edit,Write,Bash,Skill',
        '--allowedTools',
        'Read',
        'Glob',
        'Grep',
        'Bash',
        'Skill(agent-orchestra-developer)',
    ]
    if model:
        command.extend(['--model', model])
    try:
        completed = run_streaming_process(
            command,
            cwd=worktree,
            env=child_process_environment(CLAUDE_CODE_SUBPROCESS_ENV_SCRUB='1'),
            input=developer_prompt(request, '/agent-orchestra-developer'),
            timeout=max(1, timeout_seconds - 5),
        )
    except subprocess.TimeoutExpired as error:
        raise DeveloperAdapterError(CLAUDE_CODE_DEVELOPER_TIMEOUT) from error
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
            run_claude_code_reviewer(
                parsed.request, parsed.response, model=parsed.model
            )
        else:
            run_claude_code_developer(
                parsed.request, parsed.response, model=parsed.model
            )
    except (ClaudeCodeReviewerError, DeveloperAdapterError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    return 0


def developer_main(argv: list[str] | None = None) -> int:
    """Run the Claude Code developer adapter entry point."""

    arguments = list(argv) if argv is not None else sys.argv[1:]
    return main(['--role', 'developer', *arguments])


if __name__ == '__main__':
    raise SystemExit(main())
