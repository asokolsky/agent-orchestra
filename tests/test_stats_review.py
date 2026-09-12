"""Tests for review outcome statistics over a rolling window."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent_orchestra import cli as cli_module
from agent_orchestra.evidence import resolve_evidence_path
from agent_orchestra.models import (
    IssueJob,
    JobTransition,
    Run,
    RunState,
    ScenarioType,
)
from agent_orchestra.stats_review import (
    StatsError,
    build_stats_document,
    parse_since,
    read_job_events,
)
from agent_orchestra.store import JobStore, PersistedEnumError, UnreadableJob

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

END = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
START = END - timedelta(days=2)
DIGEST = f'sha256:{"a" * 64}'


def stamp(moment: datetime) -> str:
    """Return one canonical message timestamp."""

    return moment.isoformat().replace('+00:00', 'Z')


def message_id(directory: Path, name: str) -> str:
    """Return one stable canonical message UUID for a test evidence path."""

    return str(uuid5(NAMESPACE_URL, f'{directory.name}/{name}'))


def scope(directory: Path) -> dict[str, str]:
    """Return one canonical immutable diff scope for a test job."""

    return {
        'worktree_path': str(directory),
        'base_sha': 'a' * 40,
        'head_sha': 'b' * 40,
        'diff_digest': DIGEST,
    }


def finding(index: int) -> dict[str, object]:
    """Return one canonical standalone review finding."""

    return {
        'finding_id': f'f{index}',
        'severity': 'medium',
        'title': f'Finding {index}',
        'path': 'src/example.py',
        'line': index + 1,
        'explanation': 'The behavior is incorrect.',
        'acceptance_criterion': 'Correct the behavior.',
    }


def make_job(tmp_path: Path, name: str) -> tuple[Run, Path]:
    """Create one source-code job and its evidence directory."""

    worktree = tmp_path / name
    worktree.mkdir()
    job = Run.create_local(worktree, worktree, 'a' * 40, 'b' * 40, DIGEST)
    directory = resolve_evidence_path(tmp_path / 'runs', str(job.id))
    (directory / 'messages').mkdir(parents=True)
    return job, directory


def write_review(
    directory: Path,
    *,
    sequence: int,
    iteration: int,
    verdict: str,
    at: datetime,
    findings: int | None = None,
    reviewer_id: str | None = None,
) -> None:
    """Write one canonical reviewer result message."""

    stem = f'{sequence:06d}'
    name = (
        f'{stem}-{reviewer_id}-review-result.json'
        if reviewer_id
        else f'{stem}-review-result.json'
    )
    finding_count = 1 if findings is None and verdict == 'changes_requested' else 0
    if findings is not None:
        finding_count = findings
    document: dict[str, Any] = {
        'schema_version': 1,
        'message_id': message_id(directory, name),
        'in_reply_to': 'review-request',
        'run_id': directory.name,
        'sequence': sequence,
        'iteration': iteration,
        'message_type': 'review_result',
        'sender': 'reviewer',
        'recipient': 'orchestrator',
        'created_at': stamp(at),
        'scope': scope(directory),
        # No body reviewer_id: the strict envelope has no such field and
        # forbids extras, so reviewer identity is carried only by the filename.
        # Writing one here would let the reader pass on evidence the worker
        # cannot produce.
        'payload': {
            'verdict': verdict,
            'summary': 'Review completed.',
            'findings': [finding(index) for index in range(finding_count)],
            'validation': [],
            'verification_gaps': [],
            'artifact_path': 'artifacts/review.md',
        },
    }
    (directory / 'messages' / name).write_text(json.dumps(document), encoding='utf-8')


def write_batch(
    directory: Path,
    *,
    iteration: int,
    verdict: str,
    findings: int = 0,
    members: tuple[tuple[int, str], ...] = ((2, 'security'), (2, 'portability')),
    schema_version: int = 3,
) -> None:
    """Write one aggregate reviewer-set decision citing its member results."""

    batches = directory / 'review-batches'
    batches.mkdir(exist_ok=True)
    reviewer_ids = [reviewer_id for _, reviewer_id in members]
    outcomes = {
        'approved': 'approved',
        'changes_requested': 'changes_requested',
        'blocked': 'blocked',
    }
    batch_findings = []
    for index in range(findings):
        reviewer_id = reviewer_ids[index % len(reviewer_ids)]
        source = f'f{index}'
        batch_findings.append(
            finding(index)
            | {
                'finding_id': f'{reviewer_id}:{source}',
                'reviewer_id': reviewer_id,
                'source_finding_id': source,
            }
        )
    document: dict[str, Any] = {
        'schema_version': schema_version,
        'run_id': directory.name,
        'iteration': iteration,
        'reviewer_set_id': 'default',
        'aggregation_policy': 'all_required',
        'diff_digest': DIGEST,
        'verdict': verdict,
        'reviewers': [
            {
                'reviewer_id': reviewer_id,
                'outcome': outcomes[verdict],
                'result_path': (
                    f'messages/{sequence:06d}-{reviewer_id}-review-result.json'
                ),
            }
            for sequence, reviewer_id in members
        ],
        'changes_requested_by': (
            reviewer_ids if verdict == 'changes_requested' else []
        ),
        'blocked_by': reviewer_ids if verdict == 'blocked' else [],
        'incomplete_reviewers': [],
    }
    if schema_version >= 2:
        document['findings'] = batch_findings
    if schema_version >= 3:
        document['message_id'] = message_id(directory, f'batch-{iteration}')
        document['artifact_path'] = f'artifacts/review-batch-{iteration:04d}.md'
    (batches / f'{iteration:06d}.json').write_text(
        json.dumps(document), encoding='utf-8'
    )


def write_handoff(
    directory: Path, *, sequence: int, dispositions: list[str], at: datetime
) -> None:
    """Write one developer handoff carrying finding dispositions."""

    name = f'{sequence:06d}-developer-handoff.json'
    document = {
        'schema_version': 1,
        'message_id': message_id(directory, name),
        'in_reply_to': 'remediation-request',
        'run_id': directory.name,
        'sequence': sequence,
        'iteration': 1,
        'message_type': 'developer_handoff',
        'sender': 'developer',
        'recipient': 'orchestrator',
        'created_at': stamp(at),
        'scope': scope(directory),
        'payload': {
            'status': 'ready_for_review',
            'summary': 'Reviewer findings were addressed.',
            'files_changed': ['src/example.py'],
            'validation': [],
            'dispositions': [
                {'finding_id': f'f{index}', 'disposition': value, 'rationale': 'r'}
                for index, value in enumerate(dispositions)
            ],
            'remaining_risks': [],
        },
    }
    (directory / 'messages' / name).write_text(json.dumps(document), encoding='utf-8')


def report(
    tmp_path: Path, runs: Sequence[Run | UnreadableJob], **kwargs: Any
) -> dict[str, Any]:
    """Build one statistics document over the fixed window."""

    return build_stats_document(
        runs,
        evidence_root=tmp_path / 'runs',
        start=START,
        end=END,
        since='2d',
        transitions=kwargs.pop('transitions', {}),
        **kwargs,
    )


@pytest.mark.parametrize(
    'value',
    ['0d', '0h', '0w', '0m', '-1d', '1.5d', '3s', '3y', 'd', '', '2days', '2 d'],
)
def test_parse_since_rejects_unusable_windows(value: str) -> None:
    """Reject every window that is not a positive whole count of a known unit."""

    with pytest.raises(StatsError) as caught:
        parse_since(value)
    assert caught.value.code == 'invalid_since'


@pytest.mark.parametrize(
    ('value', 'expected'),
    [
        ('9h', timedelta(hours=9)),
        ('2d', timedelta(days=2)),
        ('14d', timedelta(days=14)),
        ('1w', timedelta(weeks=1)),
        ('2m', timedelta(days=60)),
    ],
)
def test_parse_since_accepts_every_supported_unit(
    value: str, expected: timedelta
) -> None:
    """Accept hours, days, weeks, and 30-day months as whole counts."""

    assert parse_since(value) == expected


def test_window_is_half_open_on_utc_instants(tmp_path: Path) -> None:
    """Count the inclusive start and exclude the exclusive end."""

    moments = {
        'before': START - timedelta(seconds=1),
        'at_start': START,
        'before_end': END - timedelta(seconds=1),
        'at_end': END,
    }
    runs = []
    for index, (name, moment) in enumerate(moments.items(), start=1):
        job, directory = make_job(tmp_path, name)
        write_review(
            directory, sequence=2 * index, iteration=1, verdict='approved', at=moment
        )
        runs.append(job)
    document = report(tmp_path, runs)
    assert document['reviews']['approved'] == 2
    assert document['jobs_total'] == 2


def test_counts_every_round_but_classifies_a_job_by_its_latest(
    tmp_path: Path,
) -> None:
    """Count verdict events per round and the job by its most recent verdict."""

    job, directory = make_job(tmp_path, 'multi')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='changes_requested',
        at=START + timedelta(hours=1),
        findings=3,
    )
    write_review(
        directory,
        sequence=6,
        iteration=2,
        verdict='approved',
        at=START + timedelta(hours=5),
    )
    document = report(tmp_path, [job])
    assert document['reviews'] == {
        'approved': 1,
        'changes_requested': 1,
        'blocked': 0,
    }
    assert document['jobs'] == {'approved': 1, 'changes_requested': 0, 'blocked': 0}
    assert document['jobs_total'] == 1
    assert document['findings']['raised'] == 3


def test_reviewer_set_members_do_not_inflate_reviews(tmp_path: Path) -> None:
    """Count one aggregate decision for a reviewer set, not one per member."""

    job, directory = make_job(tmp_path, 'batch')
    at = START + timedelta(hours=2)
    for reviewer_id in ('security', 'portability'):
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='changes_requested',
            at=at,
            reviewer_id=reviewer_id,
        )
    write_batch(directory, iteration=1, verdict='changes_requested', findings=4)
    document = report(tmp_path, [job])
    assert document['reviews']['changes_requested'] == 1
    assert document['jobs'] == {'approved': 0, 'changes_requested': 1, 'blocked': 0}
    assert document['findings']['raised'] == 4


def test_blocked_is_its_own_outcome(tmp_path: Path) -> None:
    """Report a blocked verdict without folding it into another bucket."""

    job, directory = make_job(tmp_path, 'blocked')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='blocked',
        at=START + timedelta(hours=3),
    )
    document = report(tmp_path, [job])
    assert document['reviews'] == {
        'approved': 0,
        'changes_requested': 0,
        'blocked': 1,
    }
    assert document['jobs']['blocked'] == 1


def test_a_job_without_review_never_enters_the_report(tmp_path: Path) -> None:
    """Leave a job created inside the window but never reviewed uncounted."""

    reviewed, directory = make_job(tmp_path, 'reviewed')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=4),
    )
    quiet, _ = make_job(tmp_path, 'quiet')
    document = report(tmp_path, [reviewed, quiet])
    assert document['jobs_total'] == 1
    assert document['unavailable']['count'] == 0


def test_a_disposition_outside_the_window_is_not_counted(tmp_path: Path) -> None:
    """Count a review inside the window without its later disposition."""

    job, directory = make_job(tmp_path, 'late-disposition')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='changes_requested',
        at=START + timedelta(hours=1),
        findings=2,
    )
    write_handoff(directory, sequence=4, dispositions=['addressed', 'rejected'], at=END)
    document = report(tmp_path, [job])
    assert document['findings'] == {
        'raised': 2,
        'addressed': 0,
        'rejected': 0,
        'blocked': 0,
    }


def test_dispositions_inside_the_window_are_counted(tmp_path: Path) -> None:
    """Count each developer disposition recorded inside the window."""

    job, directory = make_job(tmp_path, 'dispositions')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='changes_requested',
        at=START + timedelta(hours=1),
        findings=3,
    )
    write_handoff(
        directory,
        sequence=4,
        dispositions=['addressed', 'addressed', 'blocked'],
        at=START + timedelta(hours=2),
    )
    document = report(tmp_path, [job])
    assert document['findings'] == {
        'raised': 3,
        'addressed': 2,
        'rejected': 0,
        'blocked': 1,
    }


def test_unreadable_evidence_is_reported_not_dropped(tmp_path: Path) -> None:
    """List a reviewed job whose evidence yields no history as unavailable."""

    job, directory = make_job(tmp_path, 'unreadable')
    (directory / 'messages' / '000002-review-result.json').write_text(
        'not json', encoding='utf-8'
    )
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.APPROVED,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=6),
            ),
        )
    }
    document = report(tmp_path, [job], transitions=transitions)
    assert document['unavailable']['count'] == 1
    assert document['unavailable']['job_ids'] == [str(job.id)]
    assert document['unavailable']['reasons'] == {'invalid_evidence': 1}
    assert (
        sum(document['jobs'].values()) + document['unavailable']['count']
        == (document['jobs_total'])
    )


def test_jobs_and_unavailable_reconcile_against_jobs_total(tmp_path: Path) -> None:
    """Keep the job buckets and unavailable count summing to the job total."""

    approved, approved_directory = make_job(tmp_path, 'ok')
    write_review(
        approved_directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=1),
    )
    broken, broken_directory = make_job(tmp_path, 'broken')
    (broken_directory / 'messages' / '000002-review-result.json').write_text(
        '{', encoding='utf-8'
    )
    quiet, _ = make_job(tmp_path, 'idle')
    transitions = {
        str(broken.id): (
            JobTransition(
                job_id=str(broken.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.CHANGES_REQUESTED,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=2),
            ),
        )
    }
    document = report(tmp_path, [approved, broken, quiet], transitions=transitions)
    assert document['jobs_total'] == 2
    assert (
        sum(document['jobs'].values()) + document['unavailable']['count']
        == (document['jobs_total'])
    )


def test_window_is_reported_as_resolved_utc_instants(tmp_path: Path) -> None:
    """Report the normalized window the report was computed over."""

    document = report(tmp_path, [])
    assert document['window'] == {
        'since': '2d',
        'start': '2026-09-10T12:00:00Z',
        'end': '2026-09-12T12:00:00Z',
        'timezone': 'UTC',
    }


def test_cli_freezes_the_clock_and_excludes_issue_jobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve the window from one clock read and report source-code jobs only."""

    database = tmp_path / 'state.db'
    store = JobStore(database)
    store.initialize()

    job, directory = make_job(tmp_path, 'source')
    store.add(job)
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=END - timedelta(hours=1),
    )
    store.add_issue(
        IssueJob.create(
            provider='github',
            host='github.com',
            remote_url='https://github.com/acme/widgets/issues/16',
            namespace='acme',
            project='widgets',
            issue_number=16,
            title='Readiness',
            author='octocat',
            source_updated_at='2026-01-01T00:00:00Z',
            source_digest='digest',
        )
    )

    monkeypatch.setattr(cli_module, 'utc_now', lambda: END)
    assert (
        cli_module.main(
            [
                '--database',
                str(database),
                'stats',
                '--since',
                '2d',
                '--runs-directory',
                str(tmp_path / 'runs'),
            ]
        )
        == 0
    )
    document = json.loads(capsys.readouterr().out)
    assert document['error'] is None
    assert document['window']['start'] == '2026-09-10T12:00:00Z'
    assert document['window']['end'] == '2026-09-12T12:00:00Z'
    assert document['jobs_total'] == 1
    assert document['reviews']['approved'] == 1


