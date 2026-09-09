"""Tests for safe persistent evidence retention."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from agent_orchestra import retention
from agent_orchestra.cli import main
from agent_orchestra.evidence import JobEvidence, resolve_evidence_path
from agent_orchestra.models import IssueJob, Run, RunState
from agent_orchestra.retention import (
    RetentionError,
    apply_prune_plan,
    build_prune_plan,
)
from agent_orchestra.store import RunNotFoundError, RunStore

if TYPE_CHECKING:
    from pathlib import Path


def _terminal_source_job(tmp_path: Path) -> tuple[Path, Path, Run]:
    """Create one old failed job with indexed evidence."""

    database = tmp_path / 'state.db'
    runs = tmp_path / 'runs'
    store = RunStore(database)
    store.initialize()
    job = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(job)
    old = datetime.now(UTC) - timedelta(days=100)
    failed = replace(job, state=RunState.FAILED, updated_at=old)
    store.update(failed, RunState.QUEUED)
    evidence = resolve_evidence_path(runs, str(job.id), 'failure.json')
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{}\n')
    JobEvidence(runs, str(job.id)).record_finalized(evidence, 'failure')
    return database, runs, failed


def test_prune_defaults_to_dry_run_and_skips_active_jobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Select only old explicitly enumerated terminal states without mutation."""

    database, runs, failed = _terminal_source_job(tmp_path)
    store = RunStore(database)
    active = Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest')
    store.add(active)
    before = resolve_evidence_path(runs, str(failed.id), 'failure.json').read_bytes()

    assert (
        main(
            [
                '--database',
                str(database),
                'prune',
                '--runs-directory',
                str(runs),
                '--older-than',
                '30d',
            ]
        )
        == 0
    )

    document = json.loads(capsys.readouterr().out)
    assert document['mode'] == 'dry_run'
    assert [item['job_id'] for item in document['selected']] == [str(failed.id)]
    assert document['skipped'][0]['reason'] == 'state_not_eligible'
    assert (
        resolve_evidence_path(runs, str(failed.id), 'failure.json').read_bytes()
        == before
    )


def test_applied_prune_leaves_auditable_expiry_marker_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Expire evidence intentionally and make a repeated application a no-op."""

    database, runs, failed = _terminal_source_job(tmp_path)
    arguments = [
        '--database',
        str(database),
        'prune',
        '--runs-directory',
        str(runs),
        '--older-than',
        '30d',
        '--apply',
    ]
    assert main(arguments) == 0
    capsys.readouterr()

    job_directory = resolve_evidence_path(runs, str(failed.id))
    assert {path.name for path in job_directory.iterdir()} == {'.retention.json'}
    assert main(arguments) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated['selected'] == []
    assert repeated['skipped'][0]['reason'] == 'already_expired'

    assert (
        main(
            [
                '--database',
                str(database),
                'audit',
                str(failed.id),
                '--runs-directory',
                str(runs),
                '--verify',
            ]
        )
        == 0
    )
    audit = json.loads(capsys.readouterr().out)
    assert audit['schema_version'] == 14
    assert audit['result'] == 'expired'
    assert 'evidence_expired' in {finding['code'] for finding in audit['findings']}


def test_orphan_apply_refuses_an_unrelated_database(tmp_path: Path) -> None:
    """Refuse orphan deletion when every evidence directory is unmatched."""

    database = tmp_path / 'state.db'
    RunStore(database).initialize()
    runs = tmp_path / 'runs'
    orphan = resolve_evidence_path(runs, '20260908T000000Z-deadbeef')
    orphan.mkdir(parents=True)
    (orphan / 'failure.json').write_text('{}\n')

    with pytest.raises(RetentionError, match='orphan pruning refused'):
        build_prune_plan(
            RunStore(database),
            database,
            runs,
            older_than_days=30,
            include_orphans=True,
            delete_database_records=False,
        )


def test_orphan_apply_requires_valid_identity_and_a_database_match(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Delete only an explicitly selected, correlated orphan directory."""

    database, runs, known = _terminal_source_job(tmp_path)
    orphan_id = '20260908T000000Z-deadbeef'
    orphan_file = resolve_evidence_path(runs, orphan_id, 'failure.json')
    orphan_file.parent.mkdir(parents=True)
    orphan_file.write_text('{}\n')
    JobEvidence(runs, orphan_id).record_finalized(orphan_file, 'failure')

    assert (
        main(
            [
                '--database',
                str(database),
                'prune',
                '--runs-directory',
                str(runs),
                '--older-than',
                '30d',
                '--orphans',
                '--apply',
            ]
        )
        == 0
    )

    document = json.loads(capsys.readouterr().out)
    assert document['outcomes'] == [
        {'job_id': str(known.id), 'status': 'applied'},
        {'job_id': orphan_id, 'status': 'applied'},
    ]
    assert not orphan_file.parent.exists()


