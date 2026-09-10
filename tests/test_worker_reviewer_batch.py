"""Integration tests for concurrent reviewer-set worker execution."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
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
from agent_orchestra.store import JobStore
from agent_orchestra.worker import WorkerError, resume_review, run_queued_reviewer_set

if TYPE_CHECKING:
    from collections.abc import Callable
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
    store = JobStore(database)
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
    original_write_json_atomic = worker_module._write_json_atomic
    inspected_in_flight = False

    def inspect_before_aggregate(
        path: Path, document: dict[str, Any], evidence_type: str
    ) -> None:
        """Confirm an active reviewer batch does not require its future aggregate."""

        nonlocal inspected_in_flight
        if evidence_type == 'review_batch_result':
            in_flight = store.get(run.id)
            assert in_flight.state is RunState.REVIEWING
            audit = build_audit_document(
                in_flight,
                store.list_transitions(str(run.id)),
                (),
                tmp_path / 'runs',
                verify=True,
            )
            assert not any(
                item.get('code') == 'message_correlation_failure'
                and item.get('path') == 'review-batches/000001.json'
                for item in cast('list[dict[str, object]]', audit['findings'])
            )
            inspected_in_flight = True
        original_write_json_atomic(path, document, cast('Any', evidence_type))

    monkeypatch.setattr(worker_module, '_write_json_atomic', inspect_before_aggregate)
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
    assert inspected_in_flight
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
    aggregate = json.loads(
        (run_directory / 'review-batches/000001.json').read_text(encoding='utf-8')
    )
    assert aggregate == {
        'schema_version': 1,
        'run_id': str(run.id),
        'iteration': 1,
        'reviewer_set_id': 'default',
        'aggregation_policy': 'all_required',
        'diff_digest': DIGEST,
        'verdict': 'approved',
        'reviewers': [
            {
                'reviewer_id': reviewer_id,
                'outcome': 'approved',
                'result_path': f'messages/000002-{reviewer_id}-review-result.json',
            }
            for reviewer_id in ('security', 'portability')
        ],
        'changes_requested_by': [],
        'blocked_by': [],
        'incomplete_reviewers': [],
    }
    audit = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert audit['result'] == 'verified', audit['findings']
    assert any(
        item['evidence_type'] == 'review_batch_result' and item['verdict'] == 'approved'
        for item in cast('list[dict[str, object]]', audit['history'])
    )
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

    security_result_path = run_directory / 'messages/000002-security-review-result.json'
    security_result = json.loads(security_result_path.read_text(encoding='utf-8'))
    mutations: tuple[Callable[[dict[str, Any]], None], ...] = (
        lambda item: item.update(run_id='another-run'),
        lambda item: item.update(iteration=2),
        lambda item: item['scope'].update(diff_digest='sha256:' + 'b' * 64),
        lambda item: item['payload'].update(
            verdict='changes_requested',
            findings=[
                {
                    'finding_id': 'mismatch',
                    'severity': 'high',
                    'title': 'Mismatch',
                    'path': 'src/example.py',
                    'line': 1,
                    'explanation': 'Mismatched aggregate outcome.',
                    'acceptance_criterion': 'Match the aggregate outcome.',
                }
            ],
        ),
    )
    for mutate in mutations:
        candidate = json.loads(json.dumps(security_result))
        mutate(candidate)
        security_result_path.write_text(json.dumps(candidate), encoding='utf-8')
        mismatched = build_audit_document(
            result,
            store.list_transitions(str(run.id)),
            (),
            tmp_path / 'runs',
            verify=True,
        )
        assert 'message_correlation_failure' in {
            str(item.get('code'))
            for item in cast('list[dict[str, object]]', mismatched['findings'])
        }
    security_result_path.write_text(json.dumps(security_result), encoding='utf-8')

    aggregate['reviewers'][0]['result_path'] = (
        'messages/000002-portability-review-result.json'
    )
    (run_directory / 'review-batches/000001.json').write_text(
        json.dumps(aggregate), encoding='utf-8'
    )
    cross_member = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert 'message_correlation_failure' in {
        str(item.get('code'))
        for item in cast('list[dict[str, object]]', cross_member['findings'])
    }
    aggregate_path = run_directory / 'review-batches/000001.json'
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')
    aggregate['reviewers'][0]['result_path'] = (
        'messages/000002-security-review-result.json'
    )
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')

    aggregate['verdict'] = 'blocked'
    aggregate['reviewers'][0].update(outcome='incomplete', result_path=None)
    aggregate['incomplete_reviewers'] = ['security']
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')
    concealed_result = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert any(
        item.get('code') == 'message_correlation_failure'
        and item.get('path') == 'review-batches/000001.json'
        for item in cast('list[dict[str, object]]', concealed_result['findings'])
    )
    aggregate['verdict'] = 'approved'
    aggregate['reviewers'][0].update(
        outcome='approved',
        result_path='messages/000002-security-review-result.json',
    )
    aggregate['incomplete_reviewers'] = []
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')

    aggregate['iteration'] = 2
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')
    mismatched_iteration = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert any(
        item.get('code') == 'iteration_mismatch'
        and item.get('path') == 'review-batches/000001.json'
        for item in cast('list[dict[str, object]]', mismatched_iteration['findings'])
    )
    aggregate['iteration'] = 1
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')

    aggregate_path.unlink()
    absent = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert any(
        item.get('code') == 'message_correlation_failure'
        and item.get('path') == 'review-batches/000001.json'
        for item in cast('list[dict[str, object]]', absent['findings'])
    )
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')

    execution_path = run_directory / 'execution.json'
    execution = json.loads(execution_path.read_text(encoding='utf-8'))
    execution['reviewer_plan']['reviewers'].reverse()
    execution_path.write_text(json.dumps(execution), encoding='utf-8')
    altered_plan = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert any(
        item.get('code') == 'message_correlation_failure'
        and item.get('path') == 'review-batches/000001.json'
        for item in cast('list[dict[str, object]]', altered_plan['findings'])
    )
    execution['reviewer_plan']['reviewers'].reverse()
    execution_path.write_text(json.dumps(execution), encoding='utf-8')

    contradictory_transitions = tuple(
        replace(item, to_state=RunState.CHANGES_REQUESTED)
        if item.from_state is RunState.REVIEWING
        else item
        for item in store.list_transitions(str(run.id))
    )
    contradictory = build_audit_document(
        result,
        contradictory_transitions,
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert any(
        item.get('code') == 'message_correlation_failure'
        and item.get('path') == 'review-batches/000001.json'
        for item in cast('list[dict[str, object]]', contradictory['findings'])
    )


def test_reviewer_set_mutation_fails_terminally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never strand a reviewer batch after its immutable diff changes."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = JobStore(tmp_path / 'state.db')
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
def test_incomplete_reviewer_set_is_resumable(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupt operationally incomplete batches while blocked results fail."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = JobStore(tmp_path / 'state.db')
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
    expected_state = RunState.FAILED if mode == 'blocked' else RunState.INTERRUPTED
    assert store.get(run.id).state is expected_state
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
    assert not any(
        item.get('code') == 'message_correlation_failure'
        and item.get('path') == 'review-batches/000001.json'
        for item in cast('list[dict[str, object]]', audit['findings'])
    )
    aggregate_path = run_directory / 'review-batches/000001.json'
    assert aggregate_path.exists() is (mode == 'blocked')


def test_resume_reviewer_set_retries_only_incomplete_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve an accepted peer and increment only the missing attempt."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = JobStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)
    calls: list[str] = []

    def execute(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        assert isinstance(request, ReviewerRequest)
        reviewer_id = request.artifact_path.stem.rsplit('-', 1)[-1]
        calls.append(reviewer_id)
        if request.on_started is not None:
            request.on_started()
        if reviewer_id == 'portability' and calls.count(reviewer_id) == 1:
            command = 'reviewer'
            raise subprocess.TimeoutExpired(command, 30)
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

    monkeypatch.setattr(CommandAgentAdapter, 'execute', execute)
    plan = ReviewerExecutionPlan(
        'default',
        (
            _reviewer('security', 'codex', 'openai'),
            _reviewer('portability', 'claude-code', 'anthropic'),
        ),
    )
    with pytest.raises(WorkerError, match='reviewer batch did not complete'):
        run_queued_reviewer_set(
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
        )

    interrupted = store.get(run.id)
    assert interrupted.state is RunState.INTERRUPTED
    run_directory = next((tmp_path / 'runs').rglob('execution.json')).parent
    assert not (run_directory / 'review-batches/000001.json').exists()

    premature_aggregate = run_directory / 'review-batches/000001.json'
    premature_aggregate.parent.mkdir(parents=True, exist_ok=True)
    premature_aggregate.write_text(
        json.dumps(
            {
                'schema_version': 1,
                'run_id': str(run.id),
                'iteration': interrupted.iteration,
                'reviewer_set_id': 'default',
                'aggregation_policy': 'all_required',
                'diff_digest': DIGEST,
                'verdict': 'blocked',
                'reviewers': [
                    {
                        'reviewer_id': reviewer_id,
                        'outcome': 'incomplete',
                        'result_path': None,
                    }
                    for reviewer_id in ('security', 'portability')
                ],
                'changes_requested_by': [],
                'blocked_by': [],
                'incomplete_reviewers': ['security', 'portability'],
            }
        ),
        encoding='utf-8',
    )
    interrupted_audit = build_audit_document(
        interrupted,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert any(
        item.get('code') == 'message_correlation_failure'
        and item.get('path') == 'review-batches/000001.json'
        and item.get('message')
        == 'interrupted reviewer-set iteration must not have an aggregate result'
        for item in cast('list[dict[str, object]]', interrupted_audit['findings'])
    )
    premature_aggregate.unlink()

    active = replace(interrupted, state=RunState.REVIEWING)
    store.update(active, expected_state=RunState.INTERRUPTED)
    with pytest.raises(WorkerError, match='reviewer batch did not complete'):
        resume_review(
            store=store,
            run=active,
            runs_directory=tmp_path / 'runs',
            digest_worktree=lambda _path, _base: DIGEST,
        )
    interrupted = store.get(run.id)
    assert interrupted.state is RunState.INTERRUPTED
    assert calls.count('portability') == 1

    original_latest_attempt = worker_module._latest_reviewer_attempt

    def missing_security_attempt(path: Path, sequence: int, reviewer_id: str) -> object:
        if reviewer_id == 'security':
            return None
        return original_latest_attempt(path, sequence, reviewer_id)

    monkeypatch.setattr(
        worker_module, '_latest_reviewer_attempt', missing_security_attempt
    )
    with pytest.raises(
        WorkerError,
        match='canonical reviewer result lacks a completed successful attempt',
    ) as caught:
        resume_review(
            store=store,
            run=interrupted,
            runs_directory=tmp_path / 'runs',
            digest_worktree=lambda _path, _base: DIGEST,
        )
    assert caught.value.code == 'resume_activation_uncertain'
    assert store.get(run.id).state is RunState.INTERRUPTED
    monkeypatch.setattr(
        worker_module, '_latest_reviewer_attempt', original_latest_attempt
    )

    request_path = run_directory / 'messages/000001-security-review-request.json'
    result_path = run_directory / 'messages/000002-security-review-result.json'
    request = json.loads(request_path.read_text(encoding='utf-8'))
    result_document = json.loads(result_path.read_text(encoding='utf-8'))
    request['run_id'] = result_document['run_id'] = '20260910T000000Z-deadbeef'
    request_path.write_text(json.dumps(request), encoding='utf-8')
    result_path.write_text(json.dumps(result_document), encoding='utf-8')
    with pytest.raises(WorkerError, match='durable run scope'):
        resume_review(
            store=store,
            run=interrupted,
            runs_directory=tmp_path / 'runs',
            digest_worktree=lambda _path, _base: DIGEST,
        )
    assert store.get(run.id).state is RunState.INTERRUPTED
    request['run_id'] = result_document['run_id'] = str(run.id)
    request_path.write_text(json.dumps(request), encoding='utf-8')
    result_path.write_text(json.dumps(result_document), encoding='utf-8')

    original_next_attempt = worker_module._next_reviewer_attempt
    next_attempt_calls: list[str] = []

    def track_next_attempt(path: Path, sequence: int, reviewer_id: str) -> int:
        next_attempt_calls.append(reviewer_id)
        return original_next_attempt(path, sequence, reviewer_id)

    monkeypatch.setattr(worker_module, '_next_reviewer_attempt', track_next_attempt)

    resumed = resume_review(
        store=store,
        run=interrupted,
        runs_directory=tmp_path / 'runs',
        digest_worktree=lambda _path, _base: DIGEST,
    )

    assert resumed.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    assert calls.count('security') == 1
    assert calls.count('portability') == 2
    assert next_attempt_calls == ['portability']
    assert (
        run_directory / 'invocations/000001-reviewer-portability.attempt-0002.json'
    ).is_file()
    assert not (
        run_directory / 'invocations/000001-reviewer-security.attempt-0002.json'
    ).exists()
    assert (run_directory / 'review-batches/000001.json').is_file()


def test_reviewer_batch_sequence_comes_from_canonical_requests(tmp_path: Path) -> None:
    """Derive a later reviewer batch sequence from durable request paths."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    run_directory = worker_module._run_evidence_directory(
        tmp_path / 'runs', str(run.id)
    )
    messages = run_directory / 'messages'
    messages.mkdir(parents=True)
    plan = ReviewerExecutionPlan(
        'default',
        (
            _reviewer('security', 'codex', 'openai'),
            _reviewer('portability', 'claude-code', 'anthropic'),
        ),
    )
    for reviewer_id in ('security', 'portability'):
        path = messages / f'000007-{reviewer_id}-review-request.json'
        path.write_text(
            json.dumps({'run_id': str(run.id), 'iteration': run.iteration}),
            encoding='utf-8',
        )

    assert (
        worker_module._reviewer_batch_sequence(
            run_directory, run=run, reviewer_plan=plan
        )
        == 7
    )


@pytest.mark.parametrize('mode', ['timeout', 'nonzero', 'invalid', 'blocked'])
def test_mixed_incomplete_reviewer_set_is_resumable(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Let incomplete required reviews outrank an actionable peer finding."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = JobStore(tmp_path / 'state.db')
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
    expected_state = RunState.FAILED if mode == 'blocked' else RunState.INTERRUPTED
    assert store.get(run.id).state is expected_state


@pytest.mark.parametrize('failure_point', ['preparation', 'dispatch'])
def test_unexpected_batch_exception_fails_terminally(
    failure_point: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Close the lifecycle around preparation and worker-thread exceptions."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = JobStore(tmp_path / 'state.db')
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
    store = JobStore(tmp_path / 'state.db')
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
