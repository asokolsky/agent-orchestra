"""Tests for reviewer-qualified worker invocation evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_orchestra.invocations import InvocationIdentity, read_records
from agent_orchestra.models import Run
from agent_orchestra.worker import WorkerError, _record_invocation

if TYPE_CHECKING:
    from pathlib import Path


def test_record_invocation_uses_reviewer_qualified_schema_5_paths(
    tmp_path: Path,
) -> None:
    """Persist one reviewer member without colliding with another member."""

    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    logs = tmp_path / 'logs'
    invocations = tmp_path / 'invocations'
    identity = InvocationIdentity(vendor='openai', model=None, runtime='codex')

    invocation_id = _record_invocation(
        run=run,
        role='reviewer',
        identity=identity,
        iteration=1,
        sequence=1,
        started_at='2026-09-09T00:00:00Z',
        logs=logs,
        invocations=invocations,
        stdout='',
        stderr='',
        exit_code=None,
        finished=False,
        reviewer_id='security',
    )
    _record_invocation(
        run=run,
        role='reviewer',
        identity=identity,
        iteration=1,
        sequence=1,
        started_at='2026-09-09T00:00:00Z',
        logs=logs,
        invocations=invocations,
        stdout=None,
        stderr=None,
        exit_code=None,
        invocation_id=invocation_id,
        finished=False,
        status='running',
        reviewer_id='security',
    )
    _record_invocation(
        run=run,
        role='reviewer',
        identity=identity,
        iteration=1,
        sequence=1,
        started_at='2026-09-09T00:00:00Z',
        logs=logs,
        invocations=invocations,
        stdout='approved',
        stderr='',
        exit_code=0,
        invocation_id=invocation_id,
        finished_at_value='2026-09-09T00:00:01Z',
        response_received_at='2026-09-09T00:00:02Z',
        validation_started_at='2026-09-09T00:00:03Z',
        reviewer_id='security',
    )

    task_id = f'{run.id}:000001-reviewer-security'
    assert invocation_id == f'{task_id}:attempt-0001'
    assert (logs / '000001-reviewer-security.attempt-0001.stdout.log').is_file()
    assert (invocations / '000001-reviewer-security.attempt-0001.json').is_file()
    records = read_records(tmp_path, str(run.id))
    assert len(records) == 1
    assert records[0].schema_version == 5
    assert records[0].task_id == task_id
    assert records[0].reviewer_id == 'security'


def test_record_invocation_rejects_reviewer_id_for_developer(tmp_path: Path) -> None:
    """Keep reviewer namespaces unavailable to non-reviewer tasks."""

    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    identity = InvocationIdentity(vendor='openai', model=None, runtime='codex')

    with pytest.raises(WorkerError, match='only reviewer invocations'):
        _record_invocation(
            run=run,
            role='developer',
            identity=identity,
            iteration=1,
            sequence=1,
            started_at='2026-09-09T00:00:00Z',
            logs=tmp_path / 'logs',
            invocations=tmp_path / 'invocations',
            stdout='',
            stderr='',
            exit_code=0,
            reviewer_id='security',
        )