def test_malformed_orphan_is_reported_and_not_deleted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exclude malformed unmatched evidence from an otherwise safe plan."""

    database, runs, _ = _terminal_source_job(tmp_path)
    orphan = resolve_evidence_path(runs, '20260908T000000Z-deadbeef')
    orphan.mkdir(parents=True)
    (orphan / '.integrity.json').write_text('{}\n')

    assert (
        main(
            [
                '--database',
                str(database),
                'prune',
                '--runs-directory',
                str(runs),
                '--orphans',
                '--apply',
            ]
        )
        == 0
    )

    document = json.loads(capsys.readouterr().out)
    assert document['orphans'] == []
    assert document['invalid_paths'] == [str(orphan)]
    assert orphan.exists()


def test_database_cleanup_is_explicit_and_transactional(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Remove job history only when the separate cleanup option is present."""

    database, runs, failed = _terminal_source_job(tmp_path)

    assert (
        main(
            [
                '--database',
                str(database),
                'prune',
                '--runs-directory',
                str(runs),
                '--older-than',
                '30d',
                '--delete-database-records',
                '--apply',
            ]
        )
        == 0
    )
    capsys.readouterr()

    with pytest.raises(RunNotFoundError, match=str(failed.id)):
        RunStore(database).get(str(failed.id))
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            'SELECT COUNT(*) FROM transitions WHERE job_id = ?', (str(failed.id),)
        ).fetchone() == (0,)


def test_prune_uses_issue_review_terminal_transition_age(tmp_path: Path) -> None:
    """Apply the same explicit terminal-state policy to issue-review jobs."""

    database = tmp_path / 'state.db'
    runs = tmp_path / 'runs'
    store = RunStore(database)
    store.initialize()
    job = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url='https://github.com/acme/widgets/issues/16',
        namespace='acme',
        project='widgets',
        issue_number=16,
        title='Retention',
        author='octocat',
        source_updated_at='2026-01-01T00:00:00Z',
        source_digest='digest',
    )
    store.add_issue(job)
    old = datetime.now(UTC) - timedelta(days=100)
    failed = replace(job, state=RunState.FAILED, updated_at=old)
    store.update_issue(failed, RunState.QUEUED)
    evidence = resolve_evidence_path(runs, job.id, 'failure.json')
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{}\n')
    JobEvidence(runs, job.id).record_finalized(evidence, 'failure')

    plan = build_prune_plan(
        store,
        database,
        runs,
        older_than_days=30,
        include_orphans=False,
        delete_database_records=False,
    )

    assert [item.job_id for item in plan.selected] == [job.id]


