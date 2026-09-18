"""Deterministic developer process used by command-line integration tests."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast
from uuid import uuid4


class DeveloperConfig(TypedDict):
    """Configurable behavior for the test developer process."""

    change_worktree: bool
    disposition: str
    recoverable: bool
    execution_counter: str | None
    status: str


def load_config() -> DeveloperConfig:
    """Load the sidecar configuration for this copied developer."""

    config_path = Path(__file__).with_name(f'{Path(__file__).name}.json')
    return cast('DeveloperConfig', json.loads(config_path.read_text()))


def record_execution(counter: str | None) -> None:
    """Record one process activation when a counter path is configured."""

    if counter is None:
        return
    counter_path = Path(counter)
    prior = counter_path.read_text() if counter_path.exists() else ''
    counter_path.write_text(f'{prior}1\n')


def main(arguments: list[str]) -> None:
    """Apply configured remediation and write a correlated handoff."""

    config = load_config()
    record_execution(config['execution_counter'])
    request_path = Path(arguments[1])
    response_path = Path(arguments[2])
    request = json.loads(request_path.read_text())
    worktree = Path(request['scope']['worktree_path'])
    if config['change_worktree']:
        (worktree / 'tracked.txt').write_text('remediated\n')
    review = json.loads(Path(request['payload']['review_result_path']).read_text())
    status = config['status']
    if config['recoverable'] and request['sequence'] == 3:
        status = 'blocked'
    response = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': request['sequence'] + 1,
        'iteration': request['iteration'],
        'message_type': 'developer_handoff',
        'sender': 'developer',
        'recipient': 'orchestrator',
        'created_at': datetime.now(UTC).isoformat().replace('+00:00', 'Z'),
        'scope': request['scope'],
        'payload': {
            'status': status,
            'summary': 'remediated',
            'files_changed': ['tracked.txt'],
            'validation': [],
            'dispositions': [
                {
                    'finding_id': item['finding_id'],
                    'disposition': config['disposition'],
                    'rationale': 'evaluated',
                }
                for item in review['payload']['findings']
            ],
            'remaining_risks': [],
        },
    }
    response_path.write_text(json.dumps(response))


if __name__ == '__main__':
    main(sys.argv)
