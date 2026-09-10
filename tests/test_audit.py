"""Tests for deterministic read-only job audit documents."""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from agent_orchestra import audit as audit_module
from agent_orchestra import manifests as manifest_module
from agent_orchestra.attempt_documents import AUDIT_ATTEMPT_FIELDS
from agent_orchestra.cli import main
from agent_orchestra.evidence import (
    JobEvidence,
    resolve_evidence_path,
)
from agent_orchestra.invocations import AttemptStatus
from agent_orchestra.issue_sources import IssueLocator, IssueSnapshot, write_snapshot
from agent_orchestra.manifests import evidence_path, parse_manifest
from agent_orchestra.models import IssueJob, ProviderAction, Run, RunState
from agent_orchestra.reviewer_paths import reviewer_evidence_paths
from agent_orchestra.store import JobStore
from agent_orchestra.workflow import transition
from tests.test_job_views import add_attempt


def _arguments(database: Path, root: Path, job_id: str, *, verify: bool) -> list[str]:
    """Return one audit command line."""

    arguments = [
        '--database',
        str(database),
        'audit',
        job_id,
        '--runs-directory',
        str(root),
    ]
    if verify:
        arguments.append('--verify')
    return arguments


@pytest.mark.parametrize(
    ('reviewer_id', 'attempt'),
    [('codex', 1), ('claude-code', 2), ('review-result-000002', 9)],
)
def test_reviewer_writer_paths_are_recognized(reviewer_id: str, attempt: int) -> None:
    """Pin reviewer path writers to audit and manifest recognizers."""

    paths = reviewer_evidence_paths(
        sequence=7, iteration=3, reviewer_id=reviewer_id, attempt=attempt
    )

    assert audit_module.is_known_temporary(paths.temporary_result)
    assert manifest_module.canonical_evidence_type(paths.request) == 'review_request'
    assert manifest_module.canonical_evidence_type(paths.result) == 'review_result'


def _source_job(tmp_path: Path, *, complete: bool = True) -> tuple[Path, Path, Run]:
    """Create a source-code job with one indexed finalized artifact."""

    database = tmp_path / 'state.db'
    root = tmp_path / 'runs'
    worktree = tmp_path / 'worktree'
    worktree.mkdir()
    git = shutil.which('git')
    assert git is not None
    subprocess.run([git, 'init', '-q', str(worktree)], check=True)
    store = JobStore(database)
    store.initialize()
    job = Run.create_local(
        worktree,
        worktree,
        'a' * 40,
        'b' * 40,
        'sha256:' + 'c' * 64,
    )
    store.add(job)
    current = transition(job, RunState.PREPARING)
    store.update(current, RunState.QUEUED)
    if complete:
        for target in (
            RunState.REVIEWING,
            RunState.APPROVED,
            RunState.AWAITING_COMMIT_AUTHORIZATION,
        ):
            updated = transition(current, target)
            store.update(updated, current.state)
            current = updated
    artifact = resolve_evidence_path(root, str(job.id)) / 'failure.json'
    artifact.parent.mkdir(parents=True)
    temporary = artifact.with_suffix('.tmp')
    temporary.write_text('{}\n')
    JobEvidence(root, str(job.id)).finalize_write(temporary, artifact, 'failure')
    return database, root, current


@pytest.mark.parametrize(
    ('column', 'value', 'code'),
    [
        ('scenario', 'future_scenario', 'unknown_transition_scenario'),
        ('from_state', 'future_state', 'unknown_transition_state'),
        ('to_state', 'future_state', 'unknown_transition_state'),
    ],
)
def test_audit_retains_unrecognized_transition_values_as_findings(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    column: str,
    value: str,
    code: str,
) -> None:
    """Keep the rest of an audit readable when one transition is unknown."""

    database, root, job = _source_job(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            f'UPDATE transitions SET {column} = ? WHERE job_id = ?',  # noqa: S608
            (value, str(job.id)),
        )

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'unverifiable'
    assert code in {finding['code'] for finding in document['findings']}
    assert document['transitions'][0][column] == value


