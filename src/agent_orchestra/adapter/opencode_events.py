"""Translate complete OpenCode JSONL events into one strict role result."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, cast

from agent_orchestra.adapter.errors import AdapterError
from agent_orchestra.usage import RuntimeUsage, UsageValues


class OpenCodeEventError(AdapterError):
    """Report malformed, failed, or incomplete OpenCode event output."""


@dataclass(frozen=True, slots=True)
class OpenCodeResult:
    """One JSON result and reported model identities from a completed run."""

    value: dict[str, Any]
    session_id: str
    effective_models: tuple[str, ...]
    usage: RuntimeUsage


def parse_events(stdout: str) -> OpenCodeResult:
    """
    Require a complete, single-session JSON event stream and final JSON text.

    A raw event or partial text is never interpreted as the canonical result.
    Callers must check the process exit status before accepting this result.
    """

    session_id: str | None = None
    started = False
    finished = False
    final_text: str | None = None
    step_count = 0
    input_tokens = 0
    output_tokens = 0
    cache_creation_tokens = 0
    cache_read_tokens = 0
    total_cost = 0.0
    lines = stdout.splitlines()
    if not lines:
        msg = 'OpenCode returned no JSON events'
        raise OpenCodeEventError(msg)
    for number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise OpenCodeEventError(
                f'OpenCode event {number} is malformed JSON'
            ) from error
        if not isinstance(event, dict):
            raise OpenCodeEventError(f'OpenCode event {number} is not an object')
        event_type = event.get('type')
        event_session = event.get('sessionID')
        if (
            event_type
            not in {
                'step_start',
                'step_finish',
                'text',
                'reasoning',
                'tool_use',
                'error',
            }
            or not isinstance(event_session, str)
            or not event_session
            or type(event.get('timestamp')) is not int
            or event['timestamp'] < 0
        ):
            raise OpenCodeEventError(f'OpenCode event {number} has invalid fields')
        if session_id is None:
            session_id = event_session
        elif event_session != session_id:
            msg = 'OpenCode events span multiple sessions'
            raise OpenCodeEventError(msg)
        if event_type == 'error':
            msg = 'OpenCode reported a session error'
            raise OpenCodeEventError(msg)
        part = event.get('part')
        if not isinstance(part, dict):
            raise OpenCodeEventError(f'OpenCode event {number} has no part')
        expected_part = {
            'step_start': 'step-start',
            'step_finish': 'step-finish',
            'text': 'text',
            'reasoning': 'reasoning',
            'tool_use': 'tool',
        }[event_type]
        if part.get('type') != expected_part or part.get('sessionID') != session_id:
            raise OpenCodeEventError(f'OpenCode event {number} has invalid part')
        if event_type == 'step_start':
            if started and not finished:
                msg = 'OpenCode started a step before finishing one'
                raise OpenCodeEventError(msg)
            started = True
            finished = False
            final_text = None
        elif event_type == 'step_finish':
            if not started or finished:
                msg = 'OpenCode finished a step without starting one'
                raise OpenCodeEventError(msg)
            finished = True
            tokens = part.get('tokens')
            cache = tokens.get('cache') if isinstance(tokens, dict) else None
            counts = (
                tokens.get('input') if isinstance(tokens, dict) else None,
                tokens.get('output') if isinstance(tokens, dict) else None,
                tokens.get('reasoning') if isinstance(tokens, dict) else None,
                cache.get('read') if isinstance(cache, dict) else None,
                cache.get('write') if isinstance(cache, dict) else None,
            )
            cost = part.get('cost')
            if (
                not all(type(count) is int and count >= 0 for count in counts)
                or not isinstance(cost, int | float)
                or isinstance(cost, bool)
                or not math.isfinite(cost)
                or cost < 0
            ):
                msg = 'OpenCode step-finish has invalid usage'
                raise OpenCodeEventError(msg)
            valid_counts = cast('tuple[int, int, int, int, int]', counts)
            step_count += 1
            input_tokens += valid_counts[0]
            output_tokens += valid_counts[1]
            cache_read_tokens += valid_counts[3]
            cache_creation_tokens += valid_counts[4]
            total_cost += cost
        elif event_type == 'text':
            value = part.get('text')
            if not started or finished or not isinstance(value, str) or not value:
                msg = 'OpenCode returned text outside an active step'
                raise OpenCodeEventError(msg)
            final_text = value
        elif not started or finished:
            msg = 'OpenCode returned content outside an active step'
            raise OpenCodeEventError(msg)
    if not finished or not final_text or session_id is None:
        msg = 'OpenCode event stream is incomplete'
        raise OpenCodeEventError(msg)
    try:
        value = json.loads(final_text)
    except json.JSONDecodeError as error:
        msg = 'OpenCode final text is not JSON'
        raise OpenCodeEventError(msg) from error
    if not isinstance(value, dict):
        msg = 'OpenCode final text is not a JSON object'
        raise OpenCodeEventError(msg)
    usage = RuntimeUsage(
        turn_count=step_count,
        totals=UsageValues(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=cache_creation_tokens,
            cache_read_input_tokens=cache_read_tokens,
            total_cost_usd=total_cost,
        ),
    )
    return OpenCodeResult(value, session_id, (), usage)
