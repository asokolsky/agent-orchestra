"""Deterministic reviewer process used by command-line integration tests."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast
from uuid import uuid4


class ReviewerConfig(TypedDict):
    """Configurable behavior for the test reviewer process."""

    verdict: str
    write_artifact: bool
    loop: bool
    provenance: bool
    execution_counter: str | None
    artifact_content: str
    sleep_iteration: int | None
    sleep_seconds: float


def load_config() -> ReviewerConfig:
    """Load the sidecar configuration for this copied reviewer."""

    config_path = Path(__file__).with_name(f'{Path(__file__).name}.json')
    return cast('ReviewerConfig', json.loads(config_path.read_text()))


def record_execution(counter: str | None) -> None:
    """Record one process activation when a counter path is configured."""

    if counter is None:
        return
    counter_path = Path(counter)
    prior = counter_path.read_text() if counter_path.exists() else ''
    counter_path.write_text(f'{prior}1\n')


def main(arguments: list[str]) -> None:
    """Write a correlated review result for the supplied request."""

    config = load_config()
    record_execution(config['execution_counter'])
    if config['provenance']:
        metadata_path = Path(os.environ['AGENT_ORCHESTRA_RUNTIME_METADATA_PATH'])
        metadata_path.write_text(
            json.dumps(
                {
                    'schema_version': 2,
                    'effective_models': ['claude-primary', 'claude-fallback'],
                    'status': 'reported',
                    'timed_out': False,
                }
            )
        )

    request_path = Path(arguments[1])
    response_path = Path(arguments[2])
    request = json.loads(request_path.read_text())
    artifact_path = Path(request['payload']['artifact_path'])
    if config['write_artifact']:
        artifact_path.write_text(config['artifact_content'])
    if config['sleep_iteration'] in {None, request['iteration']}:
        time.sleep(config['sleep_seconds'])

    verdict = config['verdict']
    if config['loop']:
        verdict = 'changes_requested' if request['iteration'] == 1 else 'approved'
    findings = []
    if verdict != 'approved':
        findings.append(
            {
                'finding_id': 'F-001',
                'severity': 'medium',
                'title': 'fix',
                'path': 'tracked.txt',
                'line': 1,
                'explanation': 'Needs correction.',
                'acceptance_criterion': 'Correct the content.',
            }
        )
    response = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': request['sequence'] + 1,
        'iteration': request['iteration'],
        'message_type': 'review_result',
        'sender': 'reviewer',
        'recipient': 'orchestrator',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': request['scope'],
        'payload': {
            'verdict': verdict,
            'summary': 'reviewed',
            'findings': findings,
            'validation': [],
            'verification_gaps': [],
            'artifact_path': str(artifact_path),
        },
    }
    response_path.write_text(json.dumps(response))


if __name__ == '__main__':
    main(sys.argv)
