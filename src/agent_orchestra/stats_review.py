"""
Review outcome statistics over a rolling time window.

The report answers two questions the per-job commands cannot: how much review
happened, and where the jobs now stand. Those are different counts and the
document reports both, because a job reviewed three times is one job and three
reviews.

Every number here is derived from canonical evidence on disk rather than from a
column in the database. That keeps one source of truth, and it is why a pruned
job appears under `unavailable` instead of silently lowering a total: the
report says what it could not read.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_orchestra.errors import AgentOrchestraError
from agent_orchestra.evidence import (
    EvidencePathError,
    WorkerError,
    resolve_evidence_path,
)
from agent_orchestra.manifests import (
    canonical_evidence_type,
    evidence_ordinal,
    evidence_path,
)
from agent_orchestra.messages import reviewer_id_from_message_path
from agent_orchestra.models import Run, RunState
from agent_orchestra.store import UnreadableJob

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from agent_orchestra.models import JobTransition

SINCE_PATTERN = re.compile(r'^(\d+)([hdwm])$')
# A month is a fixed 30 days. A calendar month would make the width of a
# rolling window depend on when it is asked for, so two reports taken days
# apart would cover different spans without saying so.
SINCE_UNITS = {
    'h': timedelta(hours=1),
    'd': timedelta(days=1),
    'w': timedelta(weeks=1),
    'm': timedelta(days=30),
}
INVALID_SINCE_CODE = 'invalid_since'
INVALID_SINCE = (
    'since must be a positive whole number of hours, days, weeks, or months, '
    'such as 9h, 2d, 1w, or 2m'
)

VERDICTS = ('approved', 'changes_requested', 'blocked')
DISPOSITIONS = ('addressed', 'rejected', 'blocked')


class StatsError(AgentOrchestraError):
    """
    Raised when a statistics request cannot be interpreted.

    `stats` succeeds with a versioned JSON document, so its expected failures
    are JSON too. The stable `code` is what a caller branches on; the message
    is for a human and is not part of the contract.
    """

    def __init__(self, message: str, *, code: str) -> None:
        """Record the failure and its stable public code."""

        super().__init__(message)
        self.code = code


def parse_since(value: str) -> timedelta:
    """
    Return the rolling window width for one `--since` argument.

    The window is a duration back from now, not a calendar span: `2d` is the
    preceding 48 hours regardless of midnight, and `2m` is 60 days. Accepting
    only whole counts of a single unit keeps the resolved window reproducible
    from the argument alone.
    """

    match = SINCE_PATTERN.fullmatch(value.strip())
    if match is None:
        raise StatsError(INVALID_SINCE, code=INVALID_SINCE_CODE)
    try:
        # int() itself raises past the interpreter's digit-string limit, so the
        # conversion belongs inside the guard along with the multiplication.
        count = int(match.group(1))
        if count < 1:
            raise StatsError(INVALID_SINCE, code=INVALID_SINCE_CODE)
        return count * SINCE_UNITS[match.group(2)]
    except (OverflowError, ValueError) as error:
        # A width no datetime can express is a bad argument, and the caller is
        # owed the documented JSON error rather than a traceback.
        raise StatsError(INVALID_SINCE, code=INVALID_SINCE_CODE) from error


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    """
    One aggregate review verdict recorded for a source-code job.

    A reviewer set produces one of these per iteration, not one per member:
    the aggregate decision is the review, and the members are its breakdown.
    `findings` is a count rather than the findings themselves, because the
    report says how many were raised and never judges them.
    """

    job_id: str
    iteration: int
    verdict: str
    occurred_at: datetime
    findings: int


@dataclass(frozen=True, slots=True)
class DispositionEvent:
    """
    One developer disposition of a reviewer finding.

    Dispositions are counted where they were recorded, which need not be the
    window that contains the review that raised the finding. A review late in
    a window is routinely addressed in the next one.
    """

    job_id: str
    disposition: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class AggregateReview:
    """
    One reviewer-set verdict, carried out of the reader to be dated.

    The aggregate document has no timestamp of its own. Its member results are
    written before aggregation completes, so dating a decision by them can
    place it in a window it did not yet exist in. The durable transition that
    left review is when the decision actually landed, so the caller prefers it
    and falls back to the newest member result only when no such transition
    survives.
    """

    job_id: str
    iteration: int
    verdict: str
    findings: int
    fallback: datetime | None


@dataclass(frozen=True, slots=True)
class JobEvents:
    """
    Every review-relevant event read from one job's evidence.

    `readable` reports whether every document parsed, not whether anything was
    recovered. The two differ, and the difference matters: a job that yields
    usable events from a partly damaged directory still contributes them, and
    only a job that yields nothing at all is reported as unavailable.
    """

    reviews: tuple[ReviewEvent, ...]
    dispositions: tuple[DispositionEvent, ...]
    aggregates: tuple[AggregateReview, ...] = ()
    readable: bool = field(kw_only=True)


def _parse_timestamp(value: object) -> datetime | None:
    """
    Return one canonical message instant in UTC, or None when unusable.

    Returning None rather than raising is deliberate. A timestamp is read from
    evidence that may be damaged, and one unusable value must cost its own job
    its readability, never abort a report covering every other job.
    """

    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # An offset-free timestamp parses to a naive datetime, and comparing that
    # with the aware window raises rather than degrading. One malformed
    # document must cost its own job's readability, never the whole report.
    if parsed.tzinfo is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except OverflowError, OSError, ValueError:
        # A timestamp near the representable boundary can overflow on its way
        # to UTC. That is still one job's unusable evidence, not a reason to
        # abandon the report.
        return None


def _read_document(path: Path) -> dict[str, Any] | None:
    """Return one JSON object from evidence, or None when unreadable."""

    try:
        document = json.loads(path.read_text(encoding='utf-8'))
    except OSError, ValueError:
        return None
    return document if isinstance(document, dict) else None


def _contained_children(
    evidence_root: Path, job_id: str, directory: str
) -> list[Path] | None:
    """
    Return one evidence subdirectory's entries, or None when it is absent.

    Every path is rebuilt through the evidence containment API rather than
    walked from the job directory. A symlinked `messages` or `review-batches`
    would otherwise be followed out of the evidence root, and whatever JSON it
    pointed at would be counted as this job's history.

    Absent and unsafe are different answers and must not collapse into one. A
    job may legitimately have no `review-batches` at all, but a symlinked one
    is damaged evidence, so this raises for unsafe and returns None for absent.
    """

    resolved = resolve_evidence_path(evidence_root, job_id, directory)
    try:
        names = sorted(entry.name for entry in resolved.iterdir())
    except FileNotFoundError:
        return None
    return [
        resolve_evidence_path(evidence_root, job_id, directory, name) for name in names
    ]


def _message_documents(
    evidence_root: Path, job_id: str
) -> tuple[list[tuple[str, str, dict[str, Any]]], bool]:
    """
    Return every canonical message in one job, with whether all were read.

    A document is accepted only when its own `message_type` matches the type
    its path declares. The path is the authority because it is what the
    evidence manifest names and what the integrity index records; trusting the
    body instead would let a file named as one kind of evidence be counted as
    another.
    """

    try:
        entries = _contained_children(evidence_root, job_id, 'messages')
    except EvidencePathError, OSError, ValueError:
        return [], False
    if entries is None:
        return [], False
    documents: list[tuple[str, str, dict[str, Any]]] = []
    complete = True
    for path in entries:
        relative = f'messages/{path.name}'
        declared = canonical_evidence_type(relative)
        if declared is None:
            continue
        document = _read_document(path)
        if document is None:
            complete = False
            continue
        # The path already declares what the document must be. Trusting the
        # body's own message_type instead would let a file named as one kind
        # of evidence be counted as another.
        if document.get('message_type') != declared:
            complete = False
            continue
        documents.append((declared, relative, document))
    return documents, complete


def _batch_documents(
    evidence_root: Path, job_id: str
) -> tuple[dict[int, dict[str, Any]], bool]:
    """
    Return each iteration's aggregate decision, keyed by iteration.

    An aggregate is accepted only when its `iteration` matches the ordinal in
    its own filename, so a document cannot be attributed to a round it does not
    belong to. Anything else lowers readability and is left out; its members
    cannot stand in for it, because a member is never a review on its own.
    """

    try:
        entries = _contained_children(evidence_root, job_id, 'review-batches')
    except EvidencePathError, OSError, ValueError:
        # An unsafe or unreadable batch directory is damaged evidence, and
        # saying the job was fully read would let it drop out of the report
        # entirely rather than appear under unavailable.
        return {}, False
    if entries is None:
        # Having no batch directory is the ordinary shape of a job that was
        # never reviewed by a set, and says nothing about readability.
        return {}, True
    documents: dict[int, dict[str, Any]] = {}
    complete = True
    for path in entries:
        document = _read_document(path)
        iteration = document.get('iteration') if document is not None else None
        ordinal = evidence_ordinal('review_batch_result', f'review-batches/{path.name}')
        if document is None or not isinstance(iteration, int) or iteration != ordinal:
            complete = False
            continue
        documents[iteration] = document
    return documents, complete


def _iteration_of(document: Mapping[str, Any]) -> int | None:
    """
    Return one message's iteration when it is usable.

    The iteration is what ties a member result to the aggregate that supersedes
    it, so a message without one cannot be placed in a review round.
    """

    iteration = document.get('iteration')
    return iteration if isinstance(iteration, int) else None


def read_job_events(evidence_root: Path, job_id: str) -> JobEvents:
    """
    Read one source-code job's review verdicts and finding dispositions.

    A reviewer set records one aggregate decision per iteration alongside each
    member's own result. The aggregate is the review, so member results are
    used only for their timestamps: the aggregate document carries no time of
    its own, and it is written immediately after the last member returns. Only
    the members the aggregate cites may date it, and all of them must be
    present, so an unrelated result of the same iteration cannot skew it.

    That proxy has one known limit. If the last member returns just before the
    window ends and aggregation completes just after, the decision is counted
    in the earlier window even though it did not yet exist. Closing that gap
    needs a canonical timestamp on the aggregate itself, which is an evidence
    schema change rather than a reporting one.

    Nothing here raises. Evidence is read as it is found, and every unusable
    document lowers this job's readability instead of interrupting the scan.
    """

    messages, messages_complete = _message_documents(evidence_root, job_id)
    batches, batches_complete = _batch_documents(evidence_root, job_id)
    readable = messages_complete and batches_complete

    members: dict[int, dict[str, tuple[datetime, int | None]]] = {}
    seen_identities: set[str] = set()
    reviews: list[ReviewEvent] = []
    dispositions: list[DispositionEvent] = []
    aggregates: list[AggregateReview] = []

    for message_type, relative, document in messages:
        occurred_at = _parse_timestamp(document.get('created_at'))
        iteration = _iteration_of(document)
        payload = document.get('payload')
        if not isinstance(payload, dict) or occurred_at is None:
            readable = False
            continue
        if document.get('run_id') != job_id:
            readable = False
            continue
        # One message identity may appear once. A document copied to a second
        # canonical name would otherwise be counted twice.
        identity = document.get('message_id')
        if not isinstance(identity, str) or identity in seen_identities:
            readable = False
            continue
        seen_identities.add(identity)
        if message_type == 'review_result':
            # The body must agree with the sequence its filename declares, so a
            # document copied under another name is neither counted twice nor
            # accepted as a member that was never written.
            ordinal = evidence_ordinal('review_result', relative)
            if iteration is None or ordinal is None:
                readable = False
                continue
            if document.get('sequence') != ordinal:
                readable = False
                continue
            member_findings = payload.get('findings')
            # A member whose findings array is missing or malformed has an
            # unknown count, not a count of zero. Recording it as None keeps a
            # schema-1 aggregate that cites it from understating what was
            # raised.
            members.setdefault(iteration, {})[relative] = (
                occurred_at,
                len(member_findings) if isinstance(member_findings, list) else None,
            )
            # A member of a reviewer set is never a review by itself. Counting
            # one when its aggregate is missing or pruned would turn a single
            # decision into as many reviews as the set had members.
            if _member_reviewer(relative) is not None:
                continue
            verdict = payload.get('verdict')
            findings = payload.get('findings')
            if verdict not in VERDICTS or not isinstance(findings, list):
                readable = False
                continue
            reviews.append(
                ReviewEvent(job_id, iteration, verdict, occurred_at, len(findings))
            )
        elif message_type == 'developer_handoff':
            entries = payload.get('dispositions')
            if _handoff_relative(document) != relative:
                readable = False
                continue
            if not isinstance(entries, list):
                readable = False
                continue
            for entry in entries:
                disposition = (
                    entry.get('disposition') if isinstance(entry, dict) else None
                )
                if disposition in DISPOSITIONS:
                    dispositions.append(
                        DispositionEvent(job_id, disposition, occurred_at)
                    )
                else:
                    readable = False

    for iteration, document in sorted(batches.items()):
        verdict = document.get('verdict')
        if verdict not in VERDICTS or document.get('run_id') != job_id:
            readable = False
            continue
        # Only the members this aggregate actually cites may date it. Any other
        # result of the same iteration is not part of this decision.
        cited = _cited_result_paths(document)
        if cited is None:
            readable = False
            continue
        present = {
            relative: value
            for relative, value in members.get(iteration, {}).items()
            if relative in cited
        }
        if len(present) != len(cited) or len(set(cited)) != len(cited):
            readable = False
            continue
        raised = _aggregate_findings(document, present.values())
        if raised is None:
            readable = False
            continue
        aggregates.append(
            AggregateReview(
                job_id,
                iteration,
                verdict,
                raised,
                max((moment for moment, _ in present.values()), default=None),
            )
        )

    return JobEvents(
        tuple(reviews), tuple(dispositions), tuple(aggregates), readable=readable
    )


def _member_reviewer(relative: str) -> str | None:
    """
    Return the reviewer a canonical result path is qualified by, if any.

    Reviewer identity lives in the filename. The strict review-result envelope
    has no `reviewer_id` field and forbids extras, so the body cannot say which
    member wrote it and the path is the only place that can.
    """

    try:
        ordinal = evidence_ordinal('review_result', relative)
        if ordinal is None:
            return None
        return reviewer_id_from_message_path(
            Path(relative), sequence=ordinal, message_type='review_result'
        )
    except WorkerError, ValueError:
        return None


def _aggregate_findings(
    document: Mapping[str, Any], members: Iterable[tuple[datetime, int | None]]
) -> int | None:
    """
    Return how many findings one aggregate raised, or None when unusable.

    Schema 1 has no aggregate findings array; its findings live in the member
    results it cites, so they are summed from there. Schema 2 onward declares
    the array, and its absence there is damage rather than age.
    """

    findings = document.get('findings')
    if document.get('schema_version') == 1:
        if findings is not None:
            return None
        counts = [count for _, count in members]
        if any(count is None for count in counts):
            return None
        return sum(count for count in counts if count is not None)
    return len(findings) if isinstance(findings, list) else None


def _handoff_relative(document: Mapping[str, Any]) -> str | None:
    """
    Return one developer handoff's job-relative path from its own sequence.

    Holding a handoff to the path it claims is what stops a copy under a second
    canonical name from contributing its dispositions a second time.
    """

    sequence = document.get('sequence')
    if not isinstance(sequence, int):
        return None
    return evidence_path('developer_handoff', ordinal=sequence)


def _cited_result_paths(document: Mapping[str, Any]) -> list[str] | None:
    """
    Return every member result path one aggregate decision references.

    A member whose result was never persisted is cited with a null path and is
    absent here. That is deliberate: an incomplete member is exactly how a
    blocked batch is recorded, so requiring every member to have a result would
    discard the blocked verdicts this report is meant to count. Every member
    that does claim a path must still be present.

    The paths are returned as a list rather than a set so the caller can see a
    repeated citation. Two completed members naming one result is damaged
    evidence, and collapsing it would quietly count that member's findings once
    for both.
    """

    members = document.get('reviewers')
    if not isinstance(members, list) or not members:
        # An aggregate is a decision over members. One that declares none is
        # malformed, and returning an empty citation list would let it pass the
        # completeness checks and be counted as a real verdict.
        return None
    cited: list[str] = []
    for member in members:
        # A member that cannot be read is not a member that can be skipped.
        # Filtering it out would leave the remaining citations self-consistent,
        # and the aggregate would count as a whole decision it is not.
        if not isinstance(member, dict) or 'result_path' not in member:
            return None
        result_path = member['result_path']
        if isinstance(result_path, str):
            cited.append(result_path)
        elif result_path is not None:
            return None
    return cited


def _review_exits(
    transitions: Iterable[JobTransition],
) -> dict[int, datetime]:
    """
    Return when each review round left `REVIEWING`, keyed by iteration.

    This is the only durable record of a verdict's time when no member result
    carries one. Rounds are numbered by the order they left review, which is
    the order the aggregates were written.

    Leaving review for `INTERRUPTED` is not the end of a round: the same
    iteration resumes afterwards. Counting it would consume an iteration number
    and shift every later round onto the wrong instant.
    """

    exits = sorted(
        transition.occurred_at
        for transition in transitions
        if transition.from_state is RunState.REVIEWING
        and transition.to_state is not RunState.INTERRUPTED
    )
    return dict(enumerate(exits, start=1))


def _reviewed_in_window(
    transitions: Iterable[JobTransition], start: datetime, end: datetime
) -> bool:
    """
    Return whether durable state shows a completed review inside the window.

    This is the fallback for a job whose evidence is gone: transitions survive
    in the database when the files do not. Only leaving `REVIEWING` for a
    terminal outcome counts. Entering it says a review started, and leaving it
    for `INTERRUPTED` says one was suspended; neither produced a verdict.
    """

    return any(
        start <= transition.occurred_at < end
        and transition.from_state is RunState.REVIEWING
        and transition.to_state is not RunState.INTERRUPTED
        for transition in transitions
    )


def build_stats_document(
    runs: Sequence[Run | UnreadableJob],
    *,
    evidence_root: Path,
    start: datetime,
    end: datetime,
    since: str,
    transitions: Mapping[str, tuple[JobTransition, ...]],
) -> dict[str, object]:
    """
    Return the review statistics payload for one resolved window.

    `jobs` and `reviews` count different things on purpose and are not expected
    to agree. `jobs` classifies each job by its most recent verdict, so its
    values plus `unavailable.count` equal `jobs_total`. `reviews` counts verdict
    events, so a job reviewed three times contributes three.

    A job enters the report only by having an event inside the window. Being
    created inside it is not enough, and being created before it is no bar.
    """

    job_verdicts: dict[str, list[ReviewEvent]] = {}
    review_counts = dict.fromkeys(VERDICTS, 0)
    finding_counts = {'raised': 0} | dict.fromkeys(DISPOSITIONS, 0)
    unavailable_ids: list[str] = []
    unavailable_reasons: dict[str, int] = {}

    def report_unavailable(job_id: str, code: str) -> None:
        """Record one job whose in-window history could not be recovered."""

        unavailable_ids.append(job_id)
        unavailable_reasons[code] = unavailable_reasons.get(code, 0) + 1

    for run in runs:
        job_id = run.job_id if isinstance(run, UnreadableJob) else str(run.id)
        reviewed = _reviewed_in_window(transitions.get(job_id, ()), start, end)
        if isinstance(run, UnreadableJob):
            # The row cannot be decoded, but its transitions can, so a job with
            # an in-window verdict is reported rather than disappearing.
            if reviewed:
                report_unavailable(job_id, run.error.code)
            continue
        events = read_job_events(evidence_root, job_id)
        # A reviewer-set decision is dated by the transition that left review,
        # which is when it actually completed. Its member results were written
        # before that, so preferring them could place a verdict in a window it
        # did not yet exist in.
        exits = _review_exits(transitions.get(job_id, ()))
        dated = list(events.reviews)
        for aggregate in events.aggregates:
            moment = exits.get(aggregate.iteration, aggregate.fallback)
            if moment is not None:
                dated.append(
                    ReviewEvent(
                        aggregate.job_id,
                        aggregate.iteration,
                        aggregate.verdict,
                        moment,
                        aggregate.findings,
                    )
                )
        in_window = [review for review in dated if start <= review.occurred_at < end]
        for review in in_window:
            review_counts[review.verdict] += 1
            finding_counts['raised'] += review.findings
        contributed = bool(in_window)
        for disposition in events.dispositions:
            if start <= disposition.occurred_at < end:
                finding_counts[disposition.disposition] += 1
                contributed = True
        # A job is classified by its standing, which is its latest verdict at
        # or before the window ends, not only by the verdicts inside it.
        # Remediation routinely lands in the window after the review it answers,
        # and counting that job's findings while leaving the job itself out of
        # every bucket would describe work on a job the document does not admit
        # exists.
        if contributed:
            standing = [review for review in dated if review.occurred_at < end]
            if standing:
                job_verdicts[job_id] = standing
        # Any usable in-window history keeps a job off the unavailable list,
        # whether it was a verdict or a disposition. Only a job that gave the
        # report nothing is reported as unrecoverable.
        if contributed or events.readable:
            continue
        if reviewed:
            report_unavailable(job_id, 'invalid_evidence')

    job_counts = dict.fromkeys(VERDICTS, 0)
    for reviews in job_verdicts.values():
        latest = max(reviews, key=lambda review: (review.occurred_at, review.iteration))
        job_counts[latest.verdict] += 1

    return {
        'window': {
            'since': since,
            'start': _isoformat(start),
            'end': _isoformat(end),
            'timezone': 'UTC',
        },
        'jobs_total': len(job_verdicts) + len(unavailable_ids),
        'jobs': job_counts,
        'reviews': review_counts,
        'findings': finding_counts,
        'unavailable': {
            'count': len(unavailable_ids),
            'job_ids': sorted(unavailable_ids),
            'reasons': unavailable_reasons,
        },
    }


def _isoformat(value: datetime) -> str:
    """
    Return one UTC timestamp in the form every public document uses.

    The trailing `Z` rather than `+00:00` matches the canonical messages this
    report reads, so a consumer sees one timestamp format across the tool.
    """

    return value.isoformat().replace('+00:00', 'Z')
