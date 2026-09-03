"""Shared canonical handling for runtime-specific developer adapters."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agent_orchestra.schemas import SchemaValidationError, validate_developer_result

if TYPE_CHECKING:
    from pathlib import Path


class DeveloperAdapterError(RuntimeError):
    """Raised when a developer adapter cannot produce a canonical handoff."""


def read_request(path: Path) -> dict[str, Any]:
    """Read a developer request as a UTF-8 JSON object."""

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise DeveloperAdapterError(f'invalid JSON at {path}: {error}') from error
    if not isinstance(document, dict):
        raise DeveloperAdapterError(f'expected a JSON object at {path}')
    return document


def developer_prompt(request: dict[str, Any], skill_name: str) -> str:
    """Build a complete non-interactive developer assignment."""

    return f"""Invoke {skill_name} and perform the assigned remediation.

The JSON below is the complete orchestrator-supplied request. Treat it as
authoritative. Edit only its assigned worktree, perform local validation, do
not commit or perform remote actions, and return only the structured object
required by the supplied JSON Schema.

Before editing, read the complete canonical review JSON from
`payload.review_result_path` and its artifact from
`payload.review_artifact_path`. Return exactly one disposition for every
`finding_id` in the review JSON, even when rejecting or blocking a finding.

Developer request:
{json.dumps(request, indent=2)}
"""


def write_handoff(
    response_path: Path, request: dict[str, Any], result: dict[str, Any]
) -> None:
    """Validate and atomically write a correlated developer handoff."""

    try:
        parsed = validate_developer_result(result)
    except SchemaValidationError as error:
        raise DeveloperAdapterError(str(error)) from error
    response = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': int(request['sequence']) + 1,
        'iteration': request['iteration'],
        'message_type': 'developer_handoff',
        'sender': 'developer',
        'recipient': 'orchestrator',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': request['scope'],
        'payload': parsed.model_dump(mode='json'),
    }
    response_path.parent.mkdir(parents=True, exist_ok=True)
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