def test_interrupted_evidence_cleanup_resumes_to_completed_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Retry a cleanup that stopped after publishing its pending marker."""

    database, runs, failed = _terminal_source_job(tmp_path)
    plan = build_prune_plan(
        RunStore(database),
        database,
        runs,
        older_than_days=30,
        include_orphans=False,
        delete_database_records=False,
    )
    clear = retention._clear_directory
    calls = 0

    def interrupt_once(item: retention.PruneItem, *, keep_marker: bool) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = 'simulated interruption'
            raise OSError(message)
        clear(item, keep_marker=keep_marker)

    monkeypatch.setattr(retention, '_clear_directory', interrupt_once)
    assert apply_prune_plan(plan)[0]['status'] == 'failed'
    marker = resolve_evidence_path(runs, str(failed.id), '.retention.json')
    assert json.loads(marker.read_text())['status'] == 'pending'
    assert (
        main(
            [
                '--database',
                str(database),
                'audit',
                str(failed.id),
                '--runs-directory',
                str(runs),
                '--verify',
            ]
        )
        == 0
    )
    audit = json.loads(capsys.readouterr().out)
    assert audit['schema_version'] == 14
    assert audit['result'] == 'incomplete'

    retry = build_prune_plan(
        RunStore(database),
        database,
        runs,
        older_than_days=30,
        include_orphans=False,
        delete_database_records=False,
    )
    assert apply_prune_plan(retry)[0]['status'] == 'applied'
    assert json.loads(marker.read_text())['status'] == 'completed'


def test_apply_refuses_job_that_changed_state_after_preview(tmp_path: Path) -> None:
    """Preserve evidence when current workflow state no longer matches the plan."""

    database, runs, failed = _terminal_source_job(tmp_path)
    store = RunStore(database)
    plan = build_prune_plan(
        store,
        database,
        runs,
        older_than_days=30,
        include_orphans=False,
        delete_database_records=False,
    )
    resumed = replace(failed, state=RunState.REVIEWING, updated_at=datetime.now(UTC))
    store.update(resumed, RunState.FAILED)

    outcome = apply_prune_plan(plan)

    assert outcome[0]['status'] == 'failed'
    assert 'state changed after preview' in outcome[0]['error']
    assert resolve_evidence_path(runs, str(failed.id), 'failure.json').is_file()


def test_failed_database_cleanup_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain enough completed state to retry database deletion independently."""

    database, runs, failed = _terminal_source_job(tmp_path)
    plan = build_prune_plan(
        RunStore(database),
        database,
        runs,
        older_than_days=30,
        include_orphans=False,
        delete_database_records=True,
    )
    delete = retention._delete_database_records
    calls = 0

    def fail_once(connection: sqlite3.Connection, item: retention.PruneItem) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = 'simulated database failure'
            raise sqlite3.OperationalError(message)
        delete(connection, item)

    monkeypatch.setattr(retention, '_delete_database_records', fail_once)
    assert apply_prune_plan(plan)[0]['status'] == 'failed'
    marker = resolve_evidence_path(runs, str(failed.id), '.retention.json')
    assert json.loads(marker.read_text())['status'] == 'completed'

    retry = build_prune_plan(
        RunStore(database),
        database,
        runs,
        older_than_days=30,
        include_orphans=False,
        delete_database_records=True,
    )
    assert retry.selected[0].action == 'delete_database_records'
    assert apply_prune_plan(retry)[0]['status'] == 'applied'
    with pytest.raises(RunNotFoundError):
        RunStore(database).get(str(failed.id))
    assert {path.name for path in marker.parent.iterdir()} == {'.retention.json'}