def test_audit_reports_missing_worktree_without_mutating_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Add a missing-worktree finding while preserving durable job state."""

    database, root, job = _source_job(tmp_path)
    shutil.rmtree(job.worktree_path)
    before = database.read_bytes()

    assert main(_arguments(database, root, str(job.id), verify=False)) == 0
    document = json.loads(capsys.readouterr().out)
    assert 'worktree_missing' in {item['code'] for item in document['findings']}
    assert JobStore(database).get(job.id).state is job.state
    assert database.read_bytes() == before


def _write_json_evidence(
    root: Path,
    job_id: str,
    relative: str,
    document: dict[str, object],
    evidence_type: str,
) -> None:
    """Write one JSON fixture through the production finalization protocol."""

    path = resolve_evidence_path(root, job_id) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp')
    temporary.write_text(json.dumps(document))
    JobEvidence(root, job_id).finalize_write(
        temporary,
        path,
        evidence_type,  # type: ignore[arg-type]
    )


def _review_request(
    root: Path, job: Run, *, iteration: int, digest: str
) -> dict[str, object]:
    """Build one canonical source-code review request fixture."""

    return {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': None,
        'run_id': str(job.id),
        'sequence': 1,
        'iteration': iteration,
        'message_type': 'review_request',
        'sender': 'orchestrator',
        'recipient': 'reviewer',
        'created_at': '2026-09-07T10:00:00Z',
        'scope': {
            'worktree_path': str(job.worktree_path),
            'base_sha': job.base_sha,
            'head_sha': job.head_sha,
            'diff_digest': digest,
        },
        'payload': {
            'objective': 'Review.',
            'allowed_actions': ['read_worktree', 'write_review_evidence'],
            'timeout_seconds': 60,
            'artifact_path': str(
                resolve_evidence_path(root, str(job.id))
                / f'artifacts/{iteration:06d}-review.md'
            ),
            'prior_review_path': None,
        },
    }


def _issue_job(tmp_path: Path) -> tuple[Path, Path, IssueJob]:
    """Create a completed issue-review job with canonical iteration evidence."""

    database = tmp_path / 'state.db'
    root = tmp_path / 'runs'
    store = JobStore(database)
    store.initialize()
    digest = 'sha256:' + 'd' * 64
    snapshot = IssueSnapshot(
        locator=IssueLocator(
            provider='github',
            host='github.com',
            namespace='acme',
            project='widgets',
            number=12,
            url='https://github.com/acme/widgets/issues/12',
        ),
        title='Feature',
        body='Description',
        author='author',
        labels=('feature',),
        state='open',
        created_at='2026-09-07T09:00:00Z',
        updated_at='2026-09-07T10:00:00Z',
        digest=digest,
    )
    job = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url=snapshot.locator.url,
        namespace='acme',
        project='widgets',
        issue_number=12,
        title=snapshot.title,
        author=snapshot.author,
        source_updated_at=snapshot.updated_at,
        source_digest=digest,
    )
    store.add_issue(job)
    write_snapshot(
        root,
        job.id,
        resolve_evidence_path(root, job.id) / evidence_path('issue_snapshot'),
        snapshot,
    )
    write_snapshot(
        root,
        job.id,
        resolve_evidence_path(root, job.id)
        / evidence_path('issue_snapshot', ordinal=1),
        snapshot,
    )
    request: dict[str, object] = {
        'schema_version': 1,
        'job_id': job.id,
        'iteration': 1,
        'objective': 'Review readiness.',
        'allowed_actions': ['read_issue_snapshot', 'write_review_evidence'],
        'source': snapshot.document(),
        'prior_review': None,
    }
    result: dict[str, object] = {
        'schema_version': 1,
        'source_digest': digest,
        'verdict': 'ready',
        'summary': 'Ready.',
        'findings': [],
        'validation': ['Checked scope.'],
        'verification_gaps': [],
    }
    _write_json_evidence(
        root,
        job.id,
        evidence_path('issue_review_request', ordinal=1),
        request,
        'issue_review_request',
    )
    _write_json_evidence(
        root,
        job.id,
        evidence_path('issue_review_result', ordinal=1),
        result,
        'issue_review_result',
    )
    reviewing = replace(job, state=RunState.REVIEWING, iteration=1)
    store.update_issue(reviewing, RunState.QUEUED)
    completed = replace(reviewing, state=RunState.APPROVED)
    store.update_issue(completed, RunState.REVIEWING)
    store.add_issue_action(
        ProviderAction(
            job_id=job.id,
            iteration=1,
            action='post_feedback',
            provider_id='42',
            remote_url='https://github.com/acme/widgets/issues/12#issuecomment-42',
            created_at=datetime(2026, 9, 7, 11, tzinfo=UTC),
        )
    )
    return database, root, completed


def test_default_audit_is_versioned_deterministic_and_omits_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return history without claiming verification when it was not requested."""

    database, root, job = _source_job(tmp_path)
    arguments = _arguments(database, root, str(job.id), verify=False)

    assert main(arguments) == 0
    first = capsys.readouterr().out
    assert main(arguments) == 0
    second = capsys.readouterr().out

    assert first == second
    document = json.loads(first)
    assert document['schema_version'] == 15
    assert 'result' not in document
    assert document['job']['scenario'] == 'local_changes'
    assert [item['to_state'] for item in document['transitions']] == [
        'queued',
        'preparing',
        'reviewing',
        'approved',
        'awaiting_commit_authorization',
    ]
    assert document['evidence'][0]['status'] == 'not_verified'
    assert [item['kind'] for item in document['operations']] == [
        'commit_authorization_required'
    ]


