"""Tests for reviewer-qualified worker invocation evidence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.evidence import (
    WorkerError,
)
from agent_orchestra.invocations import (
    AttemptIdentity,
    AttemptLifecycle,
    AttemptStatus,
    InvocationEvidenceStore,
    InvocationIdentity,
    ProcessOutcome,
    record_invocation,
)
from agent_orchestra.models import Run

if TYPE_CHECKING:
    from pathlib import Path


def _complete_reviewer_invocation(
    *,
    run: Run,
    reviewer_id: str,
    run_directory: Path,
    identity: InvocationIdentity,
) -> str:
    """Persist one complete reviewer invocation lifecycle."""

    invocation_id = record_invocation(
        AttemptIdentity(
            run_id=str(run.id),
            role=RuntimeRole.REVIEWER,
            agent=identity,
            iteration=1,
            sequence=1,
            reviewer_id=reviewer_id,
        ),
        ProcessOutcome(
            started_at='2026-09-09T00:00:00Z',
            stdout='',
            stderr='',
            exit_code=None,
            finished=False,
        ),
        run_directory=run_directory,
    )
    record_invocation(
        AttemptIdentity(
            run_id=str(run.id),
            role=RuntimeRole.REVIEWER,
            agent=identity,
            iteration=1,
            sequence=1,
            invocation_id=invocation_id,
            reviewer_id=reviewer_id,
        ),
        ProcessOutcome(
            started_at='2026-09-09T00:00:00Z',
            stdout=None,
            stderr=None,
            exit_code=None,
            finished=False,
        ),
        run_directory=run_directory,
        lifecycle=AttemptLifecycle(status=AttemptStatus.RUNNING),
    )
    record_invocation(
        AttemptIdentity(
            run_id=str(run.id),
            role=RuntimeRole.REVIEWER,
            agent=identity,
            iteration=1,
            sequence=1,
            invocation_id=invocation_id,
            reviewer_id=reviewer_id,
        ),
        ProcessOutcome(
            started_at='2026-09-09T00:00:00Z',
            stdout='approved',
            stderr='',
            exit_code=0,
            finished_at='2026-09-09T00:00:01Z',
        ),
        run_directory=run_directory,
        lifecycle=AttemptLifecycle(
            response_received_at='2026-09-09T00:00:02Z',
            validation_started_at='2026-09-09T00:00:03Z',
        ),
    )
    return invocation_id


def test_record_invocation_uses_distinct_reviewer_qualified_schema_5_paths(
    tmp_path: Path,
) -> None:
    """Persist two same-sequence reviewer members without collisions."""

    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    run_directory = tmp_path
    identity = InvocationIdentity(vendor='openai', model=None, runtime='codex')

    security_id = _complete_reviewer_invocation(
        run=run,
        reviewer_id='security',
        run_directory=run_directory,
        identity=identity,
    )
    performance_id = _complete_reviewer_invocation(
        run=run,
        reviewer_id='performance',
        run_directory=run_directory,
        identity=identity,
    )

    assert security_id != performance_id
    for reviewer_id in ('security', 'performance'):
        stem = f'000001-reviewer-{reviewer_id}.attempt-0001'
        assert (run_directory / 'logs' / f'{stem}.stdout.log').is_file()
        assert (run_directory / 'logs' / f'{stem}.stderr.log').is_file()
        assert (run_directory / 'invocations' / f'{stem}.json').is_file()
    records = InvocationEvidenceStore(tmp_path).read_all(str(run.id))
    assert len(records) == 2
    assert {record.schema_version for record in records} == {5}
    assert len({record.task_id for record in records}) == 2
    assert len({record.invocation_id for record in records}) == 2
    assert {record.reviewer_id for record in records} == {'security', 'performance'}


def test_record_invocation_rejects_reviewer_id_for_developer(tmp_path: Path) -> None:
    """Keep reviewer namespaces unavailable to non-reviewer tasks."""

    run_directory = tmp_path
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    identity = InvocationIdentity(vendor='openai', model=None, runtime='codex')

    with pytest.raises(WorkerError, match='only reviewer invocations'):
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.DEVELOPER,
                agent=identity,
                iteration=1,
                sequence=1,
                reviewer_id='security',
            ),
            ProcessOutcome(
                started_at='2026-09-09T00:00:00Z', stdout='', stderr='', exit_code=0
            ),
            run_directory=run_directory,
        )


def test_record_invocation_normalizes_invalid_reviewer_id(tmp_path: Path) -> None:
    """Report invalid reviewer identifiers through the worker error boundary."""

    run_directory = tmp_path
    run = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    identity = InvocationIdentity(vendor='openai', model=None, runtime='codex')

    with pytest.raises(WorkerError, match="invalid reviewer ID: 'Security'"):
        record_invocation(
            AttemptIdentity(
                run_id=str(run.id),
                role=RuntimeRole.REVIEWER,
                agent=identity,
                iteration=1,
                sequence=1,
                reviewer_id='Security',
            ),
            ProcessOutcome(
                started_at='2026-09-09T00:00:00Z',
                stdout='',
                stderr='',
                exit_code=None,
                finished=False,
            ),
            run_directory=run_directory,
        )
