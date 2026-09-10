"""Integration tests for concurrent reviewer-set worker execution."""

from __future__ import annotations

import json
import subprocess
from threading import Barrier
from typing import TYPE_CHECKING, Any, Never, cast
from uuid import uuid4

import pytest

from agent_orchestra import worker as worker_module
from agent_orchestra.adapter.registry import DEFAULT_RUNTIME_REGISTRY
from agent_orchestra.agents import (
    AgentRequest,
    AgentResult,
    CommandAgentAdapter,
    ReviewerRequest,
)
from agent_orchestra.audit import build_audit_document
from agent_orchestra.invocations import InvocationIdentity
from agent_orchestra.models import Run, RunState
from agent_orchestra.reviewer_plan import ReviewerExecution, ReviewerExecutionPlan
from agent_orchestra.store import RunStore
from agent_orchestra.worker import WorkerError, run_queued_reviewer_set

if TYPE_CHECKING:
    from pathlib import Path

DIGEST = f'sha256:{"a" * 64}'


def _reviewer(reviewer_id: str, runtime: str, vendor: str) -> ReviewerExecution:
    """Build one reviewer used by the worker integration test."""

    return ReviewerExecution(
        reviewer_id=reviewer_id,
        command=('reviewer', reviewer_id),
        identity=InvocationIdentity(vendor=vendor, model=None, runtime=runtime),
        timeout_seconds=30,
    )


def _approved_response(request: dict[str, Any], artifact_path: Path) -> dict[str, Any]:
    """Return one correlated approved review response."""

    return {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': request['message_id'],
        'run_id': request['run_id'],
        'sequence': request['sequence'] + 1,
        'iteration': request['iteration'],
        'message_type': 'review_result',
        'sender': 'reviewer',
        'recipient': 'orchestrator',
        'created_at': '2026-09-09T20:00:00Z',
        'scope': request['scope'],
        'payload': {
            'verdict': 'approved',
            'summary': 'approved',
            'findings': [],
            'validation': [],
            'verification_gaps': [],
            'artifact_path': str(artifact_path),
        },
    }


def _changes_requested_response(
    request: dict[str, Any], artifact_path: Path
) -> dict[str, Any]:
    """Return one correlated response containing an actionable finding."""

    response = _approved_response(request, artifact_path)
    response['payload']['verdict'] = 'changes_requested'
    response['payload']['findings'] = [
        {
            'finding_id': 'finding-1',
            'severity': 'high',
            'title': 'Finding',
            'path': 'src/example.py',
            'line': 1,
            'explanation': 'The behavior is incorrect.',
            'acceptance_criterion': 'Correct the behavior.',
        }
    ]
    return response


