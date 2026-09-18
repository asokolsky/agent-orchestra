#!/usr/bin/env python3
"""Fake Codex process used by built-in adapter integration tests."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Literal, TypedDict, cast

type Mode = Literal['approved', 'changes_requested', 'nonzero', 'timeout']


class CodexConfig(TypedDict):
    """Configurable behavior for the fake Codex process."""

    mode: Mode


def load_config() -> CodexConfig:
    """Load the sidecar configuration for this copied executable."""

    config_path = Path(__file__).with_name(f'{Path(__file__).name}.json')
    return cast('CodexConfig', json.loads(config_path.read_text()))


def main(arguments: list[str]) -> None:
    """Emit configured process output and, when applicable, a review result."""

    mode = load_config()['mode']
    sys.stdin.read()
    print('child stdout', flush=True)
    print('child stderr', file=sys.stderr, flush=True)
    if mode == 'nonzero':
        raise SystemExit(9)
    if mode == 'timeout':
        time.sleep(20)

    verdict = 'changes_requested' if mode == 'changes_requested' else 'approved'
    findings = []
    if mode == 'changes_requested':
        findings.append(
            {
                'finding_id': 'F1',
                'severity': 'low',
                'title': 'Fix this',
                'path': 'a.py',
                'line': 1,
                'explanation': 'Because.',
                'acceptance_criterion': 'Fixed.',
            }
        )
    result_path = Path(arguments[arguments.index('--output-last-message') + 1])
    result_path.write_text(
        json.dumps(
            {
                'verdict': verdict,
                'summary': 'Ready.',
                'findings': findings,
                'validation': [],
                'verification_gaps': [],
            }
        )
    )


if __name__ == '__main__':
    main(sys.argv)
