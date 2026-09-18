"""Tests for command-line recovery and resume behavior."""

from __future__ import annotations

import json
import sqlite3
import sys
from threading import Barrier, Lock, Thread
from typing import TYPE_CHECKING, Any

import pytest

from agent_orchestra import (
    developer_remediation,
    invocations,
    queued_review,
    worker,
)
from agent_orchestra import evidence as evidence_module
from agent_orchestra.adapter.registry import (
    RuntimeRole,
)
from agent_orchestra.agents import AgentRequest, AgentResult, CommandAgentAdapter
from agent_orchestra.cli import (
    _working_tree_digest,
    main,
)
from agent_orchestra.evidence import (
    WorkerError,
)
from agent_orchestra.execution_context import (
    ReviewPlan,
    WorkerContext,
)
from agent_orchestra.invocations import (
    AttemptIdentity,
    InvocationEvidenceStore,
    InvocationRecord,
)
from agent_orchestra.models import Run, RunState
from agent_orchestra.queued_review import (
    run_queued_review,
)
from agent_orchestra.store import JobStore
from agent_orchestra.worker import (
    resume_review,
)
from tests.cli_helpers import (
    CliRunContext,
    add_execution_counter,
    configure_agent,
    create_worker_run,
    evidence_directory,
    resume_arguments,
    run_arguments,
    write_developer,
    write_loop_reviewer,
    write_recoverable_developer,
    write_reviewer,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_resume_interrupted_reviewer_reuses_request(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Retry an interrupted reviewer without duplicating its canonical request."""

    reviewer = tmp_path / 'reviewer.py'
    reviewer.write_text('"""Slow reviewer."""\nimport time\ntime.sleep(5)\n')

    assert (
        main(
            run_arguments(
                enqueued_run,
                '--timeout',
                '1',
                reviewer=reviewer,
                objective='Review.',
            )
        )
        == 2
    )
    capsys.readouterr()
    invocation_path = (
        evidence_directory(enqueued_run) / 'invocations/000001-reviewer.json'
    )
    invocation = invocation_path.read_text()
    invocation_path.write_text('{')
    assert main(resume_arguments(enqueued_run)) == 2
    invalid_invocation = json.loads(capsys.readouterr().out)
    assert invalid_invocation['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    invocation_path.write_text(invocation)
    (enqueued_run.repo / 'tracked.txt').write_text('changed again\n')
    assert main(resume_arguments(enqueued_run)) == 2
    changed_scope = json.loads(capsys.readouterr().out)
    assert changed_scope['error']['code'] == 'resume_scope_changed'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    (enqueued_run.repo / 'tracked.txt').write_text('changed\n')
    messages = evidence_directory(enqueued_run) / 'messages'
    request_path = messages / '000001-review-request.json'
    gapped_path = messages / '000003-review-request.json'
    request_path.rename(gapped_path)
    assert main(resume_arguments(enqueued_run)) == 2
    invalid_chain = json.loads(capsys.readouterr().out)
    assert invalid_chain['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    gapped_path.rename(request_path)
    execution_path = evidence_directory(enqueued_run) / 'execution.json'
    execution = json.loads(execution_path.read_text())
    execution['run_id'] = '20260904T000000Z-00000000'
    execution_path.write_text(json.dumps(execution))

    assert main(resume_arguments(enqueued_run)) == 2
    mismatched = json.loads(capsys.readouterr().out)
    assert mismatched['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    execution['run_id'] = enqueued_run.run.id
    execution['schema_version'] = 1
    execution_path.write_text(json.dumps(execution))

    assert main(resume_arguments(enqueued_run)) == 2
    unsupported = json.loads(capsys.readouterr().out)
    assert unsupported['error']['code'] == 'resume_metadata_unsupported'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    execution['schema_version'] = 2
    reviewer_record = execution.pop('reviewer')
    execution['schema_version'] = 3
    execution['reviewer_plan'] = {
        'schema_version': 1,
        'reviewer_set_id': 'default',
        'aggregation_policy': 'all_required',
        'reviewers': [
            {'reviewer_id': reviewer_id, **reviewer_record}
            for reviewer_id in ('security', 'portability')
        ],
    }
    execution_path.write_text(json.dumps(execution))

    assert main(resume_arguments(enqueued_run)) == 2
    invalid_reviewer_set = json.loads(capsys.readouterr().out)
    assert invalid_reviewer_set['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED
    execution['schema_version'] = 2
    execution['reviewer'] = reviewer_record
    execution.pop('reviewer_plan')
    execution_path.write_text(json.dumps(execution))
    write_reviewer(reviewer, 'approved')

    assert main(resume_arguments(enqueued_run)) == 0

    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
    ]
    invocations = evidence_directory(enqueued_run) / 'invocations'
    assert sorted(path.name for path in invocations.iterdir()) == [
        '000001-reviewer-attempt-0002.json',
        '000001-reviewer.json',
    ]
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_reports_unrecognized_interrupted_origin(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Return resume's JSON contract for an unknown transition origin."""

    reviewer = tmp_path / 'reviewer.py'
    reviewer.write_text('"""Slow reviewer."""\nimport time\ntime.sleep(5)\n')
    assert main(run_arguments(enqueued_run, '--timeout', '1', reviewer=reviewer)) == 2
    capsys.readouterr()
    with sqlite3.connect(enqueued_run.database) as connection:
        connection.execute(
            """UPDATE transitions SET from_state = 'future_state'
            WHERE job_id = ? AND to_state = 'interrupted'""",
            (str(enqueued_run.run.id),),
        )

    assert main(resume_arguments(enqueued_run)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'unknown_job_state'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.INTERRUPTED


def test_resume_revalidates_reviewer_response_without_relaunching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enqueued_run: CliRunContext,
) -> None:
    """Recover a response persisted before its validation milestone."""

    reviewer = tmp_path / 'reviewer.py'
    counter = tmp_path / 'reviewer-count.txt'
    write_reviewer(reviewer, 'approved')
    add_execution_counter(reviewer, counter)
    original_write = InvocationEvidenceStore.write

    def fail_validation_record(
        self: InvocationEvidenceStore, path: Path, record: InvocationRecord
    ) -> None:
        """Simulate a crash before the reviewer validation milestone is durable."""

        if (
            record.role == 'reviewer'
            and record.status == 'running'
            and record.validation_started_at is not None
        ):
            message = 'simulated validation milestone failure'
            raise OSError(message)
        original_write(self, path, record)

    monkeypatch.setattr(InvocationEvidenceStore, 'write', fail_validation_record)
    with pytest.raises(OSError, match='simulated validation milestone failure'):
        run_queued_review(
            context=WorkerContext(
                store=enqueued_run.store,
                runs_directory=enqueued_run.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=enqueued_run.run,
            plan=ReviewPlan(
                objective='Review.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(),
                timeout_seconds=30,
            ),
        )
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.REVIEWING

    monkeypatch.setattr(InvocationEvidenceStore, 'write', original_write)
    assert main(resume_arguments(enqueued_run)) == 0

    assert counter.read_text().splitlines() == ['1']
    record = json.loads(
        (
            evidence_directory(enqueued_run) / 'invocations/000001-reviewer.json'
        ).read_text()
    )
    assert record['status'] == 'completed'
    assert record['conclusion'] == 'succeeded'
    assert record['response_received_at'] is not None
    assert record['validation_started_at'] is not None
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_applies_completed_reviewer_conclusion_without_relaunching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enqueued_run: CliRunContext,
) -> None:
    """Recover when a completed review predates its workflow transition."""

    reviewer = tmp_path / 'reviewer.py'
    counter = tmp_path / 'reviewer-count.txt'
    write_reviewer(reviewer, 'approved')
    add_execution_counter(reviewer, counter)
    original_update = enqueued_run.store.update

    def fail_decision(run: Run, *, expected_state: RunState) -> None:
        """Simulate a crash after completion but before the review decision."""

        if run.state is RunState.APPROVED and expected_state is RunState.REVIEWING:
            message = 'simulated workflow transition failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(enqueued_run.store, 'update', fail_decision)
    with pytest.raises(OSError, match='simulated workflow transition failure'):
        run_queued_review(
            context=WorkerContext(
                store=enqueued_run.store,
                runs_directory=enqueued_run.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=enqueued_run.run,
            plan=ReviewPlan(
                objective='Review.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(),
                timeout_seconds=30,
            ),
        )
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.REVIEWING

    monkeypatch.setattr(enqueued_run.store, 'update', original_update)
    assert main(resume_arguments(enqueued_run)) == 0

    assert counter.read_text().splitlines() == ['1']
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_advances_persisted_approval_without_relaunching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enqueued_run: CliRunContext,
) -> None:
    """Recover after approval persists but its authorization wait does not."""

    reviewer = tmp_path / 'reviewer.py'
    counter = tmp_path / 'reviewer-count.txt'
    write_reviewer(reviewer, 'approved')
    add_execution_counter(reviewer, counter)
    original_update = enqueued_run.store.update

    def fail_authorization_wait(run: Run, *, expected_state: RunState) -> None:
        """Simulate a crash immediately after durable approval."""

        if (
            run.state is RunState.AWAITING_COMMIT_AUTHORIZATION
            and expected_state is RunState.APPROVED
        ):
            message = 'simulated authorization transition failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(enqueued_run.store, 'update', fail_authorization_wait)
    with pytest.raises(OSError, match='simulated authorization transition failure'):
        run_queued_review(
            context=WorkerContext(
                store=enqueued_run.store,
                runs_directory=enqueued_run.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=enqueued_run.run,
            plan=ReviewPlan(
                objective='Review.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(),
                timeout_seconds=30,
            ),
        )
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.APPROVED

    monkeypatch.setattr(enqueued_run.store, 'update', original_update)
    assert main(resume_arguments(enqueued_run)) == 0

    assert counter.read_text().splitlines() == ['1']
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_starts_persisted_remediation_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover after a remediation request persists before its active state."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    developer_counter = tmp_path / 'developer-count.txt'
    write_loop_reviewer(reviewer)
    write_developer(developer)
    add_execution_counter(developer, developer_counter)
    original_update = context.store.update

    def fail_developing(run: Run, *, expected_state: RunState) -> None:
        """Simulate a crash after the request but before active development."""

        if (
            run.state is RunState.DEVELOPING
            and expected_state is RunState.CHANGES_REQUESTED
        ):
            message = 'simulated development transition failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(context.store, 'update', fail_developing)
    with pytest.raises(OSError, match='simulated development transition failure'):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review and remediate.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(sys.executable, str(developer)),
                timeout_seconds=30,
                developer_timeout_seconds=30,
                max_iterations=3,
            ),
        )
    assert context.store.get(context.run.id).state is RunState.CHANGES_REQUESTED
    assert (
        evidence_directory(context) / 'messages/000003-remediation-request.json'
    ).is_file()

    monkeypatch.setattr(context.store, 'update', original_update)
    assert main(resume_arguments(context)) == 0

    assert developer_counter.read_text().splitlines() == ['1']
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_recovered_review_survives_pre_attempt_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume again when recovered review acceptance predates attempt evidence."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    developer_counter = tmp_path / 'developer-count.txt'
    write_loop_reviewer(reviewer)
    write_developer(developer)
    add_execution_counter(developer, developer_counter)
    original_update = context.store.update

    def fail_review_decision(run: Run, *, expected_state: RunState) -> None:
        """Simulate a crash before the accepted review decision persists."""

        if (
            run.state is RunState.CHANGES_REQUESTED
            and expected_state is RunState.REVIEWING
        ):
            message = 'simulated review decision failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(context.store, 'update', fail_review_decision)
    with pytest.raises(OSError, match='simulated review decision failure'):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review and remediate.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(sys.executable, str(developer)),
                timeout_seconds=30,
                developer_timeout_seconds=30,
                max_iterations=3,
            ),
        )
    assert context.store.get(context.run.id).state is RunState.REVIEWING

    monkeypatch.setattr(context.store, 'update', original_update)
    original_record = invocations.record_invocation

    def fail_developer_record(
        attempt: AttemptIdentity, *args: Any, **kwargs: Any
    ) -> str:
        """Simulate a crash after activating development but before evidence."""

        if attempt.role is RuntimeRole.DEVELOPER:
            message = 'simulated developer record failure'
            raise OSError(message)
        return original_record(attempt, *args, **kwargs)

    monkeypatch.setattr(
        developer_remediation, 'record_invocation', fail_developer_record
    )
    with pytest.raises(OSError, match='simulated developer record failure'):
        resume_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.store.get(context.run.id),
        )
    assert context.store.get(context.run.id).state is RunState.DEVELOPING

    monkeypatch.setattr(developer_remediation, 'record_invocation', original_record)
    assert main(resume_arguments(context)) == 0

    assert developer_counter.read_text().splitlines() == ['1']
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )


def test_resume_reports_explicit_execution_failure_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Classify a failed retry without matching its human-readable message."""

    reviewer = tmp_path / 'reviewer.py'
    reviewer.write_text('"""Slow reviewer."""\nimport time\ntime.sleep(5)\n')
    assert main(run_arguments(enqueued_run, '--timeout', '1', reviewer=reviewer)) == 2
    capsys.readouterr()
    reviewer.unlink()

    assert main(resume_arguments(enqueued_run)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_execution_failed'
    failure = json.loads(
        (evidence_directory(enqueued_run) / 'failure.json').read_text()
    )
    assert failure['error']['code'] == document['error']['code']


def test_resume_rejects_stale_artifact_from_interrupted_reviewer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    enqueued_run: CliRunContext,
) -> None:
    """Require a retried reviewer to create a fresh human artifact."""

    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'approved')
    configure_agent(
        reviewer,
        artifact_content='# Stale review\n',
        sleep_seconds=5,
    )

    assert main(run_arguments(enqueued_run, '--timeout', '1', reviewer=reviewer)) == 2
    capsys.readouterr()
    run_directory = evidence_directory(enqueued_run)
    artifact_path = run_directory / 'artifacts/review-0001.md'
    archived_path = (
        run_directory / 'logs/000002-rejected-review-artifact-attempt-0001.md'
    )
    assert not artifact_path.exists()
    assert archived_path.read_text() == '# Stale review\n'
    integrity = json.loads((run_directory / '.integrity.json').read_text())
    indexed = {entry['path']: entry for entry in integrity['entries']}
    assert 'artifacts/review-0001.md' not in indexed
    archived_relative = 'logs/000002-rejected-review-artifact-attempt-0001.md'
    assert indexed[archived_relative]['evidence_type'] == 'rejected_review_artifact'
    assert all((run_directory / path).is_file() for path in indexed)

    write_reviewer(reviewer, 'approved', write_artifact=False)
    assert main(resume_arguments(enqueued_run)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert enqueued_run.store.get(enqueued_run.run.id).state is RunState.FAILED


def test_resume_interrupted_developer_reuses_remediation_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Retry an interrupted developer without starting a replacement run."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    developer.write_text('"""Slow developer."""\nimport time\ntime.sleep(5)\n')

    with pytest.raises(WorkerError, match='developer timed out'):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review and remediate.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(sys.executable, str(developer)),
                timeout_seconds=30,
                developer_timeout_seconds=1,
                max_iterations=3,
            ),
        )

    assert context.store.get(context.run.id).state is RunState.INTERRUPTED
    invocation_path = evidence_directory(context) / 'invocations/000003-developer.json'
    invocation = invocation_path.read_text()
    first_attempt = json.loads(invocation)
    invocation_path.write_text('{')
    assert main(resume_arguments(context)) == 2
    invalid_invocation = json.loads(capsys.readouterr().out)
    assert invalid_invocation['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.INTERRUPTED
    invocation_path.write_text(invocation)
    write_developer(developer)
    assert main(resume_arguments(context)) == 0
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    messages = evidence_directory(context) / 'messages'
    assert sorted(path.name for path in messages.iterdir()) == [
        '000001-review-request.json',
        '000002-review-result.json',
        '000003-remediation-request.json',
        '000004-developer-handoff.json',
        '000005-review-request.json',
        '000006-review-result.json',
    ]
    invocations = evidence_directory(context) / 'invocations'
    assert (invocations / '000003-developer.json').is_file()
    retry = json.loads((invocations / '000003-developer-attempt-0002.json').read_text())
    assert retry['attempt'] == 2
    assert retry['timed_out'] is False
    assert retry['task_id'] == first_attempt['task_id']
    assert first_attempt['invocation_id'] == (
        f'{context.run.id}:000003-developer:attempt-0001'
    )
    assert retry['invocation_id'] == (f'{context.run.id}:000003-developer:attempt-0002')
    assert first_attempt['conclusion'] == 'timed_out'
    assert retry['conclusion'] == 'succeeded'


def test_resume_revalidates_developer_response_without_relaunching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover a developer response after edits but before validation is durable."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    counter = tmp_path / 'developer-count.txt'
    write_loop_reviewer(reviewer)
    write_developer(developer)
    add_execution_counter(developer, counter)
    original_write = InvocationEvidenceStore.write

    def fail_validation_record(
        self: InvocationEvidenceStore, path: Path, record: InvocationRecord
    ) -> None:
        """Simulate a crash before the developer validation milestone is durable."""

        if (
            record.role == 'developer'
            and record.status == 'running'
            and record.validation_started_at is not None
        ):
            message = 'simulated developer validation milestone failure'
            raise OSError(message)
        original_write(self, path, record)

    monkeypatch.setattr(InvocationEvidenceStore, 'write', fail_validation_record)
    with pytest.raises(
        OSError, match='simulated developer validation milestone failure'
    ):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review and remediate.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(sys.executable, str(developer)),
                timeout_seconds=30,
                max_iterations=3,
            ),
        )
    assert context.store.get(context.run.id).state is RunState.DEVELOPING

    monkeypatch.setattr(InvocationEvidenceStore, 'write', original_write)
    assert main(resume_arguments(context)) == 0

    assert counter.read_text().splitlines() == ['1']
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    record = json.loads(
        (evidence_directory(context) / 'invocations/000003-developer.json').read_text()
    )
    assert record['status'] == 'completed'
    assert record['conclusion'] == 'succeeded'


def test_resume_writes_recovery_request_before_activating_developer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep validation recoverable when its next request cannot be persisted."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    blocked = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=3,
        ),
    )
    assert blocked.state is RunState.VALIDATION_REQUIRED
    original_write = evidence_module.write_json_atomic

    def fail_recovery_request(
        path: Path, document: dict[str, object], evidence_type: Any
    ) -> None:
        """Simulate failure to persist only the recovery request."""

        if path.name == '000005-remediation-request.json':
            message = 'simulated write failure'
            raise OSError(message)
        original_write(path, document, evidence_type)

    monkeypatch.setattr(worker, 'write_json_atomic', fail_recovery_request)
    assert main(resume_arguments(context)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'resume_evidence_invalid'
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED
    messages = evidence_directory(context) / 'messages'
    assert not (messages / '000005-remediation-request.json').exists()


def test_resume_recovers_request_when_activation_state_did_not_persist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Launch after a recovery request persists but its active state does not."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    write_loop_reviewer(reviewer)
    write_recoverable_developer(developer)
    blocked = run_queued_review(
        context=WorkerContext(
            store=context.store,
            runs_directory=context.runs_directory,
            digest_worktree=_working_tree_digest,
        ),
        run=context.run,
        plan=ReviewPlan(
            objective='Review and remediate.',
            reviewer_command=(sys.executable, str(reviewer)),
            developer_command=(sys.executable, str(developer)),
            timeout_seconds=30,
            max_iterations=3,
        ),
    )
    assert blocked.state is RunState.VALIDATION_REQUIRED
    original_update = context.store.update

    def fail_activation(run: Run, *, expected_state: RunState) -> None:
        """Simulate failure while activating the recovery developer."""

        if (
            run.state is RunState.DEVELOPING
            and expected_state is RunState.VALIDATION_REQUIRED
        ):
            message = 'simulated activation failure'
            raise OSError(message)
        original_update(run, expected_state=expected_state)

    monkeypatch.setattr(context.store, 'update', fail_activation)
    with pytest.raises(OSError, match='simulated activation failure'):
        resume_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=blocked,
        )
    messages = evidence_directory(context) / 'messages'
    assert (messages / '000005-remediation-request.json').is_file()
    assert context.store.get(context.run.id).state is RunState.VALIDATION_REQUIRED
    invocations = evidence_directory(context) / 'invocations'
    assert not (invocations / '000005-developer.json').exists()

    monkeypatch.setattr(context.store, 'update', original_update)
    assert main(resume_arguments(context)) == 0
    assert (
        context.store.get(context.run.id).state
        is RunState.AWAITING_COMMIT_AUTHORIZATION
    )
    assert (invocations / '000005-developer.json').is_file()


@pytest.mark.parametrize('role', ['reviewer', 'developer'])
def test_concurrent_active_resumes_launch_one_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    """Let only the atomic first-attempt creator activate the selected role."""

    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    developer = tmp_path / 'developer.py'
    if role == 'reviewer':
        write_reviewer(reviewer, 'approved')
    else:
        write_loop_reviewer(reviewer)
    write_developer(developer)
    original_record = invocations.record_invocation

    def crash_before_attempt(
        attempt: AttemptIdentity, *args: Any, **kwargs: Any
    ) -> str:
        """Leave the selected role active with a request but no attempt record."""

        if attempt.role == role:
            message = 'simulated crash before attempt persistence'
            raise OSError(message)
        return original_record(attempt, *args, **kwargs)

    monkeypatch.setattr(queued_review, 'record_invocation', crash_before_attempt)
    with pytest.raises(OSError, match='simulated crash before attempt persistence'):
        run_queued_review(
            context=WorkerContext(
                store=context.store,
                runs_directory=context.runs_directory,
                digest_worktree=_working_tree_digest,
            ),
            run=context.run,
            plan=ReviewPlan(
                objective='Review and remediate.',
                reviewer_command=(sys.executable, str(reviewer)),
                developer_command=(sys.executable, str(developer)),
                timeout_seconds=30,
                max_iterations=3,
            ),
        )
    monkeypatch.setattr(queued_review, 'record_invocation', original_record)
    active = context.store.get(context.run.id)
    expected_state = RunState.REVIEWING if role == 'reviewer' else RunState.DEVELOPING
    assert active.state is expected_state

    sequence = 1 if role == 'reviewer' else 3
    target = evidence_directory(context) / 'invocations' / f'{sequence:06d}-{role}.json'
    path_type = type(target)
    real_exists = path_type.exists
    barrier = Barrier(2)
    lock = Lock()
    initial_checks = 0

    def synchronized_exists(candidate: Path) -> bool:
        """Give both resumers the same pre-creation view of the attempt path."""

        nonlocal initial_checks
        should_wait = False
        if candidate == target:
            with lock:
                if initial_checks < 2:
                    initial_checks += 1
                    should_wait = True
        if should_wait:
            barrier.wait(timeout=5)
            return False
        return real_exists(candidate)

    monkeypatch.setattr(path_type, 'exists', synchronized_exists)
    original_execute = CommandAgentAdapter.execute
    activations: list[str] = []

    def count_execute(
        adapter: CommandAgentAdapter, request: AgentRequest
    ) -> AgentResult:
        """Count process activations for the selected role."""

        if request.role == role:
            activations.append(role)
        return original_execute(adapter, request)

    monkeypatch.setattr(CommandAgentAdapter, 'execute', count_execute)
    outcomes: list[str] = []

    def resume_once() -> None:
        """Resume through an independent store handle like a separate CLI process."""

        try:
            resume_review(
                context=WorkerContext(
                    store=JobStore(context.database),
                    runs_directory=context.runs_directory,
                    digest_worktree=_working_tree_digest,
                ),
                run=active,
            )
        except WorkerError as error:
            outcomes.append(error.code or type(error).__name__)
        except BaseException as error:  # pragma: no cover - assertion reports type
            outcomes.append(type(error).__name__)
        else:
            outcomes.append('success')

    threads = (Thread(target=resume_once), Thread(target=resume_once))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) == ['resume_activation_uncertain', 'success']
    assert activations == [role]


def test_resume_does_not_fail_a_review_only_run_at_its_limit(
    tmp_path: Path, enqueued_run: CliRunContext, capsys: pytest.CaptureFixture[str]
) -> None:
    """Leave a completed review-only run at its verdict when resume is attempted."""

    # A review-only run has already reached its outcome. Resume previously
    # checked the iteration budget first and rewrote that durable
    # changes_requested into failed, destroying the review's result.
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'changes_requested')
    assert (
        main(run_arguments(enqueued_run, '--max-iterations', '1', reviewer=reviewer))
        == 0
    )
    capsys.readouterr()
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state is RunState.CHANGES_REQUESTED
    )

    result = main(
        [
            '--database',
            str(enqueued_run.database),
            'resume',
            str(enqueued_run.run.id),
            '--runs-directory',
            str(enqueued_run.runs_directory),
        ]
    )

    assert result == 2
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'job_not_resumable'
    assert (
        enqueued_run.store.get(enqueued_run.run.id).state is RunState.CHANGES_REQUESTED
    )


def test_resume_revalidates_a_review_only_run_at_its_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover a review-only reviewer response without failing on the budget."""

    # This reaches _resume_reviewer_validation, the one reordered site the other
    # tests do not exercise: the reviewer persisted a changes_requested response
    # but the workflow transition out of REVIEWING never landed. Resume must
    # apply that verdict, not fail the job on a budget it could never spend.
    context = create_worker_run(tmp_path)
    reviewer = tmp_path / 'reviewer.py'
    write_reviewer(reviewer, 'changes_requested')
    original_update = JobStore.update
    crash_leaving_reviewing = True

    def update(self: JobStore, updated: Run, *, expected_state: RunState) -> None:
        """Fail the first transition that leaves REVIEWING."""

        nonlocal crash_leaving_reviewing
        if crash_leaving_reviewing and expected_state is RunState.REVIEWING:
            crash_leaving_reviewing = False
            message = 'simulated reviewing transition failure'
            raise OSError(message)
        return original_update(self, updated, expected_state=expected_state)

    worker_context = WorkerContext(
        store=context.store,
        runs_directory=context.runs_directory,
        digest_worktree=_working_tree_digest,
    )
    plan = ReviewPlan(
        objective='Review the change.',
        reviewer_command=(sys.executable, str(reviewer)),
        developer_command=(),
        timeout_seconds=30,
        max_iterations=1,
    )
    monkeypatch.setattr(JobStore, 'update', update)
    with pytest.raises(OSError, match='simulated reviewing transition failure'):
        run_queued_review(context=worker_context, run=context.run, plan=plan)

    recovered = resume_review(
        context=worker_context, run=context.store.get(context.run.id)
    )

    assert recovered.state is RunState.CHANGES_REQUESTED
    assert context.store.get(context.run.id).state is RunState.CHANGES_REQUESTED