def test_a_naive_timestamp_costs_only_its_own_job(tmp_path: Path) -> None:
    """Degrade an offset-free timestamp instead of aborting the whole report."""

    good, good_directory = make_job(tmp_path, 'aware')
    write_review(
        good_directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=1),
    )
    bad, bad_directory = make_job(tmp_path, 'naive')
    document = json.loads(
        (good_directory / 'messages' / '000002-review-result.json').read_text()
    )
    document['run_id'] = bad_directory.name
    document['created_at'] = '2026-09-11T12:00:00'
    (bad_directory / 'messages' / '000002-review-result.json').write_text(
        json.dumps(document), encoding='utf-8'
    )

    result = report(tmp_path, [good, bad])
    assert result['reviews']['approved'] == 1
    assert result['jobs_total'] == 1


def test_a_naive_transition_timestamp_costs_only_its_own_job(tmp_path: Path) -> None:
    """Report an unusable transition timestamp without aborting the report."""

    job, _ = make_job(tmp_path, 'naive-transition')
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.APPROVED,
                scope_digest=DIGEST,
                occurred_at=START.replace(tzinfo=None),
            ),
        )
    }

    result = report(tmp_path, [job], transitions=transitions)

    assert result['unavailable'] == {
        'count': 1,
        'job_ids': [str(job.id)],
        'reasons': {'invalid_evidence': 1},
    }
    assert result['jobs_total'] == 1


