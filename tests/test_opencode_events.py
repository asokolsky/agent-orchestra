"""Exercise complete and adversarial OpenCode machine event streams."""

from __future__ import annotations

import json
from typing import Any

import pytest

from agent_orchestra.adapter.opencode_events import OpenCodeEventError, parse_events


def _event(kind: str, part: dict[str, Any] | None = None) -> str:
    """Render one event for a single deterministic fake session."""

    event: dict[str, Any] = {
        'type': kind,
        'timestamp': 1,
        'sessionID': 'session-1',
    }
    if part is not None:
        event['part'] = {'sessionID': 'session-1', **part}
    return json.dumps(event)


def _complete_stream() -> str:
    """Return a successful one-step run with a JSON final response."""

    return '\n'.join(
        (
            _event('step_start', {'type': 'step-start'}),
            _event('text', {'type': 'text', 'text': '{"verdict":"approved"}'}),
            _event(
                'step_finish',
                {
                    'type': 'step-finish',
                    'cost': 0.01,
                    'tokens': {
                        'input': 10,
                        'output': 5,
                        'reasoning': 1,
                        'cache': {'read': 2, 'write': 3},
                    },
                },
            ),
        )
    )


def test_parse_complete_opencode_stream() -> None:
    """A completed event stream yields only the final JSON object."""

    result = parse_events(_complete_stream())

    assert result.value == {'verdict': 'approved'}
    assert result.session_id == 'session-1'
    assert result.effective_models == ()
    assert result.usage.turn_count == 1
    assert result.usage.totals is not None
    assert result.usage.totals.input_tokens == 10
    assert result.usage.totals.cache_creation_input_tokens == 3


@pytest.mark.parametrize(
    ('stream', 'message'),
    [
        ('', 'no JSON events'),
        ('{', 'malformed JSON'),
        (_event('step_start', {'type': 'step-start'}), 'incomplete'),
        (
            '\n'.join(_complete_stream().splitlines()[:-1]),
            'incomplete',
        ),
        (
            _complete_stream().replace('"session-1"', '"session-2"', 1),
            'invalid part',
        ),
        (
            _complete_stream()
            + '\n'
            + _event('step_start', {'type': 'step-start'}).replace(
                'session-1', 'session-2'
            ),
            'multiple sessions',
        ),
        (
            _complete_stream() + '\n' + _event('error'),
            'session error',
        ),
        (
            _complete_stream().replace('"cost": 0.01', '"cost": -1'),
            'invalid usage',
        ),
        (
            _complete_stream().replace('{\\"verdict\\":\\"approved\\"}', 'prose'),
            'not JSON',
        ),
    ],
)
def test_reject_incomplete_or_malformed_events(stream: str, message: str) -> None:
    """Failures cannot be promoted to a canonical result."""

    with pytest.raises(OpenCodeEventError, match=message):
        parse_events(stream)