def test_verify_detects_modified_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report altered bytes as a failed verification with stable context."""

    database, root, job = _source_job(tmp_path)
    (resolve_evidence_path(root, str(job.id)) / 'failure.json').write_text('changed\n')

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    assert document['evidence'][0]['status'] == 'modified'
    assert [item['code'] for item in document['findings']] == ['evidence_modified']


def test_audit_reports_unrecognized_canonical_looking_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Do not silently omit a JSON message absent from the evidence manifest."""

    database, root, job = _source_job(tmp_path)
    unknown = resolve_evidence_path(root, str(job.id)) / 'messages/000003-future.json'
    unknown.parent.mkdir()
    unknown.write_text('{}')

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    assert 'unknown_canonical_evidence' in {
        finding['code'] for finding in document['findings']
    }


def test_audit_rejects_indexed_unrecognized_canonical_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Apply manifest recognition even when an unknown message is indexed."""

    database, root, job = _source_job(tmp_path)
    unknown = resolve_evidence_path(root, str(job.id)) / 'messages/000003-future.json'
    unknown.parent.mkdir()
    unknown.write_text('{}', encoding='utf-8')
    JobEvidence(root, str(job.id)).record_finalized(unknown, 'process_stdout')

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0
    document = json.loads(capsys.readouterr().out)

    assert document['result'] == 'failed'
    assert 'unknown_canonical_evidence' in {
        finding['code'] for finding in document['findings']
    }


def test_audit_rejects_indexed_non_json_in_manifest_namespace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject every indexed file occupying a manifest-owned namespace."""

    database, root, job = _source_job(tmp_path)
    unknown = resolve_evidence_path(root, str(job.id)) / 'messages/000003-future.log'
    unknown.parent.mkdir()
    unknown.write_text('future output\n', encoding='utf-8')
    JobEvidence(root, str(job.id)).record_finalized(unknown, 'process_stdout')

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0
    document = json.loads(capsys.readouterr().out)

    assert document['result'] == 'failed'
    assert 'unknown_canonical_evidence' in {
        finding['code'] for finding in document['findings']
    }