def test_a_naive_transition_outside_the_window_does_not_include_the_job(
    tmp_path: Path,
) -> None:
    """Ignore a damaged transition that cannot represent in-window activity."""

    job, _ = make_job(tmp_path, 'old-naive-transition')
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.APPROVED,
                scope_digest=DIGEST,
                occurred_at=(START - timedelta(days=2)).replace(tzinfo=None),
            ),
        )
    }

    result = report(tmp_path, [job], transitions=transitions)

    assert result['unavailable']['count'] == 0
    assert result['jobs_total'] == 0


def test_a_result_from_another_job_is_not_counted(tmp_path: Path) -> None:
    """Refuse a canonical message whose run does not match its directory."""

    job, directory = make_job(tmp_path, 'foreign')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=1),
    )
    path = directory / 'messages' / '000002-review-result.json'
    document = json.loads(path.read_text())
    document['run_id'] = 'someone-elses-job'
    path.write_text(json.dumps(document), encoding='utf-8')

    assert report(tmp_path, [job])['reviews']['approved'] == 0


def test_a_mislabelled_message_is_not_counted(tmp_path: Path) -> None:
    """Refuse a document whose type disagrees with the path that declares it."""

    job, directory = make_job(tmp_path, 'mislabelled')
    write_handoff(
        directory,
        sequence=4,
        dispositions=['addressed'],
        at=START + timedelta(hours=1),
    )
    path = directory / 'messages' / '000004-developer-handoff.json'
    document = json.loads(path.read_text())
    document['message_type'] = 'review_result'
    path.write_text(json.dumps(document), encoding='utf-8')

    assert report(tmp_path, [job])['findings']['addressed'] == 0


