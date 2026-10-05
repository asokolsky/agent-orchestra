"""Pin every documented public JSON document to a CLI-produced golden file."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Self

import pytest

from agent_orchestra import cli, models, reviewer_batch_run, worker, workflow
from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.evidence import (
    IntegrityEntry,
    resolve_evidence_path,
    write_json_atomic,
)
from agent_orchestra.invocations import usage_document
from agent_orchestra.models import IssueJob, ProviderAction, Run, RunState
from agent_orchestra.public_documents import (
    batch_document,
    evidence_document,
    history_document,
    review_result_document,
)
from agent_orchestra.retention import PruneItem, PrunePlan, plan_document
from agent_orchestra.schemas import (
    DeveloperHandoffMessageSchema,
    RemediationRequestMessageSchema,
    ReviewerBatchResultSchemaV3,
)
from agent_orchestra.store import JobStore
from agent_orchestra.usage import ModelUsage, RuntimeUsage, UsageValues
from tests.test_job_views import add_attempt, create_reviewed_batch_job

GOLDENS = Path(__file__).parent / 'data' / 'public_json'
SOURCE_ID = '20261001T000000Z-aaaaaaaa'
ISSUE_ID = '20261001T000000Z-bbbbbbbb'
NOW = datetime(2026, 10, 1, tzinfo=UTC)


class FixtureDatetime(datetime):
    """Keep worker message timestamps inside the fixture's statistics window."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> Self:
        """
        Return the fixture instant in the requested timezone.

        Return a naive UTC value when the caller supplies no timezone.
        """

        instant = cls(2026, 10, 1, tzinfo=UTC)
        return (
            instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)
        )


# docs/cli.md: enqueue-locals, review-issue, post-issue-feedback, prune,
# jobs, cancel, job, tasks, task, audit, stats, run, resume, and config.
# init, enqueue-local, enqueue-issue, and skills intentionally emit text.
PUBLIC_DOCUMENTS = (
    'enqueue-locals',
    'review-issue',
    'post-issue-feedback',
    'prune',
    'jobs',
    'cancel',
    'job-source',
    'job-issue',
    'tasks',
    'task',
    'audit-source',
    'audit-issue',
    'audit-verify',
    'audit-error',
    'stats',
    'run',
    'run-error',
    'resume',
    'resume-error',
    'config',
)


def test_golden_inventory_covers_cli_reference() -> None:
    """Force a golden-file decision when the CLI reference gains a command."""

    reference = (Path(__file__).parent.parent / 'docs' / 'cli.md').read_text(
        encoding='utf-8'
    )
    headings = set(re.findall(r'^#{2,3} `([^`]+)`$', reference, flags=re.MULTILINE))
    text_commands = {
        'init',
        'enqueue-local',
        'enqueue-issue',
        'skills',
        'skills install',
    }
    documented_json = headings - text_commands
    variants = {
        'job-source',
        'job-issue',
        'audit-source',
        'audit-issue',
        'audit-verify',
        'audit-error',
        'run-error',
        'resume-error',
    }
    golden_commands = {
        name.split('-', 1)[0] if name in variants else name for name in PUBLIC_DOCUMENTS
    }

    assert documented_json == golden_commands


