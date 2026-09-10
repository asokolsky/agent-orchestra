"""Integration tests for concurrent reviewer-set worker execution."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from typing import TYPE_CHECKING, Any, Never, cast
from uuid import uuid4

import pytest

from agent_orchestra import evidence as evidence_module
from agent_orchestra import invocations, reviewer_batch_run
from agent_orchestra.adapter.registry import DEFAULT_RUNTIME_REGISTRY
from agent_orchestra.agents import (
    AgentRequest,
    AgentResult,
    CommandAgentAdapter,
    DeveloperRequest,
    ReviewerRequest,
)
from agent_orchestra.audit import build_audit_document
from agent_orchestra.evidence import (
    WorkerError,
)
from agent_orchestra.execution_context import WorkerContext
from agent_orchestra.invocations import InvocationIdentity
from agent_orchestra.models import Run, RunState
from agent_orchestra.reviewer_plan import ReviewerExecution, ReviewerExecutionPlan
from agent_orchestra.store import JobStore
from agent_orchestra.worker import resume_review, run_queued_reviewer_set

if TYPE_CHECKING:
    from collections.abc import Callable

DIGEST = f'sha256:{"a" * 64}'


def test_reviewer_batch_module_does_not_depend_on_worker() -> None:
    """Keep reviewer-batch execution below the worker orchestration facade."""

    source = Path(reviewer_batch_run.__file__).read_text(encoding='utf-8')
    assert 'agent_orchestra.worker' not in source


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
    original_write_json_atomic = evidence_module.write_json_atomic
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

    monkeypatch.setattr(
        reviewer_batch_run, 'write_json_atomic', inspect_before_aggregate
    )
    plan = ReviewerExecutionPlan(
        'default',
        (
            _reviewer('security', 'codex', 'openai'),
            _reviewer('portability', 'claude-code', 'anthropic'),
        ),
    )

    result = run_queued_reviewer_set(
        context=WorkerContext(
            store=store,
            runs_directory=tmp_path / 'runs',
            digest_worktree=lambda _path, _base: DIGEST,
            registry=DEFAULT_RUNTIME_REGISTRY,
        ),
        run=run,
        objective='Review the change.',
        reviewer_plan=plan,
        developer_command=(),
        developer_timeout_seconds=30,
        max_iterations=3,
        developer_identity=InvocationIdentity(
            vendor='openai', model=None, runtime='codex'
        ),
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
    aggregate_text = (run_directory / 'review-batches/000001.json').read_text(
        encoding='utf-8'
    )
    assert aggregate_text.startswith('{\n  "schema_version": 3,')
    aggregate = json.loads(aggregate_text)
    message_id = aggregate.pop('message_id')
    artifact_path = aggregate.pop('artifact_path')
    assert isinstance(message_id, str)
    assert artifact_path == 'artifacts/review-batch-0001.md'
    assert aggregate == {
        'schema_version': 3,
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
        'findings': [],
    }
    aggregate['message_id'] = message_id
    aggregate['artifact_path'] = artifact_path
    assert (
        (run_directory / 'artifacts/review-batch-0001.md')
        .read_text(encoding='utf-8')
        .startswith(f'# Review batch: run {run.id}\n')
    )
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

    aggregate['artifact_path'] = 'artifacts/unrelated.md'
    aggregate_path = run_directory / 'review-batches/000001.json'
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')
    mismatched_artifact = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert any(
        item.get('code') == 'message_correlation_failure'
        and item.get('message')
        == 'review batch artifact path is not canonical evidence'
        for item in cast('list[dict[str, object]]', mismatched_artifact['findings'])
    )
    aggregate['artifact_path'] = artifact_path
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')

    security_result_path = run_directory / 'messages/000002-security-review-result.json'
    security_result = json.loads(security_result_path.read_text(encoding='utf-8'))
    aggregate['message_id'] = security_result['message_id']
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')
    duplicate_identity = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert any(
        item.get('code') == 'message_correlation_failure'
        and item.get('message') == 'canonical evidence contains a duplicate message ID'
        for item in cast('list[dict[str, object]]', duplicate_identity['findings'])
    )
    aggregate['message_id'] = message_id
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')
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


def test_reviewer_set_persists_namespaced_aggregate_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep colliding member finding IDs distinct in canonical batch evidence."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    store = JobStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)

    def request_changes(
        _adapter: CommandAgentAdapter, request: AgentRequest
    ) -> AgentResult:
        """Return the same source finding ID from each required reviewer."""

        assert isinstance(request, ReviewerRequest)
        if request.on_started is not None:
            request.on_started()
        document = json.loads(request.request_path.read_text(encoding='utf-8'))
        request.artifact_path.write_text('# Review\n', encoding='utf-8')
        request.response_path.write_text(
            json.dumps(_changes_requested_response(document, request.artifact_path)),
            encoding='utf-8',
        )
        return AgentResult(
            succeeded=True,
            summary='changes requested',
            stdout='',
            stderr='',
            exit_code=0,
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', request_changes)
    result = run_queued_reviewer_set(
        context=WorkerContext(
            store=store,
            runs_directory=tmp_path / 'runs',
            digest_worktree=lambda _path, _base: DIGEST,
        ),
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
        developer_timeout_seconds=30,
        max_iterations=3,
        developer_identity=InvocationIdentity(
            vendor='openai', model=None, runtime='codex'
        ),
    )

    assert result.state is RunState.CHANGES_REQUESTED
    run_directory = next((tmp_path / 'runs').rglob('execution.json')).parent
    aggregate = json.loads(
        (run_directory / 'review-batches/000001.json').read_text(encoding='utf-8')
    )
    assert [finding['finding_id'] for finding in aggregate['findings']] == [
        'security:finding-1',
        'portability:finding-1',
    ]
    assert [finding['reviewer_id'] for finding in aggregate['findings']] == [
        'security',
        'portability',
    ]
    assert all(
        finding['source_finding_id'] == 'finding-1' for finding in aggregate['findings']
    )
    audit = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert audit['result'] == 'verified', audit['findings']
    aggregate['findings'][0]['title'] = 'Altered aggregate finding'
    aggregate_path = run_directory / 'review-batches/000001.json'
    aggregate_path.write_text(json.dumps(aggregate), encoding='utf-8')
    altered = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert any(
        finding.get('code') == 'message_correlation_failure'
        and finding.get('message') == 'review batch findings differ from member results'
        for finding in cast('list[dict[str, object]]', altered['findings'])
    )


def test_reviewer_set_remediates_rejected_batch_before_next_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give one complete rejected batch to the developer, then review its edit."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    changed = worktree / 'changed.txt'
    new_digest = f'sha256:{"b" * 64}'
    store = JobStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)
    developer_calls = 0

    def execute(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        """Reject the first batch, remediate it once, and approve the next batch."""

        nonlocal developer_calls
        if request.on_started is not None:
            request.on_started()
        document = json.loads(request.request_path.read_text(encoding='utf-8'))
        if isinstance(request, ReviewerRequest):
            if document['iteration'] == 1:
                assert document['payload']['prior_review_path'] is None
            else:
                prior_path = Path(document['payload']['prior_review_path'])
                assert prior_path.name == (
                    f'000002-{request.artifact_path.stem.removeprefix("review-0002-")}'
                    '-review-result.json'
                )
                assert prior_path.is_file()
            request.artifact_path.write_text('# Review\n', encoding='utf-8')
            response = (
                _changes_requested_response(document, request.artifact_path)
                if document['iteration'] == 1
                else _approved_response(document, request.artifact_path)
            )
        else:
            assert isinstance(request, DeveloperRequest)
            developer_calls += 1
            aggregate_path = Path(document['payload']['review_result_path'])
            aggregate = json.loads(aggregate_path.read_text(encoding='utf-8'))
            assert aggregate['message_id'] == document['in_reply_to']
            finding_ids = [item['finding_id'] for item in aggregate['findings']]
            assert finding_ids == [
                'security:finding-1',
                'portability:finding-1',
            ]
            changed.write_text('fixed\n', encoding='utf-8')
            response = {
                'schema_version': 1,
                'message_id': str(uuid4()),
                'in_reply_to': document['message_id'],
                'run_id': document['run_id'],
                'sequence': document['sequence'] + 1,
                'iteration': document['iteration'],
                'message_type': 'developer_handoff',
                'sender': 'developer',
                'recipient': 'orchestrator',
                'created_at': '2026-09-10T20:00:00Z',
                'scope': document['scope'],
                'payload': {
                    'status': 'ready_for_review',
                    'summary': 'Fixed both findings.',
                    'files_changed': ['changed.txt'],
                    'validation': [],
                    'dispositions': [
                        {
                            'finding_id': finding_id,
                            'disposition': 'addressed',
                            'rationale': 'Fixed.',
                        }
                        for finding_id in finding_ids
                    ],
                    'remaining_risks': [],
                },
            }
        request.response_path.write_text(json.dumps(response), encoding='utf-8')
        return AgentResult(
            succeeded=True,
            summary='complete',
            stdout='',
            stderr='',
            exit_code=0,
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', execute)
    result = run_queued_reviewer_set(
        context=WorkerContext(
            store=store,
            runs_directory=tmp_path / 'runs',
            digest_worktree=lambda _path, _base: (
                new_digest if changed.exists() else DIGEST
            ),
        ),
        run=run,
        objective='Review the change.',
        reviewer_plan=ReviewerExecutionPlan(
            'default',
            (
                _reviewer('security', 'codex', 'openai'),
                _reviewer('portability', 'claude-code', 'anthropic'),
            ),
        ),
        developer_command=('developer',),
        developer_timeout_seconds=30,
        max_iterations=3,
        developer_identity=InvocationIdentity(
            vendor='openai', model=None, runtime='codex'
        ),
    )

    assert result.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    assert result.iteration == 2
    assert result.diff_digest == new_digest
    assert developer_calls == 1
    run_directory = next((tmp_path / 'runs').rglob('execution.json')).parent
    assert (run_directory / 'review-batches/000001.json').is_file()
    assert (run_directory / 'review-batches/000002.json').is_file()
    assert (run_directory / 'messages/000003-remediation-request.json').is_file()
    for reviewer_id in ('security', 'portability'):
        assert (
            run_directory / f'messages/000005-{reviewer_id}-review-request.json'
        ).is_file()
    audit = build_audit_document(
        result,
        store.list_transitions(str(run.id)),
        (),
        tmp_path / 'runs',
        verify=True,
    )
    assert audit['result'] == 'verified', audit['findings']


@pytest.mark.parametrize(
    'first_outcome', ['timeout', 'blocked', 'crash_window', 'disagreement']
)
def test_reviewer_set_resume_retries_only_recoverable_developer(
    first_outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resume the aggregate remediation without redispatching completed reviewers."""

    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    changed = worktree / 'changed.txt'
    new_digest = f'sha256:{"b" * 64}'
    store = JobStore(tmp_path / 'state.db')
    store.initialize()
    run = Run.create_local(worktree, worktree, 'HEAD', 'HEAD', DIGEST)
    store.add(run)
    reviewer_calls = 0
    developer_calls = 0

    def execute(_adapter: CommandAgentAdapter, request: AgentRequest) -> AgentResult:
        """Interrupt the first developer attempt and complete its retry."""

        nonlocal developer_calls, reviewer_calls
        if request.on_started is not None:
            request.on_started()
        document = json.loads(request.request_path.read_text(encoding='utf-8'))
        if isinstance(request, ReviewerRequest):
            reviewer_calls += 1
            request.artifact_path.write_text('# Review\n', encoding='utf-8')
            response = (
                _changes_requested_response(document, request.artifact_path)
                if document['iteration'] == 1
                else _approved_response(document, request.artifact_path)
            )
            request.response_path.write_text(json.dumps(response), encoding='utf-8')
            return AgentResult(
                succeeded=True,
                summary='reviewed',
                stdout='',
                stderr='',
                exit_code=0,
            )
        assert isinstance(request, DeveloperRequest)
        developer_calls += 1
        if developer_calls == 1 and first_outcome == 'timeout':
            command = 'developer'
            raise subprocess.TimeoutExpired(command, 30)
        aggregate = json.loads(
            Path(document['payload']['review_result_path']).read_text(encoding='utf-8')
        )
        blocked = developer_calls == 1 and first_outcome == 'blocked'
        disagreement = developer_calls == 1 and first_outcome == 'disagreement'
        if not blocked and not disagreement:
            changed.write_text('fixed\n', encoding='utf-8')
        response = {
            'schema_version': 1,
            'message_id': str(uuid4()),
            'in_reply_to': document['message_id'],
            'run_id': document['run_id'],
            'sequence': document['sequence'] + 1,
            'iteration': document['iteration'],
            'message_type': 'developer_handoff',
            'sender': 'developer',
            'recipient': 'orchestrator',
            'created_at': '2026-09-10T20:00:00Z',
            'scope': document['scope'],
            'payload': {
                'status': 'blocked' if blocked else 'ready_for_review',
                'summary': 'Blocked.' if blocked else 'Fixed.',
                'files_changed': [] if blocked else ['changed.txt'],
                'validation': [],
                'dispositions': [
                    {
                        'finding_id': finding['finding_id'],
                        'disposition': (
                            'blocked'
                            if blocked
                            else 'rejected'
                            if disagreement
                            else 'addressed'
                        ),
                        'rationale': (
                            'Needs retry.'
                            if blocked
                            else 'Not applicable.'
                            if disagreement
                            else 'Fixed.'
                        ),
                    }
                    for finding in aggregate['findings']
                ],
                'remaining_risks': [],
            },
        }
        request.response_path.write_text(json.dumps(response), encoding='utf-8')
        return AgentResult(
            succeeded=True,
            summary='fixed',
            stdout='',
            stderr='',
            exit_code=0,
        )

    monkeypatch.setattr(CommandAgentAdapter, 'execute', execute)
    original_write_json_atomic = cast('Any', reviewer_batch_run).write_json_atomic
    fail_remediation_write = first_outcome == 'crash_window'

    def write_json(path: Path, document: dict[str, Any], evidence_type: str) -> None:
        """Inject a single crash after the rejected batch becomes durable."""

        nonlocal fail_remediation_write
        if evidence_type == 'remediation_request' and fail_remediation_write:
            fail_remediation_write = False
            message = 'injected remediation write failure'
            raise OSError(message)
        original_write_json_atomic(path, document, cast('Any', evidence_type))

    monkeypatch.setattr(reviewer_batch_run, 'write_json_atomic', write_json)
    context = WorkerContext(
        store=store,
        runs_directory=tmp_path / 'runs',
        digest_worktree=lambda _path, _base: new_digest if changed.exists() else DIGEST,
    )
    plan = ReviewerExecutionPlan(
        'default',
        (
            _reviewer('security', 'codex', 'openai'),
            _reviewer('portability', 'claude-code', 'anthropic'),
        ),
    )

    def start() -> Run:
        """Start the reviewer-set workflow under the selected recovery outcome."""

        return run_queued_reviewer_set(
            context=context,
            run=run,
            objective='Review the change.',
            reviewer_plan=plan,
            developer_command=('developer',),
            developer_timeout_seconds=30,
            max_iterations=3,
            developer_identity=InvocationIdentity(
                vendor='openai', model=None, runtime='codex'
            ),
        )

    if first_outcome == 'timeout':
        with pytest.raises(WorkerError, match='developer timed out'):
            start()
        recoverable = store.get(run.id)
        assert recoverable.state is RunState.INTERRUPTED
    elif first_outcome == 'crash_window':
        with pytest.raises(OSError, match='injected remediation write failure'):
            start()
        recoverable = store.get(run.id)
        assert recoverable.state is RunState.CHANGES_REQUESTED
    else:
        recoverable = start()
        assert recoverable.state is (
            RunState.CHANGES_REQUESTED
            if first_outcome == 'disagreement'
            else RunState.VALIDATION_REQUIRED
        )
    assert reviewer_calls == 2
    if first_outcome == 'disagreement':
        marker = json.loads(
            (next((tmp_path / 'runs').rglob('decision-required.json'))).read_text(
                encoding='utf-8'
            )
        )
        handoff_path = Path(marker['developer_handoff_path'])
        handoff = json.loads(handoff_path.read_text(encoding='utf-8'))
        dispositions = handoff['payload']['dispositions']
        handoff['payload']['dispositions'] = dispositions[:-1]
        handoff_path.write_text(json.dumps(handoff), encoding='utf-8')
        with pytest.raises(WorkerError, match='disposition for every finding'):
            resume_review(context=context, run=recoverable)
        handoff['payload']['dispositions'] = dispositions
        handoff_path.write_text(json.dumps(handoff), encoding='utf-8')
        assert resume_review(context=context, run=recoverable) == recoverable
        assert developer_calls == 1
        assert reviewer_calls == 2
        return
    if first_outcome == 'timeout':
        request_path = next((tmp_path / 'runs').rglob('*-remediation-request.json'))
        request = json.loads(request_path.read_text(encoding='utf-8'))
        request['payload']['objective'] = 'Tampered objective.'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        with pytest.raises(WorkerError, match='miscorrelated'):
            resume_review(context=context, run=recoverable)
        request['payload']['objective'] = 'Review the change.'
        request_path.write_text(json.dumps(request), encoding='utf-8')
        batch_path = Path(request['payload']['review_result_path'])
        batch = json.loads(batch_path.read_text(encoding='utf-8'))
        batch['reviewers'][0]['result_path'], batch['reviewers'][1]['result_path'] = (
            batch['reviewers'][1]['result_path'],
            batch['reviewers'][0]['result_path'],
        )
        batch_path.write_text(json.dumps(batch), encoding='utf-8')
        with pytest.raises(WorkerError, match='invalid in_reply_to'):
            resume_review(context=context, run=recoverable)
        batch['reviewers'][0]['result_path'], batch['reviewers'][1]['result_path'] = (
            batch['reviewers'][1]['result_path'],
            batch['reviewers'][0]['result_path'],
        )
        batch['diff_digest'] = new_digest
        request['scope']['diff_digest'] = new_digest
        batch_path.write_text(json.dumps(batch), encoding='utf-8')
        request_path.write_text(json.dumps(request), encoding='utf-8')
        with pytest.raises(WorkerError, match='miscorrelated'):
            resume_review(context=context, run=recoverable)
        batch['diff_digest'] = DIGEST
        request['scope']['diff_digest'] = DIGEST
        batch_path.write_text(json.dumps(batch), encoding='utf-8')
        request_path.write_text(json.dumps(request), encoding='utf-8')
    result = resume_review(context=context, run=recoverable)
    assert result.state is RunState.AWAITING_COMMIT_AUTHORIZATION
    assert reviewer_calls == 4
    assert developer_calls == (1 if first_outcome == 'crash_window' else 2)


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
            context=WorkerContext(
                store=store,
                runs_directory=tmp_path / 'runs',
                digest_worktree=lambda _path, _base: next(digests),
            ),
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
            developer_timeout_seconds=30,
            max_iterations=3,
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
            context=WorkerContext(
                store=store,
                runs_directory=tmp_path / 'runs',
                digest_worktree=lambda _path, _base: DIGEST,
            ),
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
            developer_timeout_seconds=30,
            max_iterations=3,
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
            context=WorkerContext(
                store=store,
                runs_directory=tmp_path / 'runs',
                digest_worktree=lambda _path, _base: DIGEST,
            ),
            run=run,
            objective='Review the change.',
            reviewer_plan=plan,
            developer_command=(),
            developer_timeout_seconds=30,
            max_iterations=3,
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
            context=WorkerContext(
                store=store,
                runs_directory=tmp_path / 'runs',
                digest_worktree=lambda _path, _base: DIGEST,
            ),
            run=active,
        )
    interrupted = store.get(run.id)
    assert interrupted.state is RunState.INTERRUPTED
    assert calls.count('portability') == 1

    original_latest_attempt = reviewer_batch_run._latest_reviewer_attempt

    def missing_security_attempt(path: Path, sequence: int, reviewer_id: str) -> object:
        if reviewer_id == 'security':
            return None
        return original_latest_attempt(path, sequence, reviewer_id)

    monkeypatch.setattr(
        reviewer_batch_run, '_latest_reviewer_attempt', missing_security_attempt
    )
    with pytest.raises(
        WorkerError,
        match='canonical reviewer result lacks a completed successful attempt',
    ) as caught:
        resume_review(
            context=WorkerContext(
                store=store,
                runs_directory=tmp_path / 'runs',
                digest_worktree=lambda _path, _base: DIGEST,
            ),
            run=interrupted,
        )
    assert caught.value.code == 'resume_activation_uncertain'
    assert store.get(run.id).state is RunState.INTERRUPTED
    monkeypatch.setattr(
        reviewer_batch_run, '_latest_reviewer_attempt', original_latest_attempt
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
            context=WorkerContext(
                store=store,
                runs_directory=tmp_path / 'runs',
                digest_worktree=lambda _path, _base: DIGEST,
            ),
            run=interrupted,
        )
    assert store.get(run.id).state is RunState.INTERRUPTED
    request['run_id'] = result_document['run_id'] = str(run.id)
    request_path.write_text(json.dumps(request), encoding='utf-8')
    result_path.write_text(json.dumps(result_document), encoding='utf-8')

    original_next_attempt = reviewer_batch_run._next_reviewer_attempt
    next_attempt_calls: list[str] = []

    def track_next_attempt(path: Path, sequence: int, reviewer_id: str) -> int:
        next_attempt_calls.append(reviewer_id)
        return original_next_attempt(path, sequence, reviewer_id)

    monkeypatch.setattr(
        reviewer_batch_run, '_next_reviewer_attempt', track_next_attempt
    )

    resumed = resume_review(
        context=WorkerContext(
            store=store,
            runs_directory=tmp_path / 'runs',
            digest_worktree=lambda _path, _base: DIGEST,
        ),
        run=interrupted,
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
    run_directory = invocations.prepare_run_evidence_directory(
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
        reviewer_batch_run._reviewer_batch_sequence(
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
            context=WorkerContext(
                store=store,
                runs_directory=tmp_path / 'runs',
                digest_worktree=lambda _path, _base: DIGEST,
            ),
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
            developer_timeout_seconds=30,
            max_iterations=3,
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
        monkeypatch.setattr(reviewer_batch_run, 'write_json_atomic', fail)
    else:
        monkeypatch.setattr(reviewer_batch_run, '_execute_reviewer_dispatch', fail)

    with pytest.raises(OSError, match='injected evidence failure'):
        run_queued_reviewer_set(
            context=WorkerContext(
                store=store,
                runs_directory=tmp_path / 'runs',
                digest_worktree=lambda _path, _base: DIGEST,
            ),
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
            developer_timeout_seconds=30,
            max_iterations=3,
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
            context=WorkerContext(
                store=store,
                runs_directory=tmp_path / 'runs',
                digest_worktree=lambda _path, _base: DIGEST,
            ),
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
            developer_timeout_seconds=30,
            max_iterations=3,
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