def test_an_unsupported_message_schema_is_not_counted(tmp_path: Path) -> None:
    """Refuse a message declaring a schema version the package cannot read."""

    job, directory = make_job(tmp_path, 'unsupported-message-schema')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=1),
    )
    path = directory / 'messages' / '000002-review-result.json'
    document = json.loads(path.read_text())
    document['schema_version'] = 999
    path.write_text(json.dumps(document), encoding='utf-8')

    events = read_job_events(tmp_path / 'runs', str(job.id))
    assert not events.readable
    assert not events.reviews
    assert report(tmp_path, [job])['reviews']['approved'] == 0


def test_an_unsupported_batch_schema_is_not_counted(tmp_path: Path) -> None:
    """Refuse an aggregate declaring a schema version the package cannot read."""

    job, directory = make_job(tmp_path, 'unsupported-batch-schema')
    for reviewer_id in ('security', 'portability'):
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='approved',
            at=START + timedelta(hours=1),
            reviewer_id=reviewer_id,
        )
    write_batch(directory, iteration=1, verdict='approved')
    path = directory / 'review-batches' / '000001.json'
    document = json.loads(path.read_text())
    document['schema_version'] = 999
    path.write_text(json.dumps(document), encoding='utf-8')

    events = read_job_events(tmp_path / 'runs', str(job.id))
    assert not events.readable
    assert not events.aggregates
    assert report(tmp_path, [job])['reviews']['approved'] == 0


def test_an_unusable_aggregate_does_not_expose_its_members(tmp_path: Path) -> None:
    """Keep member results from becoming separate rounds when the batch is bad."""

    job, directory = make_job(tmp_path, 'bad-batch')
    at = START + timedelta(hours=2)
    for reviewer_id in ('security', 'portability'):
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='changes_requested',
            at=at,
            reviewer_id=reviewer_id,
        )
    batches = directory / 'review-batches'
    batches.mkdir()
    (batches / '000001.json').write_text('{ broken', encoding='utf-8')

    result = report(tmp_path, [job])
    assert result['reviews']['changes_requested'] == 0


def test_entering_review_alone_never_enters_the_report(tmp_path: Path) -> None:
    """Require a transition out of review before reporting a job unavailable."""

    job, directory = make_job(tmp_path, 'still-reviewing')
    # Unreadable evidence is what forces the transition rule to decide; with a
    # readable directory the guard returns before ever consulting it.
    (directory / 'messages' / '000002-review-result.json').write_text(
        '{ broken', encoding='utf-8'
    )
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.PREPARING,
                to_state=RunState.REVIEWING,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=1),
            ),
        )
    }
    result = report(tmp_path, [job], transitions=transitions)
    assert result['jobs_total'] == 0
    assert result['unavailable']['count'] == 0


def test_usable_dispositions_keep_a_job_off_the_unavailable_list(
    tmp_path: Path,
) -> None:
    """Let in-window dispositions count even when another document is unusable."""

    job, directory = make_job(tmp_path, 'partial')
    write_handoff(
        directory,
        sequence=4,
        dispositions=['addressed'],
        at=START + timedelta(hours=2),
    )
    (directory / 'messages' / '000002-review-result.json').write_text(
        '{ broken', encoding='utf-8'
    )
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.CHANGES_REQUESTED,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=1),
            ),
        )
    }
    result = report(tmp_path, [job], transitions=transitions)
    assert result['findings']['addressed'] == 1
    assert result['unavailable']['count'] == 0
    # The review evidence is unreadable, so durable state places the job.
    assert result['jobs'] == {'approved': 0, 'changes_requested': 1, 'blocked': 0}
    assert result['jobs_total'] == 1


def test_an_undecodable_job_row_is_reported_not_dropped(tmp_path: Path) -> None:
    """Report a source job whose row cannot be decoded under its stable code."""

    job_id = '20260911T120000Z-deadbeef'
    unreadable = UnreadableJob(
        job_id=job_id,
        created_at=stamp(START),
        error=PersistedEnumError(job_id=job_id, field='state', value='nope'),
    )
    transitions = {
        job_id: (
            JobTransition(
                job_id=job_id,
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.APPROVED,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=1),
            ),
        )
    }
    result = report(tmp_path, [unreadable], transitions=transitions)
    assert result['unavailable'] == {
        'count': 1,
        'job_ids': [job_id],
        'reasons': {'unknown_job_state': 1},
    }
    assert result['jobs_total'] == 1