def test_reviewer_set_runs_concurrently_with_disjoint_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run all required reviewers together and preserve their own evidence."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    database = tmp_path / 'state.db'
    store = RunStore(database)
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)
    barrier = Barrier(2)

    def approve(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        assert request.role == 'reviewer'
        if request.on_started is not None:
            request.on_started()
        barrier.wait(timeout=5)
        document = json.loads(request.request_path.read_text(encoding='utf-8'))
        request.artifact_path.write_text('# Review\n', encoding='utf-8')
        request.response_path.write_text(
            json.dumps(_approved_response(document, request.artifact_path)),
            encoding='utf-8',
        )
        return AgentResult(
            succeeded=True,
            summary='approved',
            stdout='',
            stderr='',
            exit_code=0,
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', approve)
    plan = ReviewerExecutionPlan(
        'default',
        (
            _reviewer('security', 'codex', 'openai'),
            _reviewer('portability', 'claude-code', 'anthropic'),
        ),
    )

    result = run_queued_reviewer_set(
        store=store,
        run=run,
        objective='Review the change.',
        reviewer_plan=plan,
        developer_command=(),
        runs_directory=tmp_path / 'runs',
        developer_timeout_seconds=30,
        max_iterations=3,
        digest_worktree=lambda _path, _base: DIGEST,
        developer_identity=InvocationIdentity(
            vendor='openai', model=None, runtime='codex'
        ),
        registry=DEFAULT_RUNTIME_REGISTRY,
    )

    assert result.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    run_directory = next((tmp_path / 'runs').rglob('execution.json')).parent
    assert (
        json.loads((run_directory / 'execution.json').read_text(encoding='utf-8'))[
            'schema_version'
        ]
        == 3
    )
    for reviewer_id in ('security', 'portability'):
        assert (
            run_directory / f'messages/000001-{reviewer_id}-review-request.json'
        ).is_file()
        assert (
            run_directory / f'messages/000002-{reviewer_id}-review-result.json'
        ).is_file()
        assert (run_directory / f'artifacts/review-0001-{reviewer_id}.md').is_file()
        assert (
            run_directory
            / f'invocations/000001-reviewer-{reviewer_id}.attempt-0001.json'
        ).is_file()
    audit = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert audit['result'] == 'verified'
    finding_codes = {
        str(finding.get('code'))
        for finding in cast('list[dict[str, object]]', audit['findings'])
    }
    assert (
        not {
            'message_correlation_failure',
            'unknown_canonical_evidence',
            'evidence_type_mismatch',
        }
        & finding_codes
    )


def test_reviewer_set_mutation_fails_terminally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never strand a reviewer batch after its immutable diff changes."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)

    def approve(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        assert isinstance(request, ReviewerRequest)
        if request.on_started is not None:
            request.on_started()
        document = json.loads(request.request_path.read_text(encoding='utf-8'))
        request.artifact_path.write_text('# Review\n', encoding='utf-8')
        request.response_path.write_text(
            json.dumps(_approved_response(document, request.artifact_path)),
            encoding='utf-8',
        )
        return AgentResult(
            succeeded=True,
            summary='approved',
            stdout='',
            stderr='',
            exit_code=0,
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', approve)
    digests = iter((DIGEST, f'sha256:{"b" * 64}'))

    with pytest.raises(WorkerError, match='worktree changed'):
        run_queued_reviewer_set(
            store=store,
            run=run,
            objective='Review the change.',
            reviewer_plan=ReviewerExecutionPlan(
                'default',
                (
                    _reviewer('security', 'codex', 'openai'),
                    _reviewer('portability', 'claude-code', 'anthropic'),
                ),
            ),
            developer_command=(),
            runs_directory=tmp_path / 'runs',
            developer_timeout_seconds=30,
            max_iterations=3,
            digest_worktree=lambda _path, _base: next(digests),
            developer_identity=InvocationIdentity(
                vendor='openai', model=None, runtime='codex'
            ),
        )

    assert store.get(run.id).state is RunState.FAILED


@pytest.mark.parametrize('mode', ['timeout', 'nonzero', 'invalid', 'blocked'])
def test_incomplete_reviewer_set_fails_terminally(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finish every blocked reviewer-batch path in a terminal state."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)

    def execute(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        assert isinstance(request, ReviewerRequest)
        if request.on_started is not None:
            request.on_started()
        document = json.loads(request.request_path.read_text(encoding='utf-8'))
        request.artifact_path.write_text('# Review\n', encoding='utf-8')
        if mode == 'timeout':
            request.response_path.write_text('{}', encoding='utf-8')
            command = 'reviewer'
            raise subprocess.TimeoutExpired(command, 30)
        if mode == 'nonzero':
            request.response_path.write_text('{}', encoding='utf-8')
            return AgentResult(
                succeeded=False,
                summary='failed',
                stdout='',
                stderr='failure',
                exit_code=1,
            )
        response = _approved_response(document, request.artifact_path)
        if mode == 'invalid':
            response = {}
        elif mode == 'blocked':
            response['payload']['verdict'] = 'blocked'
        request.response_path.write_text(json.dumps(response), encoding='utf-8')
        return AgentResult(
            succeeded=True,
            summary=mode,
            stdout='',
            stderr='',
            exit_code=0,
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', execute)

    with pytest.raises(WorkerError) as caught:
        run_queued_reviewer_set(
            store=store,
            run=run,
            objective='Review the change.',
            reviewer_plan=ReviewerExecutionPlan(
                'default',
                (
                    _reviewer('security', 'codex', 'openai'),
                    _reviewer('portability', 'claude-code', 'anthropic'),
                ),
            ),
            developer_command=(),
            runs_directory=tmp_path / 'runs',
            developer_timeout_seconds=30,
            max_iterations=3,
            digest_worktree=lambda _path, _base: DIGEST,
            developer_identity=InvocationIdentity(
                vendor='openai', model=None, runtime='codex'
            ),
        )

    assert caught.value.code == 'reviewer_batch_incomplete'
    assert store.get(run.id).state is RunState.FAILED
    run_directory = next((tmp_path / 'runs').rglob('execution.json')).parent
    failure = json.loads((run_directory / 'failure.json').read_text(encoding='utf-8'))
    assert failure['error'] == {
        'code': 'reviewer_batch_incomplete',
        'message': 'reviewer batch did not complete',
    }
    if mode in {'timeout', 'nonzero'}:
        assert not tuple(run_directory.glob('.*.review-result.json'))
        for reviewer_id in ('security', 'portability'):
            assert (
                run_directory
                / f'logs/{reviewer_id}-rejected-review-result-attempt-0001.json'
            ).is_file()
            assert (
                run_directory
                / f'logs/{reviewer_id}-rejected-review-artifact-attempt-0001.md'
            ).is_file()
        audit = build_audit_document(
            store.get(run.id),
            store.list_transitions(str(run.id)),
            (),
            tmp_path / 'runs',
            verify=True,
        )
        assert audit['result'] != 'incomplete'


@pytest.mark.parametrize('mode', ['timeout', 'nonzero', 'invalid', 'blocked'])
def test_mixed_incomplete_reviewer_set_fails_terminally(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Let incomplete required reviews outrank an actionable peer finding."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)

    def execute(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        assert isinstance(request, ReviewerRequest)
        if request.on_started is not None:
            request.on_started()
        document = json.loads(request.request_path.read_text(encoding='utf-8'))
        request.artifact_path.write_text('# Review\n', encoding='utf-8')
        if request.artifact_path.stem.endswith('security'):
            response = _changes_requested_response(document, request.artifact_path)
        elif mode == 'timeout':
            command = 'reviewer'
            raise subprocess.TimeoutExpired(command, 30)
        elif mode == 'nonzero':
            return AgentResult(
                succeeded=False,
                summary='failed',
                stdout='',
                stderr='failure',
                exit_code=1,
            )
        else:
            response = _approved_response(document, request.artifact_path)
            if mode == 'invalid':
                response = {}
            else:
                response['payload']['verdict'] = 'blocked'
        request.response_path.write_text(json.dumps(response), encoding='utf-8')
        return AgentResult(
            succeeded=True,
            summary=mode,
            stdout='',
            stderr='',
            exit_code=0,
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', execute)

    with pytest.raises(WorkerError) as caught:
        run_queued_reviewer_set(
            store=store,
            run=run,
            objective='Review the change.',
            reviewer_plan=ReviewerExecutionPlan(
                'default',
                (
                    _reviewer('security', 'codex', 'openai'),
                    _reviewer('portability', 'claude-code', 'anthropic'),
                ),
            ),
            developer_command=(),
            runs_directory=tmp_path / 'runs',
            developer_timeout_seconds=30,
            max_iterations=3,
            digest_worktree=lambda _path, _base: DIGEST,
            developer_identity=InvocationIdentity(
                vendor='openai', model=None, runtime='codex'
            ),
        )

    assert caught.value.code == 'reviewer_batch_incomplete'
    assert store.get(run.id).state is RunState.FAILED


@pytest.mark.parametrize('failure_point', ['preparation', 'dispatch'])
def test_unexpected_batch_exception_fails_terminally(
    failure_point: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close the lifecycle around preparation and worker-thread exceptions."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)

    def fail(*_args: object, **_kwargs: object) -> Never:
        message = 'injected evidence failure'
        raise OSError(message)

    if failure_point == 'preparation':
        monkeypatch.setattr(worker_module, '_write_json_atomic', fail)
    else:
        monkeypatch.setattr(worker_module, '_execute_reviewer_dispatch', fail)

    with pytest.raises(OSError, match='injected evidence failure'):
        run_queued_reviewer_set(
            store=store,
            run=run,
            objective='Review the change.',
            reviewer_plan=ReviewerExecutionPlan(
                'default',
                (
                    _reviewer('security', 'codex', 'openai'),
                    _reviewer('portability', 'claude-code', 'anthropic'),
                ),
            ),
            developer_command=(),
            runs_directory=tmp_path / 'runs',
            developer_timeout_seconds=30,
            max_iterations=3,
            digest_worktree=lambda _path, _base: DIGEST,
            developer_identity=InvocationIdentity(
                vendor='openai', model=None, runtime='codex'
            ),
        )

    assert store.get(run.id).state is RunState.FAILED


def test_unexpected_adapter_exception_finalizes_partial_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finalize reviewer evidence before propagating an unexpected adapter error."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = RunStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)

    def execute(_adapter: CommandAgentAdapter, request: AgentRequest) -> Never:
        assert isinstance(request, ReviewerRequest)
        if request.on_started is not None:
            request.on_started()
        request.response_path.write_text('{}', encoding='utf-8')
        request.artifact_path.write_text('# Partial review\n', encoding='utf-8')
        message = 'unexpected adapter failure'
        raise RuntimeError(message)

    monkeypatch.setattr(CommandAgentAdapter, 'execute', execute)

    with pytest.raises(RuntimeError, match='unexpected adapter failure'):
        run_queued_reviewer_set(
            store=store,
            run=run,
            objective='Review the change.',
            reviewer_plan=ReviewerExecutionPlan(
                'default',
                (
                    _reviewer('security', 'codex', 'openai'),
                    _reviewer('portability', 'claude-code', 'anthropic'),
                ),
            ),
            developer_command=(),
            runs_directory=tmp_path / 'runs',
            developer_timeout_seconds=30,
            max_iterations=3,
            digest_worktree=lambda _path, _base: DIGEST,
            developer_identity=InvocationIdentity(
                vendor='openai', model=None, runtime='codex'
            ),
        )

    failed = store.get(run.id)
    assert failed.state is RunState.FAILED
    run_directory = next((tmp_path / 'runs').rglob('execution.json')).parent
    assert not tuple(run_directory.glob('.*.review-result.json'))
    assert not tuple((run_directory / 'artifacts').glob('review-*.md'))
    for reviewer_id in ('security', 'portability'):
        assert (
            run_directory
            / f'logs/{reviewer_id}-rejected-review-result-attempt-0001.json'
        ).is_file()
        assert (
            run_directory
            / f'logs/{reviewer_id}-rejected-review-artifact-attempt-0001.md'
        ).is_file()
    audit = build_audit_document(
        failed,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert audit['result'] != 'incomplete'