def test_completed_expiry_reports_unexpected_new_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail verification when content appears after completed expiry."""

    database, runs, failed = _terminal_source_job(tmp_path)
    plan = build_prune_plan(
        RunStore(database),
        database,
        runs,
        older_than_days=30,
        include_orphans=False,
        delete_database_records=False,
    )
    assert apply_prune_plan(plan)[0]['status'] == 'applied'
    unexpected = resolve_evidence_path(runs, str(failed.id), 'unexpected.json')
    unexpected.write_text('{}\n')

    assert (
        main(
            [
                '--database',
                str(database),
                'audit',
                str(failed.id),
                '--runs-directory',
                str(runs),
                '--verify',
            ]
        )
        == 0
    )

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    assert 'unexpected_evidence_after_expiry' in {
        finding['code'] for finding in document['findings']
    }


def test_symlinked_job_candidate_is_reported_without_blocking_other_jobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skip a symlinked candidate and retain stable JSON while pruning safe jobs."""

    database, runs, failed = _terminal_source_job(tmp_path)
    linked_id = '20260908T000000Z-deadbeef'
    linked = resolve_evidence_path(runs, linked_id)
    linked.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / 'outside-evidence'
    target.mkdir()
    linked.symlink_to(target, target_is_directory=True)
    arguments = [
        '--database',
        str(database),
        'prune',
        '--runs-directory',
        str(runs),
        '--older-than',
        '30d',
    ]

    assert main(arguments) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview['invalid_paths'] == [str(linked)]
    assert [item['job_id'] for item in preview['selected']] == [str(failed.id)]

    assert main([*arguments, '--apply']) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied['outcomes'] == [{'job_id': str(failed.id), 'status': 'applied'}]
    assert linked.is_symlink()
    assert target.is_dir()


def test_stored_job_with_unsafe_evidence_path_is_skipped_not_fatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skip a stored job whose own evidence path cannot be safely resolved."""

    database, runs, failed = _terminal_source_job(tmp_path)
    store = RunStore(database)
    old = datetime.now(UTC) - timedelta(days=100)
    unsafe = replace(
        Run.create_local(tmp_path, tmp_path, 'base', 'head', 'digest'),
        state=RunState.FAILED,
        updated_at=old,
    )
    store.add(replace(unsafe, state=RunState.QUEUED))
    store.update(unsafe, RunState.QUEUED)
    # The stored job's own evidence directory is a symlink, so resolving it
    # raises. The other job must still be prunable.
    linked = resolve_evidence_path(runs, str(unsafe.id))
    linked.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / 'outside-stored-evidence'
    target.mkdir()
    linked.symlink_to(target, target_is_directory=True)
    arguments = [
        '--database',
        str(database),
        'prune',
        '--runs-directory',
        str(runs),
        '--older-than',
        '30d',
    ]

    assert main(arguments) == 0
    preview = json.loads(capsys.readouterr().out)

    assert [item['job_id'] for item in preview['selected']] == [str(failed.id)]
    assert {item['job_id']: item['reason'] for item in preview['skipped']} == {
        str(unsafe.id): 'evidence_path_unsafe'
    }
    assert linked.is_symlink()
    assert target.is_dir()


def test_file_named_like_a_shard_does_not_block_pruning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a non-directory year entry instead of failing the whole command."""

    database, runs, failed = _terminal_source_job(tmp_path)
    impostor = runs / '2025'
    impostor.write_text('not a shard\n')
    arguments = [
        '--database',
        str(database),
        'prune',
        '--runs-directory',
        str(runs),
        '--older-than',
        '30d',
    ]

    assert main(arguments) == 0
    preview = json.loads(capsys.readouterr().out)

    assert str(impostor) in preview['invalid_paths']
    assert [item['job_id'] for item in preview['selected']] == [str(failed.id)]

    assert main([*arguments, '--apply']) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied['outcomes'] == [{'job_id': str(failed.id), 'status': 'applied'}]
    assert impostor.is_file()


def test_unreadable_shard_does_not_block_pruning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report a shard that cannot be enumerated and prune everything else."""

    database, runs, failed = _terminal_source_job(tmp_path)
    blocked = runs / '2024' / '01' / '01'
    blocked.mkdir(parents=True)
    unreadable = blocked.parent
    unreadable.chmod(0o000)
    try:
        assert (
            main(
                [
                    '--database',
                    str(database),
                    'prune',
                    '--runs-directory',
                    str(runs),
                    '--older-than',
                    '30d',
                ]
            )
            == 0
        )
        preview = json.loads(capsys.readouterr().out)
    finally:
        unreadable.chmod(0o755)

    assert str(unreadable) in preview['invalid_paths']
    assert [item['job_id'] for item in preview['selected']] == [str(failed.id)]