@pytest.mark.parametrize(
    'value', ['0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00']
)
def test_a_boundary_timestamp_costs_only_its_own_job(
    tmp_path: Path, value: str
) -> None:
    """Degrade a timestamp that overflows on its way to UTC, never the report."""

    good, good_directory = make_job(tmp_path, f'ok{abs(hash(value)) % 97}')
    write_review(
        good_directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=1),
    )
    bad, bad_directory = make_job(tmp_path, f'edge{abs(hash(value)) % 89}')
    document = json.loads(
        (good_directory / 'messages' / '000002-review-result.json').read_text()
    )
    document['run_id'] = bad_directory.name
    document['created_at'] = value
    (bad_directory / 'messages' / '000002-review-result.json').write_text(
        json.dumps(document), encoding='utf-8'
    )

    result = report(tmp_path, [good, bad])
    assert result['reviews']['approved'] == 1


def test_a_body_that_contradicts_its_filename_is_not_counted(
    tmp_path: Path,
) -> None:
    """Refuse a result whose sequence disagrees with the path it was read from."""

    job, directory = make_job(tmp_path, 'mismatched-path')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=1),
    )
    path = directory / 'messages' / '000002-review-result.json'
    document = json.loads(path.read_text())
    document['sequence'] = 4
    path.write_text(json.dumps(document), encoding='utf-8')

    assert report(tmp_path, [job])['reviews']['approved'] == 0


def test_a_duplicated_result_is_counted_once(tmp_path: Path) -> None:
    """Count one review when the same message is copied under another name."""

    job, directory = make_job(tmp_path, 'duplicated')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=1),
    )
    original = directory / 'messages' / '000002-review-result.json'
    (directory / 'messages' / '000004-review-result.json').write_text(
        original.read_text(), encoding='utf-8'
    )

    assert report(tmp_path, [job])['reviews']['approved'] == 1


@pytest.mark.parametrize('members', [1, 2])
def test_members_without_an_aggregate_are_never_reviews(
    tmp_path: Path, members: int
) -> None:
    """Refuse to turn an interrupted reviewer set into one review per member."""

    job, directory = make_job(tmp_path, f'no-aggregate-{members}')
    for reviewer_id in ('security', 'portability')[:members]:
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='changes_requested',
            at=START + timedelta(hours=2),
            findings=2,
            reviewer_id=reviewer_id,
        )

    result = report(tmp_path, [job])
    assert result['reviews']['changes_requested'] == 0
    assert result['findings']['raised'] == 0
    assert result['jobs_total'] == 0


@pytest.mark.parametrize('value', ['999999999999h', '9999999999m'])
def test_an_unconstructable_window_is_an_invalid_argument(value: str) -> None:
    """Reject a duration no timedelta can hold as a stable argument error."""

    with pytest.raises(StatsError) as caught:
        parse_since(value)
    assert caught.value.code == 'invalid_since'


@pytest.mark.parametrize('value', ['999999999999h', '99999999w'])
def test_an_unsubtractable_window_exits_with_the_stable_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], value: str
) -> None:
    """
    Reject a width that overflows the window start rather than crashing.

    A count can be representable as a duration and still leave no datetime when
    taken off the current instant, so the guard belongs at the subtraction too.
    """

    database = tmp_path / 'state.db'
    JobStore(database).initialize()
    assert (
        cli_module.main(
            [
                '--database',
                str(database),
                'stats',
                '--since',
                value,
                '--runs-directory',
                str(tmp_path / 'runs'),
            ]
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_since'


def test_a_schema_one_aggregate_still_counts(tmp_path: Path) -> None:
    """Count a reviewer-set decision written before aggregate findings existed."""

    job, directory = make_job(tmp_path, 'schema-one')
    at = START + timedelta(hours=2)
    for reviewer_id in ('security', 'portability'):
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='approved',
            at=at,
            reviewer_id=reviewer_id,
        )
    write_batch(directory, iteration=1, verdict='approved', schema_version=1)

    result = report(tmp_path, [job])
    assert result['reviews']['approved'] == 1
    assert result['findings']['raised'] == 0


def test_a_blocked_batch_with_an_incomplete_member_still_counts(
    tmp_path: Path,
) -> None:
    """
    Count a blocked aggregate, whose incomplete member has no result path.

    An `incomplete` member is required to carry a null `result_path`, so this
    is the canonical shape of a blocked reviewer set rather than damage.
    Rejecting it would drop the blocked verdicts the report exists to show.
    """

    job, directory = make_job(tmp_path, 'blocked-batch')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=2),
        reviewer_id='security',
    )
    write_batch(
        directory,
        iteration=1,
        verdict='blocked',
        members=((2, 'security'),),
    )
    path = directory / 'review-batches' / '000001.json'
    document = json.loads(path.read_text())
    document['reviewers'].append(
        {'reviewer_id': 'portability', 'outcome': 'incomplete', 'result_path': None}
    )
    document['incomplete_reviewers'] = ['portability']
    path.write_text(json.dumps(document), encoding='utf-8')

    result = report(tmp_path, [job])
    assert result['reviews']['blocked'] == 1
    assert result['jobs']['blocked'] == 1


def test_a_copied_handoff_is_counted_once(tmp_path: Path) -> None:
    """Refuse a handoff republished under a second canonical filename."""

    job, directory = make_job(tmp_path, 'copied-handoff')
    write_handoff(
        directory,
        sequence=4,
        dispositions=['addressed', 'addressed'],
        at=START + timedelta(hours=2),
    )
    original = directory / 'messages' / '000004-developer-handoff.json'
    # The copy claims the sequence its new name declares, so the path agrees and
    # only the repeated message identity can catch it.
    document = json.loads(original.read_text())
    document['sequence'] = 6
    (directory / 'messages' / '000006-developer-handoff.json').write_text(
        json.dumps(document), encoding='utf-8'
    )

    assert report(tmp_path, [job])['findings']['addressed'] == 2


