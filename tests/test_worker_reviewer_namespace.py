"""Tests for reviewer-qualified worker invocation evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.evidence import (
    WorkerError,
)
from agent_orchestra.invocations import (
    AttemptStatus,
    InvocationEvidenceStore,
    InvocationIdentity,
)
from agent_orchestra.models import Run
from agent_orchestra.worker import _record_invocation

if TYPE_CHECKING:
    from pathlib import Path


def _complete_reviewer_invocation(
    *,
    run: Run,
    reviewer_id: str,
    logs: Path,
    invocations: Path,
    identity: InvocationIdentity,
) -> str:
    """Persist one complete reviewer invocation lifecycle."""

    invocation_id = _record_invocation(
        run=run,
        role=RuntimeRole.REVIEWER,
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
        reviewer_id=reviewer_id,
    )
    _record_invocation(
        run=run,
        role=RuntimeRole.REVIEWER,
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
        status=AttemptStatus.RUNNING,
        reviewer_id=reviewer_id,
    )
    _record_invocation(
        run=run,
        role=RuntimeRole.REVIEWER,
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
        reviewer_id=reviewer_id,
    )
    return invocation_id


def test_record_invocation_uses_distinct_reviewer_qualified_schema_5_paths(
    tmp_path: Path,
) -> None:
    """Persist two same-sequence reviewer members without collisions."""

    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    logs = tmp_path / 'logs'
    invocations = tmp_path / 'invocations'
    identity = InvocationIdentity(vendor='openai', model=None, runtime='codex')

    security_id = _complete_reviewer_invocation(
        run=run,
        reviewer_id='security',
        logs=logs,
        invocations=invocations,
        identity=identity,
    )
    performance_id = _complete_reviewer_invocation(
        run=run,
        reviewer_id='performance',
        logs=logs,
        invocations=invocations,
        identity=identity,
    )

    assert security_id != performance_id
    for reviewer_id in ('security', 'performance'):
        stem = f'000001-reviewer-{reviewer_id}.attempt-0001'
        assert (logs / f'{stem}.stdout.log').is_file()
        assert (logs / f'{stem}.stderr.log').is_file()
        assert (invocations / f'{stem}.json').is_file()
    records = InvocationEvidenceStore(tmp_path).read_all(str(run.id))
    assert len(records) == 2
    assert {record.schema_version for record in records} == {5}
    assert len({record.task_id for record in records}) == 2
    assert len({record.invocation_id for record in records}) == 2
    assert {record.reviewer_id for record in records} == {'security', 'performance'}


def test_record_invocation_rejects_reviewer_id_for_developer(tmp_path: Path) -> None:
    """Keep reviewer namespaces unavailable to non-reviewer tasks."""

    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    identity = InvocationIdentity(vendor='openai', model=None, runtime='codex')

    with pytest.raises(WorkerError, match='only reviewer invocations'):
        _record_invocation(
            run=run,
            role=RuntimeRole.DEVELOPER,
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


def test_record_invocation_normalizes_invalid_reviewer_id(tmp_path: Path) -> None:
    """Report invalid reviewer identifiers through the worker error boundary."""

    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    identity = InvocationIdentity(vendor='openai', model=None, runtime='codex')

    with pytest.raises(WorkerError, match="invalid reviewer ID: 'Security'"):
        _record_invocation(
            run=run,
            role=RuntimeRole.REVIEWER,
            identity=identity,
            iteration=1,
            sequence=1,
            started_at='2026-09-09T00:00:00Z',
            logs=tmp_path / 'logs',
            invocations=tmp_path / 'invocations',
            stdout='',
            stderr='',
            exit_code=None,
            finished=False,
            reviewer_id='Security',
        )