def test_audit_rejects_indexed_canonical_evidence_type_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject an index type that disagrees with a manifest-recognized path."""

    database, root, job = _issue_job(tmp_path)
    request = resolve_evidence_path(root, str(job.id)) / evidence_path(
        'issue_review_request', ordinal=2
    )
    request.parent.mkdir(parents=True, exist_ok=True)
    request.write_text('{}', encoding='utf-8')
    JobEvidence(root, str(job.id)).record_finalized(request, 'process_stdout')

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0
    document = json.loads(capsys.readouterr().out)

    assert document['result'] == 'failed'
    assert 'evidence_type_mismatch' in {
        finding['code'] for finding in document['findings']
    }


def test_verify_complete_current_evidence_is_verified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Verify a native index whose finalized bytes remain unchanged."""

    database, root, job = _source_job(tmp_path)

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'verified'
    assert document['findings'] == []


def test_audit_attempt_publishes_its_declared_key_set(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail when a record field reaches the audit document undeclared."""

    database, root, job = _source_job(tmp_path)
    add_attempt(job, resolve_evidence_path(root, str(job.id)))

    assert main(_arguments(database, root, str(job.id), verify=False)) == 0

    document = json.loads(capsys.readouterr().out)
    attempts = [attempt for task in document['tasks'] for attempt in task['attempts']]
    assert attempts
    for attempt in attempts:
        declared = [field for field in AUDIT_ATTEMPT_FIELDS if field in attempt]
        assert list(attempt) == [*declared, 'streams']


def test_verify_active_attempt_marks_streams_in_progress(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return incomplete without treating live stream files as failures."""

    database, root, job = _source_job(tmp_path, complete=False)
    add_attempt(
        job, resolve_evidence_path(root, str(job.id)), status=AttemptStatus.RUNNING
    )
    invocation = next(
        (resolve_evidence_path(root, str(job.id)) / 'invocations').glob('*.json')
    )
    JobEvidence(root, str(job.id)).record_finalized(invocation, 'invocation_record')
    qualified_temporary = (
        resolve_evidence_path(root, str(job.id))
        / '.000001-reviewer-codex.attempt-0001.review-result.json'
    )
    qualified_temporary.write_text('partial')
    nested_temporary = (
        resolve_evidence_path(root, str(job.id))
        / 'artifacts/.000001-reviewer-codex.attempt-0001.review-result.json'
    )
    nested_temporary.parent.mkdir(exist_ok=True)
    nested_temporary.write_text('stale nested partial')

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    statuses = {item['path']: item['status'] for item in document['evidence']}
    assert list(statuses.values()).count('in_progress') == 3
    assert (
        statuses['artifacts/.000001-reviewer-codex.attempt-0001.review-result.json']
        == 'unindexed'
    )
    serialized = json.dumps(document)
    assert str(root) not in serialized
    assert 'child stdout' not in serialized
    assert 'child stderr' not in serialized


def test_verify_reports_unindexed_partial_and_stale_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Distinguish an interrupted temporary from stale finalized evidence."""

    database, root, job = _source_job(tmp_path)
    job_directory = resolve_evidence_path(root, str(job.id))
    temporary_name = f'.candidate-result-{uuid4()}.json'
    (job_directory / temporary_name).write_text('partial')
    (job_directory / 'stale-result.json').write_text('stale')
    (job_directory / '.final-result.json').write_text('hidden stale')
    (job_directory / 'candidate-approved.json').write_text('named stale')
    invalid_qualified = '.000001-reviewer-Upper.attempt-0001.review-result.json'
    (job_directory / invalid_qualified).write_text('invalid reviewer ID')

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    statuses = {item['path']: item['status'] for item in document['evidence']}
    assert statuses[temporary_name] == 'in_progress'
    assert statuses['stale-result.json'] == 'unindexed'
    assert statuses['.final-result.json'] == 'unindexed'
    assert statuses['candidate-approved.json'] == 'unindexed'
    assert statuses[invalid_qualified] == 'unindexed'
    assert [item['code'] for item in document['findings']].count(
        'unindexed_evidence'
    ) == 4


def test_default_audit_does_not_report_canonical_verification_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep schema and correlation checks additive to --verify."""

    database, root, job = _issue_job(tmp_path)
    result = resolve_evidence_path(root, job.id) / 'iterations/000001/result.json'
    result.write_text('{not-json')

    assert main(_arguments(database, root, job.id, verify=False)) == 0

    document = json.loads(capsys.readouterr().out)
    assert 'result' not in document
    assert document['findings'] == []


def test_verify_binds_source_message_scope_to_selected_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject an internally valid message for another diff digest."""

    database, root, job = _source_job(tmp_path)
    artifact = resolve_evidence_path(root, str(job.id)) / 'artifacts/000001-review.md'
    request: dict[str, object] = {
        'schema_version': 1,
        'message_id': str(uuid4()),
        'in_reply_to': None,
        'run_id': str(job.id),
        'sequence': 1,
        'iteration': 1,
        'message_type': 'review_request',
        'sender': 'orchestrator',
        'recipient': 'reviewer',
        'created_at': '2026-09-07T10:00:00Z',
        'scope': {
            'worktree_path': str(job.worktree_path),
            'base_sha': job.base_sha,
            'head_sha': job.head_sha,
            'diff_digest': 'sha256:' + 'f' * 64,
        },
        'payload': {
            'objective': 'Review.',
            'allowed_actions': ['read_worktree', 'write_review_evidence'],
            'timeout_seconds': 60,
            'artifact_path': str(artifact),
            'prior_review_path': None,
        },
    }
    _write_json_evidence(
        root,
        str(job.id),
        'messages/000001-review-request.json',
        request,
        'review_request',
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO transitions (
                job_id, scenario, from_state, to_state, scope_digest, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(job.id),
                'local_changes',
                'developing',
                'reviewing',
                'sha256:' + 'f' * 64,
                '2026-09-07T10:00:00+00:00',
            ),
        )

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    assert 'scope_digest_mismatch' in {item['code'] for item in document['findings']}


def test_verify_keeps_interrupted_review_retry_in_same_iteration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Do not shift later source iterations when an interrupted review resumes."""

    database, root, job = _source_job(tmp_path)
    later_digest = 'sha256:' + 'e' * 64
    with sqlite3.connect(database) as connection:
        connection.executemany(
            """INSERT INTO transitions (
                job_id, scenario, from_state, to_state, scope_digest, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            [
                (
                    str(job.id),
                    'local_changes',
                    'interrupted',
                    'reviewing',
                    job.diff_digest,
                    '2026-09-07T10:01:00+00:00',
                ),
                (
                    str(job.id),
                    'local_changes',
                    'developing',
                    'reviewing',
                    later_digest,
                    '2026-09-07T10:02:00+00:00',
                ),
            ],
        )
    _write_json_evidence(
        root,
        str(job.id),
        'messages/000001-review-request.json',
        _review_request(root, job, iteration=2, digest=later_digest),
        'review_request',
    )

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert 'scope_digest_mismatch' not in {
        item['code'] for item in document['findings']
    }


def test_verify_missing_reviewing_digest_remains_unverifiable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep an unknown reviewing digest distinct from a proven mismatch."""

    database, root, job = _source_job(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE transitions SET scope_digest = NULL WHERE to_state = 'reviewing'"
        )
    _write_json_evidence(
        root,
        str(job.id),
        'messages/000001-review-request.json',
        _review_request(root, job, iteration=1, digest=job.diff_digest or ''),
        'review_request',
    )

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    codes = {item['code'] for item in document['findings']}
    assert document['result'] == 'unverifiable'
    assert 'transition_digest_missing' in codes
    assert 'scope_digest_mismatch' not in codes


def test_verify_binds_issue_iteration_and_source_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject an issue request whose ordinal and snapshot digest differ."""

    database, root, job = _issue_job(tmp_path)
    request_path = (
        resolve_evidence_path(root, job.id) / 'iterations/000001/request.json'
    )
    request = json.loads(request_path.read_text())
    request['iteration'] = 2
    request['source']['source_digest'] = 'sha256:' + 'e' * 64
    _write_json_evidence(
        root,
        job.id,
        'iterations/000001/request.json',
        request,
        'issue_review_request',
    )

    assert main(_arguments(database, root, job.id, verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    codes = {item['code'] for item in document['findings']}
    assert {'iteration_mismatch', 'source_digest_mismatch'} <= codes


def test_verify_binds_root_issue_snapshot_to_first_iteration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Reject a root snapshot replaced by another valid revision."""

    database, root, job = _issue_job(tmp_path)
    root_snapshot_path = resolve_evidence_path(root, job.id) / 'issue.json'
    root_snapshot = json.loads(root_snapshot_path.read_text())
    root_snapshot['source_digest'] = 'sha256:' + 'e' * 64
    _write_json_evidence(
        root,
        job.id,
        'issue.json',
        root_snapshot,
        'issue_snapshot',
    )

    assert main(_arguments(database, root, job.id, verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    assert any(
        item['code'] == 'source_digest_mismatch' and item['path'] == 'issue.json'
        for item in document['findings']
    )


def test_verify_aggregates_duplicate_and_missing_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report independent integrity failures together."""

    database, root, job = _source_job(tmp_path)
    index_path = resolve_evidence_path(root, str(job.id)) / '.integrity.json'
    index = json.loads(index_path.read_text())
    index['entries'].append(dict(index['entries'][0]))
    index_path.write_text(json.dumps(index))
    (resolve_evidence_path(root, str(job.id)) / 'failure.json').unlink()

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    assert {item['code'] for item in document['findings']} == {
        'duplicate_evidence_identity',
        'evidence_missing',
    }


def test_verify_rejects_symlinked_evidence_without_following_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report an indexed file replaced by a symlink as an escape."""

    database, root, job = _source_job(tmp_path)
    evidence = resolve_evidence_path(root, str(job.id)) / 'failure.json'
    outside = tmp_path / 'outside.txt'
    outside.write_text('secret')
    evidence.unlink()
    evidence.symlink_to(outside)

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'failed'
    assert document['findings'][0]['code'] == 'evidence_path_escape'
    assert 'secret' not in json.dumps(document)


def test_verify_transition_without_digest_is_unverifiable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Preserve uncertainty for a transition without its correlation anchor."""

    database, root, job = _source_job(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            'UPDATE transitions SET scope_digest = NULL WHERE job_id = ?',
            (str(job.id),),
        )

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'unverifiable'
    assert len(document['findings']) == len(document['transitions'])
    assert {item['code'] for item in document['findings']} == {
        'transition_digest_missing'
    }


def test_verify_missing_index_is_unverifiable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Do not guess when a job has no integrity index."""

    database = tmp_path / 'state.db'
    root = tmp_path / 'runs'
    store = JobStore(database)
    store.initialize()
    job = IssueJob.create(
        provider='github',
        host='github.com',
        remote_url='https://github.com/acme/widgets/issues/12',
        namespace='acme',
        project='widgets',
        issue_number=12,
        title='Feature',
        author='author',
        source_updated_at='2026-09-07T10:00:00Z',
        source_digest='sha256:' + 'd' * 64,
    )
    store.add_issue(job)
    ordinary = resolve_evidence_path(root, job.id) / 'pre-index.json'
    ordinary.parent.mkdir(parents=True)
    ordinary.write_text('{"pre_index": true}\n')

    assert main(_arguments(database, root, job.id, verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'unverifiable'
    assert document['job']['scenario'] == 'issue_review'
    assert document['transitions'][0]['scope_digest'] == job.source_digest
    assert document['findings'][0]['code'] == 'integrity_index_missing'
    assert document['evidence'][0]['status'] == 'unverifiable'
    assert all(item['code'] != 'unindexed_evidence' for item in document['findings'])


def test_verify_backfilled_index_does_not_treat_omissions_as_tampering(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep files omitted by a reconciler-built index unverifiable."""

    database, root, job = _source_job(tmp_path)
    index_path = resolve_evidence_path(root, str(job.id)) / '.integrity.json'
    index = json.loads(index_path.read_text())
    index['backfilled_at'] = '2026-09-08T08:00:00Z'
    index_path.write_text(json.dumps(index))
    for ordinal in range(5):
        (
            resolve_evidence_path(root, str(job.id)) / f'pre-index-{ordinal}.log'
        ).write_text('legacy')

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'unverifiable'
    assert 'integrity_index_backfilled' in {
        item['code'] for item in document['findings']
    }
    assert all(item['code'] != 'unindexed_evidence' for item in document['findings'])
    omitted = [item for item in document['evidence'] if item['path'].startswith('pre-')]
    assert len(omitted) == 5
    assert {item['status'] for item in omitted} == {'unverifiable'}


def test_verify_missing_index_still_detects_malformed_canonical_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Aggregate detectable tampering even when the integrity index is absent."""

    database, root, job = _issue_job(tmp_path)
    (resolve_evidence_path(root, job.id) / 'iterations/000001/result.json').write_text(
        '{not-json'
    )
    (resolve_evidence_path(root, job.id) / '.integrity.json').unlink()

    assert main(_arguments(database, root, job.id, verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    codes = {item['code'] for item in document['findings']}
    assert document['result'] == 'failed'
    assert {'integrity_index_missing', 'invalid_canonical_json'} <= codes


def test_verify_completed_issue_job_reports_iterations_and_provider_actions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Audit issue-review history without fetching the provider."""

    database, root, job = _issue_job(tmp_path)

    assert main(_arguments(database, root, job.id, verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'verified'
    assert [item['evidence_type'] for item in document['history']] == [
        'issue_snapshot',
        'issue_snapshot',
        'issue_review_request',
        'issue_review_result',
    ]
    assert document['history'][-1]['verdict'] == 'ready'
    assert document['provider_actions'][0]['provider_id'] == '42'


def test_issue_audit_uses_manifest_template_for_ordinals(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep issue writers, recognition, ordinals, and audit on one path contract."""

    manifest_path = (
        Path(__file__).parents[1] / 'src/agent_orchestra/manifest/evidence.toml'
    )
    custom = parse_manifest(
        'evidence',
        manifest_path.read_text(encoding='utf-8').replace('iterations/', 'history/'),
    )
    packaged_load = manifest_module.load_manifest
    monkeypatch.setattr(
        manifest_module,
        'load_manifest',
        lambda manifest_id: (
            custom if manifest_id == 'evidence' else packaged_load(manifest_id)
        ),
    )
    database, root, job = _issue_job(tmp_path)

    assert (resolve_evidence_path(root, job.id) / 'history/000001/issue.json').is_file()
    assert main(_arguments(database, root, job.id, verify=True)) == 0

    document = json.loads(capsys.readouterr().out)
    assert document['result'] == 'verified'
    assert {item['iteration'] for item in document['history']} == {None, 1}


def test_audit_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Leave database and external evidence byte-for-byte unchanged."""

    database, root, job = _source_job(tmp_path)
    before_database = database.read_bytes()
    before_files = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob('*')
        if path.is_file()
    }

    assert main(_arguments(database, root, str(job.id), verify=True)) == 0
    capsys.readouterr()

    assert database.read_bytes() == before_database
    assert {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob('*')
        if path.is_file()
    } == before_files


def test_audit_reports_missing_job_as_versioned_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Return a stable public error for an unknown job identifier."""

    database = tmp_path / 'state.db'
    JobStore(database).initialize()

    assert main(_arguments(database, tmp_path / 'runs', 'missing', verify=True)) == 2

    document = json.loads(capsys.readouterr().out)
    assert document['schema_version'] == 18
    assert document['job_id'] == 'missing'
    assert document['error']['code'] == 'job_not_found'