def test_a_handoff_contradicting_its_filename_is_not_counted(tmp_path: Path) -> None:
    """Refuse a handoff whose sequence disagrees with the path it was read from."""

    job, directory = make_job(tmp_path, 'handoff-mismatch')
    write_handoff(
        directory,
        sequence=4,
        dispositions=['addressed'],
        at=START + timedelta(hours=2),
    )
    path = directory / 'messages' / '000004-developer-handoff.json'
    document = json.loads(path.read_text())
    document['sequence'] = 6
    path.write_text(json.dumps(document), encoding='utf-8')

    assert report(tmp_path, [job])['findings']['addressed'] == 0


def test_a_schema_one_batch_counts_findings_from_its_members(
    tmp_path: Path,
) -> None:
    """Sum a schema-1 batch's findings from the member results it cites."""

    job, directory = make_job(tmp_path, 'schema-one-findings')
    at = START + timedelta(hours=2)
    for reviewer_id, findings in (('security', 2), ('portability', 3)):
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='changes_requested',
            at=at,
            findings=findings,
            reviewer_id=reviewer_id,
        )
    write_batch(
        directory,
        iteration=1,
        verdict='changes_requested',
        schema_version=1,
    )

    result = report(tmp_path, [job])
    assert result['reviews']['changes_requested'] == 1
    assert result['findings']['raised'] == 5


def test_a_damaged_schema_two_aggregate_is_not_read_as_zero(
    tmp_path: Path,
) -> None:
    """Refuse a schema-2 aggregate missing the findings array it must declare."""

    job, directory = make_job(tmp_path, 'damaged-v2')
    at = START + timedelta(hours=2)
    for reviewer_id in ('security', 'portability'):
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='changes_requested',
            at=at,
            reviewer_id=reviewer_id,
        )
    write_batch(directory, iteration=1, verdict='changes_requested', findings=2)
    path = directory / 'review-batches' / '000001.json'
    document = json.loads(path.read_text())
    del document['findings']
    path.write_text(json.dumps(document), encoding='utf-8')

    assert report(tmp_path, [job])['reviews']['changes_requested'] == 0


def test_an_all_incomplete_blocked_batch_is_dated_from_its_transition(
    tmp_path: Path,
) -> None:
    """
    Count a blocked batch that no member result can place in time.

    Schema 1 allows every member to be incomplete, leaving no member result to
    date the decision. The verdict is real, so the transition out of review
    places it rather than the batch being dropped.
    """

    job, directory = make_job(tmp_path, 'all-incomplete')
    batches = directory / 'review-batches'
    batches.mkdir()
    (batches / '000001.json').write_text(
        json.dumps(
            {
                'schema_version': 1,
                'run_id': directory.name,
                'iteration': 1,
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
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.FAILED,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=4),
            ),
        )
    }
    result = report(tmp_path, [job], transitions=transitions)
    assert result['reviews']['blocked'] == 1
    assert result['jobs']['blocked'] == 1
    assert result['unavailable']['count'] == 0


def test_an_interrupted_round_does_not_consume_an_iteration(
    tmp_path: Path,
) -> None:
    """
    Place an undated aggregate by the exit that carried a verdict.

    An interrupted round resumes as the same iteration, so counting its exit
    would number the rounds wrongly and date iteration 2 by the interruption
    rather than by the verdict that followed it.
    """

    job, directory = make_job(tmp_path, 'interrupted-round')
    batches = directory / 'review-batches'
    batches.mkdir()
    (batches / '000001.json').write_text(
        json.dumps(
            {
                'schema_version': 1,
                'run_id': directory.name,
                'iteration': 1,
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

    def leaving(to_state: RunState, at: datetime) -> JobTransition:
        """Build one transition out of review."""

        return JobTransition(
            job_id=str(job.id),
            scenario=ScenarioType.LOCAL_CHANGES,
            from_state=RunState.REVIEWING,
            to_state=to_state,
            scope_digest=DIGEST,
            occurred_at=at,
        )

    # The interruption falls before the window and the verdict inside it.
    transitions = {
        str(job.id): (
            leaving(RunState.INTERRUPTED, START - timedelta(hours=1)),
            leaving(RunState.FAILED, START + timedelta(hours=1)),
        )
    }
    result = report(tmp_path, [job], transitions=transitions)
    assert result['reviews']['blocked'] == 1
    assert result['jobs']['blocked'] == 1


def test_an_unusable_review_exit_does_not_renumber_later_rounds(
    tmp_path: Path,
) -> None:
    """Keep later aggregates paired with their durable review-exit ordinal."""

    job, directory = make_job(tmp_path, 'unusable-first-exit')
    write_batch(directory, iteration=1, verdict='blocked', schema_version=1)
    first_batch = directory / 'review-batches' / '000001.json'
    first_document = json.loads(first_batch.read_text())
    for reviewer in first_document['reviewers']:
        reviewer['outcome'] = 'incomplete'
        reviewer['result_path'] = None
    first_document['blocked_by'] = []
    first_document['incomplete_reviewers'] = ['security', 'portability']
    first_batch.write_text(json.dumps(first_document), encoding='utf-8')

    for reviewer_id in ('security', 'portability'):
        write_review(
            directory,
            sequence=6,
            iteration=2,
            verdict='approved',
            at=START - timedelta(days=2),
            reviewer_id=reviewer_id,
        )
    write_batch(
        directory,
        iteration=2,
        verdict='approved',
        members=((6, 'security'), (6, 'portability')),
        schema_version=1,
    )
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.FAILED,
                scope_digest=DIGEST,
                occurred_at=START.replace(tzinfo=None),
            ),
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.APPROVED,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=1),
            ),
        )
    }

    result = report(tmp_path, [job], transitions=transitions)

    assert result['reviews'] == {
        'approved': 1,
        'changes_requested': 0,
        'blocked': 0,
    }
    assert result['jobs'] == {
        'approved': 1,
        'changes_requested': 0,
        'blocked': 0,
    }


