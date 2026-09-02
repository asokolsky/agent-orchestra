"""Run a read-only Codex review from an agent-orchestra review request."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_orchestra.models import (
    REVIEW_FINDING_FIELDS,
    Finding,
    Review,
    Severity,
    Verdict,
)
from agent_orchestra.reports import render_review
from agent_orchestra.skill_install import AgentTarget, skill_destination


class CodexReviewerError(RuntimeError):
    """Raised when Codex cannot produce a valid structured review."""


INVALID_RESULT_FIELDS = 'Codex review has missing or unknown fields'
INVALID_VERDICT = 'Codex review has an invalid verdict'
INVALID_SUMMARY = 'Codex review summary must be text'
INVALID_LISTS = 'Codex review list fields are invalid'
INVALID_FINDINGS = 'Codex review findings are invalid'
APPROVED_WITH_FINDINGS = 'approved Codex review cannot contain findings'
MISSING_REQUEST_PATHS = 'review request is missing required paths'
CODEX_NOT_FOUND = 'codex executable not found'
CODEX_TIMEOUT = 'codex review timed out'
REVIEWER_SKILL_MISSING = (
    'agent-orchestra-reviewer skill is not installed; run '
    '`agent-orchestra skills install --agent codex --skill agent-orchestra-reviewer`'
)
ARTIFACT_OUTSIDE_RUN = 'review artifact path must be inside the request run directory'
REQUEST_OUTSIDE_MESSAGES = 'review request must be inside a run messages directory'


REVIEW_SCHEMA: dict[str, Any] = {
    'type': 'object',
    'additionalProperties': False,
    'required': [
        'verdict',
        'summary',
        'findings',
        'validation',
        'verification_gaps',
    ],
    'properties': {
        'verdict': {
            'type': 'string',
            'enum': ['approved', 'changes_requested', 'blocked'],
        },
        'summary': {'type': 'string'},
        'findings': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': sorted(REVIEW_FINDING_FIELDS),
                'properties': {
                    'finding_id': {'type': 'string'},
                    'severity': {
                        'type': 'string',
                        'enum': ['critical', 'high', 'medium', 'low'],
                    },
                    'title': {'type': 'string'},
                    'path': {'type': ['string', 'null']},
                    'line': {'type': ['integer', 'null'], 'minimum': 1},
                    'explanation': {'type': 'string'},
                    'acceptance_criterion': {'type': 'string'},
                },
            },
        },
        'validation': {'type': 'array', 'items': {'type': 'string'}},
        'verification_gaps': {'type': 'array', 'items': {'type': 'string'}},
    },
}


def _read_object(path: Path) -> dict[str, Any]:
    """Read a JSON object from a UTF-8 file."""

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise CodexReviewerError(f'invalid JSON at {path}: {error}') from error
    if not isinstance(document, dict):
        raise CodexReviewerError(f'expected a JSON object at {path}')
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


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Write a formatted UTF-8 JSON object atomically."""

    _write_text_atomic(path, json.dumps(document, indent=2) + '\n')


def _prompt(request: dict[str, Any]) -> str:
    """Build the complete non-interactive reviewer assignment."""

    return f"""Invoke $agent-orchestra-reviewer and perform the assigned review.

The JSON below is the complete orchestrator-supplied review request. Treat it
as authoritative even though it is embedded in this prompt. Work only in its
scope, keep the review read-only, verify the exact diff digest, and return only
the JSON object required by the supplied output schema. Do not write files;
agent-orchestra will persist the response and Markdown artifact.

Review request:
{json.dumps(request, indent=2)}
"""


def _validate_result(result: dict[str, Any]) -> None:
    """Apply stable checks in case the installed CLI ignores the schema."""

    if set(result) != set(REVIEW_SCHEMA['required']):
        raise CodexReviewerError(INVALID_RESULT_FIELDS)
    if result['verdict'] not in {'approved', 'changes_requested', 'blocked'}:
        raise CodexReviewerError(INVALID_VERDICT)
    if not isinstance(result['summary'], str):
        raise CodexReviewerError(INVALID_SUMMARY)
    if not isinstance(result['findings'], list) or any(
        not isinstance(result[key], list)
        or any(not isinstance(value, str) for value in result[key])
        for key in ('validation', 'verification_gaps')
    ):
        raise CodexReviewerError(INVALID_LISTS)
    if any(
        not isinstance(item, dict)
        or set(item) != REVIEW_FINDING_FIELDS
        or item['severity'] not in {'critical', 'high', 'medium', 'low'}
        or any(
            not isinstance(item[key], str)
            for key in (
                'finding_id',
                'title',
                'explanation',
                'acceptance_criterion',
            )
        )
        or (item['path'] is not None and not isinstance(item['path'], str))
        or (
            item['line'] is not None
            and (
                not isinstance(item['line'], int)
                or isinstance(item['line'], bool)
                or item['line'] < 1
            )
        )
        for item in result['findings']
    ):
        raise CodexReviewerError(INVALID_FINDINGS)
    if result['verdict'] == 'approved' and result['findings']:
        raise CodexReviewerError(APPROVED_WITH_FINDINGS)


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


def run_codex_reviewer(
    request_path: Path, response_path: Path, *, model: str | None = None
) -> None:
    """Invoke Codex and persist a correlated response plus review artifact."""

    request = _read_object(request_path)
    try:
        worktree = Path(request['scope']['worktree_path'])
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
                'exec',
                '--ephemeral',
                '--ignore-user-config',
                '--sandbox',
                'read-only',
                '--cd',
                str(worktree),
                '--output-schema',
                str(schema_path),
                '--output-last-message',
                str(result_path),
                '--color',
                'never',
            ]
            if model:
                command.extend(['--model', model])
            command.append('-')
            completed = subprocess.run(
                command,
                input=_prompt(request),
                check=False,
                capture_output=True,
                text=True,
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
    _write_text_atomic(artifact_path, render_review(_review(request, result)))
    response = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': 2,
        'iteration': request['iteration'],
        'message_type': 'review_result',
        'sender': 'reviewer',
        'recipient': 'orchestrator',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': request['scope'],
        'payload': {**result, 'artifact_path': str(artifact_path)},
    }
    _write_json_atomic(response_path, response)


def main(argv: list[str] | None = None) -> int:
    """Run the Codex reviewer command adapter."""

    arguments = list(argv) if argv is not None else sys.argv[1:]
    parser = argparse.ArgumentParser(prog='agent-orchestra-codex-reviewer')
    parser.add_argument('--model')
    parser.add_argument('request', type=Path)
    parser.add_argument('response', type=Path)
    parsed = parser.parse_args(arguments)
    try:
        run_codex_reviewer(parsed.request, parsed.response, model=parsed.model)
    except (CodexReviewerError, OSError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