def _stored_jobs(root: Path) -> tuple[JobStore, Run, IssueJob]:
    """Persist stable source and issue records for document fixtures."""

    database = root / 'state.db'
    store = JobStore(database)
    store.initialize()
    worktree = root / 'worktree'
    worktree.mkdir()
    source = replace(
        Run.create_local(worktree, worktree, 'base', 'head', 'sha256:fixture'),
        id=SOURCE_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    issue = replace(
        IssueJob.create(
            provider='github',
            host='github.com',
            remote_url='https://github.com/example/repo/issues/1',
            namespace='example',
            project='repo',
            issue_number=1,
            title='Example issue',
            author='author',
            source_updated_at='2026-10-01T00:00:00Z',
            source_digest='sha256:issue',
        ),
        id=ISSUE_ID,
        created_at=NOW,
        updated_at=NOW,
    )
    store.add(source)
    store.add_issue(issue)
    return store, source, issue


def _normalise(value: object, root: Path) -> object:
    """Replace only fixture-dependent paths while retaining public values."""

    if isinstance(value, dict):
        evidence_entry = 'evidence_type' in value and 'path' in value
        return {
            key: '<int>'
            if evidence_entry and key == 'size' and isinstance(item, int)
            else '<sha256>'
            if evidence_entry and key == 'sha256' and isinstance(item, str)
            else '<timestamp>'
            if evidence_entry and key == 'finalized_at' and isinstance(item, str)
            else _normalise(item, root)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalise(item, root) for item in value]
    if isinstance(value, str):
        if re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
            return '<uuid>'
        return value.replace(str(root), '/ROOT')
    return value


def _arguments(root: Path, command: str, *extra: str) -> list[str]:
    """Keep all CLI state and evidence inside the test directory."""

    return [
        '--database',
        str(root / 'state.db'),
        command,
        *extra,
    ]


def _assert_golden(name: str, document: object, root: Path) -> None:
    """
    Compare a public document with its committed fixture.

    Return nothing; assertion failures show any changed field or public value.
    """

    assert _normalise(document, root) == json.loads(
        (GOLDENS / f'{name}.json').read_text(encoding='utf-8')
    )


def _append_developer_handoff(job: Run, runs: Path) -> None:
    """
    Persist a correlated remediation request and populated developer handoff.

    Return nothing. Validate both fixture messages with the canonical schemas
    and index them through the normal atomic evidence writer.
    """

    directory = resolve_evidence_path(runs, str(job.id))
    batch_path = directory / 'review-batches' / '000001.json'
    batch = json.loads(batch_path.read_text(encoding='utf-8'))
    identity: dict[str, object] = {
        'schema_version': 1,
        'run_id': str(job.id),
        'iteration': 1,
        'created_at': '2026-10-01T00:00:00Z',
        'scope': {
            'worktree_path': str(job.worktree_path),
            'base_sha': job.base_sha,
            'head_sha': job.head_sha,
            'diff_digest': job.diff_digest,
        },
    }
    request: dict[str, object] = {
        **identity,
        'message_id': '00000000-0000-4000-8000-000000000003',
        'in_reply_to': batch['message_id'],
        'sequence': 3,
        'message_type': 'remediation_request',
        'sender': 'orchestrator',
        'recipient': 'developer',
        'payload': {
            'objective': 'Address the review findings.',
            'allowed_actions': [],
            'timeout_seconds': 30,
            'review_result_path': str(batch_path),
            'review_artifact_path': str(directory / batch['artifact_path']),
        },
    }
    handoff: dict[str, object] = {
        **identity,
        'message_id': '00000000-0000-4000-8000-000000000004',
        'in_reply_to': request['message_id'],
        'sequence': 4,
        'message_type': 'developer_handoff',
        'sender': 'developer',
        'recipient': 'orchestrator',
        'payload': {
            'status': 'ready_for_review',
            'summary': 'Addressed the fixture findings.',
            'files_changed': ['src/example.py'],
            'validation': [{'command': 'mise run tests', 'outcome': 'passed'}],
            'dispositions': [
                {
                    'finding_id': finding['finding_id'],
                    'disposition': 'addressed',
                    'rationale': 'Added the required regression coverage.',
                }
                for finding in batch['findings']
            ],
            'remaining_risks': [],
        },
    }
    RemediationRequestMessageSchema.model_validate(request)
    DeveloperHandoffMessageSchema.model_validate(handoff)
    write_json_atomic(
        directory / 'messages' / '000003-remediation-request.json',
        request,
        'remediation_request',
    )
    write_json_atomic(
        directory / 'messages' / '000004-developer-handoff.json',
        handoff,
        'developer_handoff',
    )


@pytest.mark.parametrize('name', PUBLIC_DOCUMENTS)
def test_public_json_matches_golden(
    name: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail on a changed field, value, or nesting in any documented document."""

    monkeypatch.setattr(cli, '_distribution_version', lambda: '0.1.0')
    monkeypatch.setattr(cli, 'utc_now', lambda: NOW)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    _store, source, issue = _stored_jobs(tmp_path)
    runs = tmp_path / 'runs'
    job_directory = resolve_evidence_path(runs, SOURCE_ID)
    job_directory.mkdir(parents=True)
    resolve_evidence_path(runs, ISSUE_ID).mkdir(parents=True)
    task_id = add_attempt(
        source, job_directory, role=RuntimeRole.DEVELOPER, reviewer_id=None
    )
    common = ['--runs-directory', str(runs)]

    if name == 'enqueue-locals':
        directory = tmp_path / 'empty'
        directory.mkdir()
        argv = _arguments(tmp_path, 'enqueue-locals', str(directory))
    elif name == 'review-issue':
        monkeypatch.setattr(cli, 'run_issue_review', lambda *_args, **_kwargs: issue)
        argv = _arguments(tmp_path, 'review-issue', ISSUE_ID, *common)
    elif name == 'post-issue-feedback':
        action = ProviderAction(
            job_id=ISSUE_ID,
            iteration=1,
            action='posted',
            provider_id='note-1',
            remote_url='https://github.com/example/repo/issues/1#issuecomment-1',
            created_at=NOW,
        )
        monkeypatch.setattr(
            cli, 'publish_issue_feedback', lambda *_args, **_kwargs: action
        )
        argv = _arguments(
            tmp_path, 'post-issue-feedback', ISSUE_ID, '--authorize', *common
        )
    elif name == 'prune':
        argv = _arguments(tmp_path, 'prune', *common)
    elif name == 'jobs':
        argv = _arguments(tmp_path, 'jobs')
    elif name == 'cancel':
        argv = _arguments(tmp_path, 'cancel', SOURCE_ID, '--reason', 'fixture')
    elif name == 'job-source':
        argv = _arguments(tmp_path, 'job', SOURCE_ID, *common)
    elif name == 'job-issue':
        argv = _arguments(tmp_path, 'job', ISSUE_ID, *common)
    elif name == 'tasks':
        argv = _arguments(tmp_path, 'tasks', SOURCE_ID, *common)
    elif name == 'task':
        argv = _arguments(tmp_path, 'task', task_id, *common)
    elif name == 'audit-source':
        argv = _arguments(tmp_path, 'audit', SOURCE_ID, *common)
    elif name == 'audit-issue':
        argv = _arguments(tmp_path, 'audit', ISSUE_ID, *common)
    elif name == 'audit-verify':
        argv = _arguments(tmp_path, 'audit', SOURCE_ID, '--verify', *common)
    elif name == 'audit-error':
        argv = _arguments(tmp_path, 'audit', 'missing', *common)
    elif name == 'stats':
        argv = _arguments(tmp_path, 'stats', '--since', '7d', *common)
    elif name == 'run':
        monkeypatch.setattr(
            cli,
            'run_queued_review',
            lambda **_kwargs: replace(source, state=RunState.APPROVED),
        )
        argv = _arguments(
            tmp_path, 'run', SOURCE_ID, '--objective', 'Review the fixture', *common
        )
    elif name == 'run-error':
        argv = _arguments(
            tmp_path, 'run', 'missing', '--objective', 'Review the fixture', *common
        )
    elif name == 'resume':
        monkeypatch.setattr(
            cli,
            'resume_review',
            lambda **_kwargs: replace(source, state=RunState.APPROVED),
        )
        argv = _arguments(tmp_path, 'resume', SOURCE_ID, *common)
    elif name == 'resume-error':
        argv = _arguments(tmp_path, 'resume', 'missing', *common)
    elif name == 'config':
        argv = _arguments(tmp_path, 'config', 'show', *common)
    else:
        pytest.fail(f'unknown golden case: {name}')

    assert cli.main(argv) == (2 if name.endswith('-error') else 0)
    captured = capsys.readouterr()
    assert captured.err == ''
    _assert_golden(name, json.loads(captured.out), tmp_path)


def test_internal_field_does_not_enter_public_job_document(tmp_path: Path) -> None:
    """An internal record can gain data without publishing a new key."""

    _, source, _ = _stored_jobs(tmp_path)

    @dataclass(frozen=True, slots=True)
    class ExtendedRun(Run):
        """Stand in for a future record with one additional internal field."""

        internal_only: str = 'must not be public'

    extended = ExtendedRun(
        id=source.id,
        scenario=source.scenario,
        repo_path=source.repo_path,
        worktree_path=source.worktree_path,
        state=source.state,
        base_sha=source.base_sha,
        head_sha=source.head_sha,
        diff_digest=source.diff_digest,
        iteration=source.iteration,
        remote_url=source.remote_url,
        supersedes_run_id=source.supersedes_run_id,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )

    assert cli._job_summary(extended) == cli._job_summary(source)


@pytest.mark.parametrize('extended', [False, True])
def test_public_usage_values_match_golden(extended: bool) -> None:
    """Pin the nested attempt usage shape when runtimes report actual values."""

    @dataclass(frozen=True, slots=True)
    class ExtendedUsageValues(UsageValues):
        """Represent future usage bookkeeping that must stay private."""

        internal_only: str = 'must not be public'

    values_type = ExtendedUsageValues if extended else UsageValues
    usage = RuntimeUsage(
        turn_count=2,
        totals=values_type(input_tokens=10, output_tokens=5, total_cost_usd=0.25),
        models=(
            ModelUsage(
                model='example-model',
                values=values_type(
                    input_tokens=8,
                    cache_creation_input_tokens=3,
                    cache_read_input_tokens=2,
                ),
            ),
        ),
    )

    assert usage_document(usage) == json.loads(
        (GOLDENS / 'usage.json').read_text(encoding='utf-8')
    )


@pytest.mark.parametrize('command', ['job', 'tasks', 'task', 'audit', 'stats'])
def test_populated_reviewer_json_matches_golden(
    command: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pin reviewer identities, batch results, findings, history, and runtime counts.

    Return nothing. The real worker persists correlated evidence using a local
    fake adapter; each CLI serializer must match its populated golden document.
    """

    monkeypatch.setattr(cli, '_distribution_version', lambda: '0.1.0')
    monkeypatch.setattr(cli, 'utc_now', lambda: NOW)
    monkeypatch.setattr(models, 'utc_now', lambda: NOW)
    monkeypatch.setattr(models, 'create_job_id', lambda _at: SOURCE_ID)
    monkeypatch.setattr(reviewer_batch_run, 'utc_now', lambda: NOW)
    monkeypatch.setattr(reviewer_batch_run, 'timestamp', lambda: '2026-10-01T00:00:00Z')
    monkeypatch.setattr(reviewer_batch_run, 'datetime', FixtureDatetime)
    monkeypatch.setattr(worker, 'utc_now', lambda: NOW)
    monkeypatch.setattr(worker, 'datetime', FixtureDatetime)
    monkeypatch.setattr(workflow, 'utc_now', lambda: NOW)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    database, job, runs = create_reviewed_batch_job(
        tmp_path, monkeypatch, request_changes=True
    )
    if command == 'audit':
        _append_developer_handoff(job, runs)
    argv = ['--database', str(database), command]
    if command == 'task':
        argv.append(f'{job.id}:000001-reviewer-security')
    elif command != 'stats':
        argv.append(str(job.id))
    argv.extend(['--runs-directory', str(runs)])
    if command == 'audit':
        argv.append('--verify')
    elif command == 'stats':
        monkeypatch.setattr(cli, 'utc_now', lambda: NOW + timedelta(days=1))
        argv.extend(['--since', '30d'])

    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err == ''
    _assert_golden(f'reviewer-{command}', json.loads(captured.out), tmp_path)


def test_populated_prune_json_matches_golden(tmp_path: Path) -> None:
    """
    Pin selected, skipped, orphan, and successful and failed outcome shapes.

    Return nothing; fixture records exercise the serializer without deleting
    evidence or touching a database.
    """

    selected = PruneItem(
        job_id=SOURCE_ID,
        category='job',
        state='failed',
        age_days=100,
        terminal_at='2026-06-23T00:00:00Z',
        evidence_path=str(tmp_path / 'runs' / SOURCE_ID),
        bytes=42,
        database_records={'jobs': 1, 'transitions': 2, 'provider_actions': 0},
        action='expire_evidence',
        reason='eligible',
    )
    skipped = replace(
        selected,
        job_id=ISSUE_ID,
        state='queued',
        age_days=None,
        terminal_at=None,
        evidence_path=str(tmp_path / 'runs' / ISSUE_ID),
        reason='state_not_eligible',
    )
    orphan = replace(
        selected,
        job_id='20260623T000000Z-cccccccc',
        evidence_path=str(tmp_path / 'runs' / '20260623T000000Z-cccccccc'),
        category='orphan',
        state=None,
        database_records={'jobs': 0, 'transitions': 0, 'provider_actions': 0},
        action='delete_orphan',
        reason='orphan_evidence',
    )
    plan = PrunePlan(
        database=tmp_path / 'state.db',
        runs_directory=tmp_path / 'runs',
        older_than_days=90,
        delete_database_records=False,
        selected=(selected,),
        skipped=(skipped,),
        orphans=(orphan,),
        invalid_paths=(str(tmp_path / 'runs' / 'invalid'),),
    )
    document = plan_document(
        plan,
        applied=True,
        outcomes=(
            {'job_id': SOURCE_ID, 'status': 'applied'},
            {'job_id': orphan.job_id, 'status': 'failed', 'error': 'fixture failure'},
        ),
    )
    _assert_golden('prune-populated', document, tmp_path)


def test_populated_enqueue_json_matches_golden(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pin bulk capture results containing successful, clean, and failed repos.

    Return nothing. Capture is stubbed while the CLI builds the public document.
    """

    monkeypatch.setattr(cli, '_distribution_version', lambda: '0.1.0')
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    for name in ('changed', 'clean', 'failed'):
        repo = tmp_path / name
        repo.mkdir()
        (repo / '.git').mkdir()

    def capture(repo: Path, _base: str) -> Run | None:
        """
        Return stable capture state, or raise the fixture's inspection error.

        Clean repos return None; failed repos raise OSError.
        """

        if repo.name == 'failed':
            message = 'fixture inspection failure'
            raise OSError(message)
        if repo.name == 'clean':
            return None
        return replace(
            Run.create_local(repo, repo, 'base', 'head', 'sha256:fixture'),
            id=SOURCE_ID,
            created_at=NOW,
            updated_at=NOW,
        )

    monkeypatch.setattr(cli, '_capture_local_run', capture)
    assert cli.main(_arguments(tmp_path, 'enqueue-locals', str(tmp_path))) == 0
    captured = capsys.readouterr()
    assert captured.err == ''
    _assert_golden('enqueue-locals-populated', json.loads(captured.out), tmp_path)


def test_populated_config_json_matches_golden(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pin configured runtime defaults and reviewer-set member documents.

    Return nothing; config show reads a local fixture configuration.
    """

    monkeypatch.setattr(cli, '_distribution_version', lambda: '0.1.0')
    config_home = tmp_path / 'config'
    config = config_home / 'agent-orchestra' / 'config.toml'
    config.parent.mkdir(parents=True)
    config.write_text(
        '[runtimes.codex]\nmodel = "gpt-6-sol"\neffort = "medium"\n'
        '[runtimes.claude-code]\nmodel = "claude-opus-5-5"\neffort = "high"\n'
        '[reviewer_sets.default]\n'
        'members = [{ id = "security", runtime = "codex", required = true },\n'
        ' { id = "portability", runtime = "claude-code", required = true }]\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_home))
    assert (
        cli.main(
            _arguments(
                tmp_path, 'config', 'show', '--runs-directory', str(tmp_path / 'runs')
            )
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.err == ''
    _assert_golden('config-populated', json.loads(captured.out), tmp_path)


def test_future_batch_fields_remain_private() -> None:
    """
    Keep future persisted batch and member fields out of public batch output.

    Return nothing. A Pydantic subclass simulates a new persisted record field;
    nested dictionary additions simulate evolved finding and member schemas.
    """

    document = json.loads((GOLDENS / 'reviewer-job.json').read_text(encoding='utf-8'))[
        'job'
    ]['review_batches'][0]
    document.pop('job_id')
    document.pop('path')
    document['run_id'] = SOURCE_ID
    document['message_id'] = '00000000-0000-4000-8000-000000000001'
    baseline = ReviewerBatchResultSchemaV3.model_validate(document)

    class ExtendedBatch(ReviewerBatchResultSchemaV3):
        """Represent future persisted batch bookkeeping."""

        internal_only: str = 'must not be public'

    extended = ExtendedBatch.model_validate(baseline.model_dump(mode='json'))
    extended_document = extended.model_dump(mode='json')
    extended_document['reviewers'][0]['internal_member'] = 'private'
    extended_document['findings'][0]['internal_finding'] = 'private'
    assert batch_document(extended_document) == batch_document(
        baseline.model_dump(mode='json')
    )


@pytest.mark.parametrize(
    'status', ['not_verified', 'verified', 'expired', 'in_progress']
)
def test_future_integrity_fields_remain_private(status: str) -> None:
    """
    Keep added integrity-index metadata out of each public evidence status.

    Return nothing; a dataclass subclass simulates a future persisted field.
    """

    @dataclass(frozen=True, slots=True)
    class ExtendedEntry(IntegrityEntry):
        """Represent future private integrity metadata."""

        internal_only: str = 'must not be public'

    entry = IntegrityEntry(
        job_id=SOURCE_ID,
        evidence_type='review_result',
        path='messages/000002-review-result.json',
        size=42,
        sha256='sha256:' + 'a' * 64,
        finalized_at='2026-10-01T00:00:00Z',
    )
    extended = ExtendedEntry(**asdict(entry))
    assert evidence_document(asdict(extended), status=status) == {
        **asdict(entry),
        'status': status,
    }


def test_future_review_result_fields_remain_private() -> None:
    """
    Keep internal payload and finding additions out of per-reviewer output.

    Return nothing; use the populated golden as the representative payload.
    """

    payload = json.loads((GOLDENS / 'reviewer-task.json').read_text(encoding='utf-8'))[
        'task'
    ]['review_result']
    expected = review_result_document(payload)
    payload['internal_only'] = 'private'
    payload['findings'][0]['internal_finding'] = 'private'
    assert review_result_document(payload) == expected


def test_future_history_fields_remain_private() -> None:
    """
    Keep internal disposition and validation additions out of audit history.

    Return nothing; use a populated developer handoff from the audit golden.
    """

    audit = json.loads((GOLDENS / 'reviewer-audit.json').read_text(encoding='utf-8'))
    handoff = next(
        item
        for item in audit['history']
        if item['evidence_type'] == 'developer_handoff'
    )
    expected = deepcopy(handoff)
    document = {
        'iteration': handoff['iteration'],
        'message_id': handoff['message_id'],
        'payload': {
            'dispositions': handoff['dispositions'],
            'validation': handoff['validation'],
        },
    }
    payload = document['payload']
    payload['dispositions'][0]['internal_only'] = 'private'
    payload['validation'][0]['internal_only'] = 'private'
    assert (
        history_document(
            document,
            path=handoff['path'],
            evidence_type='developer_handoff',
            iteration=1,
        )
        == expected
    )