def test_an_interruption_alone_is_not_review_activity(tmp_path: Path) -> None:
    """Refuse to report a job whose only exit from review was an interruption."""

    job, directory = make_job(tmp_path, 'only-interrupted')
    (directory / 'messages' / '000002-review-result.json').write_text(
        '{ broken', encoding='utf-8'
    )
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.INTERRUPTED,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=1),
            ),
        )
    }
    result = report(tmp_path, [job], transitions=transitions)
    assert result['jobs_total'] == 0
    assert result['unavailable']['count'] == 0


def test_a_schema_one_batch_with_a_damaged_member_is_unusable(
    tmp_path: Path,
) -> None:
    """Refuse to understate findings when a cited member's array is damaged."""

    job, directory = make_job(tmp_path, 'damaged-member')
    at = START + timedelta(hours=2)
    for reviewer_id in ('security', 'portability'):
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='changes_requested',
            at=at,
            findings=2,
            reviewer_id=reviewer_id,
        )
    path = directory / 'messages' / '000002-security-review-result.json'
    document = json.loads(path.read_text())
    del document['payload']['findings']
    path.write_text(json.dumps(document), encoding='utf-8')
    write_batch(
        directory,
        iteration=1,
        verdict='changes_requested',
        schema_version=1,
    )

    result = report(tmp_path, [job])
    assert result['reviews']['changes_requested'] == 0
    assert result['findings']['raised'] == 0


def test_a_batch_citing_one_result_twice_is_unusable(tmp_path: Path) -> None:
    """
    Refuse an aggregate whose completed members name the same result.

    Two members citing one result is damaged evidence. Deduplicating it would
    count that member's findings once and report the batch as usable with an
    understated total.
    """

    job, directory = make_job(tmp_path, 'duplicate-citation')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='changes_requested',
        at=START + timedelta(hours=2),
        findings=2,
        reviewer_id='security',
    )
    write_batch(
        directory,
        iteration=1,
        verdict='changes_requested',
        schema_version=1,
    )
    path = directory / 'review-batches' / '000001.json'
    document = json.loads(path.read_text())
    document['reviewers'][1]['result_path'] = (
        'messages/000002-security-review-result.json'
    )
    path.write_text(json.dumps(document), encoding='utf-8')

    result = report(tmp_path, [job])
    assert result['reviews']['changes_requested'] == 0
    assert result['findings']['raised'] == 0


@pytest.mark.parametrize('directory', ['messages', 'review-batches'])
def test_a_symlinked_evidence_directory_is_not_followed(
    tmp_path: Path, directory: str
) -> None:
    """
    Refuse an evidence subdirectory that points outside the evidence root.

    Following one would count whatever JSON it addressed as this job's history,
    so a fabricated approval placed anywhere readable would enter the report.
    """

    job, job_directory = make_job(tmp_path, f'symlinked-{directory}')
    outside = tmp_path / f'outside-{directory}'
    outside.mkdir()
    (outside / '000002-review-result.json').write_text(
        json.dumps(
            {
                'schema_version': 1,
                'message_id': 'fabricated',
                'run_id': job_directory.name,
                'sequence': 2,
                'iteration': 1,
                'message_type': 'review_result',
                'created_at': stamp(START + timedelta(hours=1)),
                'payload': {'verdict': 'approved', 'findings': []},
            }
        ),
        encoding='utf-8',
    )
    target = job_directory / directory
    if target.exists():
        target.rmdir()
    target.symlink_to(outside, target_is_directory=True)

    result = report(tmp_path, [job])
    assert result['reviews']['approved'] == 0
    assert result['jobs_total'] == 0


@pytest.mark.parametrize('members', [None, []])
def test_an_aggregate_declaring_no_members_is_unusable(
    tmp_path: Path, members: list[object] | None
) -> None:
    """
    Refuse an aggregate with no reviewers rather than counting it as a verdict.

    An empty citation list satisfies every completeness check by vacuity, so a
    malformed document would otherwise be reported as a real blocked review.
    """

    job, directory = make_job(tmp_path, f'no-members-{members is None}')
    write_batch(directory, iteration=1, verdict='blocked', schema_version=1)
    path = directory / 'review-batches' / '000001.json'
    document = json.loads(path.read_text())
    if members is None:
        del document['reviewers']
    else:
        document['reviewers'] = members
    path.write_text(json.dumps(document), encoding='utf-8')
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.FAILED,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=1),
            ),
        )
    }
    result = report(tmp_path, [job], transitions=transitions)
    assert result['reviews']['blocked'] == 0
    assert result['unavailable']['count'] == 1


def test_an_overlong_since_value_is_an_invalid_argument() -> None:
    """Reject a digit string past the interpreter's conversion limit."""

    with pytest.raises(StatsError) as caught:
        parse_since('9' * 5000 + 'd')
    assert caught.value.code == 'invalid_since'


def test_an_aggregate_with_a_malformed_member_is_unusable(tmp_path: Path) -> None:
    """
    Refuse an aggregate mixing a readable member with an unreadable one.

    Skipping the bad entry would leave the remaining citations self-consistent,
    so the batch would be counted as a whole decision when part of it cannot be
    read at all.
    """

    job, directory = make_job(tmp_path, 'malformed-member')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='changes_requested',
        at=START + timedelta(hours=2),
        findings=2,
        reviewer_id='security',
    )
    write_batch(
        directory,
        iteration=1,
        verdict='changes_requested',
        schema_version=1,
    )
    path = directory / 'review-batches' / '000001.json'
    document = json.loads(path.read_text())
    document['reviewers'][1] = {}
    path.write_text(json.dumps(document), encoding='utf-8')

    result = report(tmp_path, [job])
    assert result['reviews']['changes_requested'] == 0
    assert result['findings']['raised'] == 0


def test_an_unsafe_batch_directory_is_reported_unavailable(tmp_path: Path) -> None:
    """
    Report a reviewed job whose batch directory is unsafe, never omit it.

    An absent `review-batches` is ordinary and says nothing about readability.
    A symlinked one is damaged evidence, and calling the job fully read would
    drop it from the report instead of listing it under unavailable.
    """

    job, job_directory = make_job(tmp_path, 'unsafe-batches')
    outside = tmp_path / 'outside-batches'
    outside.mkdir()
    (job_directory / 'review-batches').symlink_to(outside, target_is_directory=True)
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.APPROVED,
                scope_digest=DIGEST,
                occurred_at=START + timedelta(hours=1),
            ),
        )
    }
    result = report(tmp_path, [job], transitions=transitions)
    assert result['unavailable']['count'] == 1
    assert result['jobs_total'] == 1


def test_an_absent_batch_directory_is_not_damage(tmp_path: Path) -> None:
    """Leave a single-reviewer job readable when it has no batch directory."""

    job, directory = make_job(tmp_path, 'no-batches')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='approved',
        at=START + timedelta(hours=1),
    )
    result = report(tmp_path, [job])
    assert result['reviews']['approved'] == 1
    assert result['unavailable']['count'] == 0


def test_a_reviewer_set_round_in_the_workers_own_shape_is_counted(
    tmp_path: Path,
) -> None:
    """
    Count a reviewer-set round written exactly as the worker writes it.

    `ReviewResultMessageSchema` has no `reviewer_id` field and forbids extras,
    so a member result carries its reviewer only in its filename. A reader that
    looked for identity in the body would reject every real reviewer-set round
    while synthetic evidence carrying the extra field passed.
    """

    job, directory = make_job(tmp_path, 'worker-shaped')
    at = START + timedelta(hours=2)
    for reviewer_id in ('security', 'portability'):
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='changes_requested',
            at=at,
            reviewer_id=reviewer_id,
        )
        path = directory / 'messages' / f'000002-{reviewer_id}-review-result.json'
        assert 'reviewer_id' not in json.loads(path.read_text())
    write_batch(directory, iteration=1, verdict='changes_requested', findings=2)

    result = report(tmp_path, [job])
    assert result['reviews']['changes_requested'] == 1
    assert result['jobs'] == {'approved': 0, 'changes_requested': 1, 'blocked': 0}
    assert result['findings']['raised'] == 2


def test_an_invalid_window_is_reported_without_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report an invalid duration whether or not the database exists."""

    assert (
        cli_module.main(
            [
                '--database',
                str(tmp_path / 'missing.db'),
                'stats',
                '--since',
                '0d',
                '--runs-directory',
                str(tmp_path / 'runs'),
            ]
        )
        == 2
    )
    document = json.loads(capsys.readouterr().out)
    assert document['error']['code'] == 'invalid_since'


def test_a_job_remediated_in_the_window_is_classified(tmp_path: Path) -> None:
    """
    Classify a job whose review preceded the window but whose work is inside it.

    Remediation routinely lands in the window after the review it answers.
    Counting those dispositions while leaving the job out of every bucket would
    report findings addressed for a job the document does not admit exists.
    """

    job, directory = make_job(tmp_path, 'cross-window')
    write_review(
        directory,
        sequence=2,
        iteration=1,
        verdict='changes_requested',
        at=START - timedelta(hours=6),
        findings=2,
    )
    write_handoff(
        directory,
        sequence=4,
        dispositions=['addressed', 'addressed'],
        at=START + timedelta(hours=1),
    )

    result = report(tmp_path, [job])
    assert result['reviews'] == {
        'approved': 0,
        'changes_requested': 0,
        'blocked': 0,
    }
    assert result['findings']['addressed'] == 2
    assert result['jobs'] == {'approved': 0, 'changes_requested': 1, 'blocked': 0}
    assert result['jobs_total'] == 1
    assert (
        sum(result['jobs'].values()) + result['unavailable']['count']
        == (result['jobs_total'])
    )


def test_an_aggregate_completing_after_the_window_is_excluded(
    tmp_path: Path,
) -> None:
    """
    Date a reviewer-set verdict by when it completed, not when a member finished.

    A member can return just before the window ends while aggregation completes
    just after it. Dating by the member would count a decision that did not yet
    exist inside the window.
    """

    job, directory = make_job(tmp_path, 'late-aggregate')
    for reviewer_id in ('security', 'portability'):
        write_review(
            directory,
            sequence=2,
            iteration=1,
            verdict='approved',
            at=END - timedelta(minutes=1),
            reviewer_id=reviewer_id,
        )
    write_batch(directory, iteration=1, verdict='approved')
    transitions = {
        str(job.id): (
            JobTransition(
                job_id=str(job.id),
                scenario=ScenarioType.LOCAL_CHANGES,
                from_state=RunState.REVIEWING,
                to_state=RunState.APPROVED,
                scope_digest=DIGEST,
                occurred_at=END + timedelta(minutes=1),
            ),
        )
    }
    result = report(tmp_path, [job], transitions=transitions)
    assert result['reviews']['approved'] == 0
    assert result['jobs_total'] == 0


def test_a_disposition_without_a_readable_standing_is_reported(
    tmp_path: Path,
) -> None:
    """
    Report a job that worked in the window but whose verdict cannot be read.

    Both documents are canonical when written, and only the earlier review
    later becomes unreadable. Counting the disposition while leaving the job
    out of every total would describe work on a job the document does not
    admit exists.
    """

    job, directory = make_job(tmp_path, 'standing-unreadable')
    write_handoff(
        directory,
        sequence=4,
        dispositions=['addressed'],
        at=START + timedelta(hours=2),
    )
    (directory / 'messages' / '000002-review-result.json').write_text(
        '{ broken', encoding='utf-8'
    )

    # No transitions either, so neither evidence nor durable state can place it.
    result = report(tmp_path, [job])
    assert result['findings']['addressed'] == 1
    assert result['unavailable']['count'] == 1
    assert result['jobs_total'] == 1
    assert (
        sum(result['jobs'].values()) + result['unavailable']['count']
        == (result['jobs_total'])
    )
