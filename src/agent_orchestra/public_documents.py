"""
Select public fields from validated canonical evidence documents.

Persisted schemas and public CLI documents evolve independently. These
projections deliberately name nested fields instead of inheriting additions to
an evidence model through its model_dump or a dictionary spread.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REVIEW_FINDING_FIELDS = (
    'finding_id',
    'severity',
    'title',
    'path',
    'line',
    'explanation',
    'acceptance_criterion',
)
ISSUE_FINDING_FIELDS = (
    'finding_id',
    'dimension',
    'severity',
    'title',
    'section',
    'explanation',
    'suggested_change',
)
BATCH_FINDING_FIELDS = (*REVIEW_FINDING_FIELDS, 'reviewer_id', 'source_finding_id')
BATCH_FIELDS = (
    'schema_version',
    'iteration',
    'reviewer_set_id',
    'aggregation_policy',
    'diff_digest',
    'verdict',
    'reviewers',
    'changes_requested_by',
    'blocked_by',
    'incomplete_reviewers',
)
EVIDENCE_FIELDS = ('job_id', 'evidence_type', 'path', 'size', 'sha256', 'finalized_at')


def _select(document: Mapping[str, object], fields: Sequence[str]) -> dict[str, object]:
    """
    Return the explicitly named fields present in a validated document.

    Preserve missing optional and legacy fields rather than inventing values.
    Validation belongs to the canonical evidence reader before projection.
    """

    return {field: document[field] for field in fields if field in document}


def finding_document(document: Mapping[str, object]) -> dict[str, object]:
    """
    Return one public source, aggregate, or issue finding.

    Preserve each established vocabulary while withholding unknown fields.
    """

    fields = (
        ISSUE_FINDING_FIELDS
        if 'dimension' in document
        else BATCH_FINDING_FIELDS
        if 'reviewer_id' in document
        else REVIEW_FINDING_FIELDS
    )
    return _select(document, fields)


def batch_document(document: Mapping[str, object]) -> dict[str, object]:
    """
    Return the public reviewer batch, including its explicit nested projections.

    Preserve optional schema-2 findings and schema-3 correlation fields. The
    caller adds the public job identity and evidence path.
    """

    result = _select(document, BATCH_FIELDS)
    result['reviewers'] = [
        _select(member, ('reviewer_id', 'outcome', 'result_path'))
        for member in cast('list[Mapping[str, object]]', document['reviewers'])
    ]
    if 'findings' in document:
        result['findings'] = [
            finding_document(item)
            for item in cast('list[Mapping[str, object]]', document['findings'])
        ]
    result.update(_select(document, ('message_id', 'artifact_path')))
    return result


def review_result_document(document: Mapping[str, object]) -> dict[str, object]:
    """
    Return the public per-reviewer result payload.

    The caller adds message identity and paths; internal payload additions are
    withheld until this projection explicitly selects them.
    """

    result = _select(document, ('verdict', 'summary'))
    result['findings'] = [
        finding_document(item)
        for item in cast('list[Mapping[str, object]]', document['findings'])
    ]
    result.update(
        _select(document, ('validation', 'verification_gaps', 'artifact_path'))
    )
    return result


def history_document(
    document: Mapping[str, object],
    *,
    path: str,
    evidence_type: str,
    iteration: object,
) -> dict[str, object]:
    """
    Return an audit history summary with explicit findings and dispositions.

    Canonical envelopes use payload fields; issue and batch results use fields
    at the document root. Validation commands retain their existing string or
    structured outcome vocabulary.
    """

    payload = cast('Mapping[str, object]', document.get('payload') or {})
    findings = cast(
        'list[Mapping[str, object]]',
        document.get('findings') or payload.get('findings', []),
    )
    return {
        'path': path,
        'evidence_type': evidence_type,
        'iteration': document.get('iteration', iteration),
        'message_id': document.get('message_id'),
        'verdict': document.get('verdict') or payload.get('verdict'),
        'findings': [finding_document(item) for item in findings],
        'dispositions': [
            _select(item, ('finding_id', 'disposition', 'rationale'))
            for item in cast(
                'list[Mapping[str, object]]', payload.get('dispositions', [])
            )
        ],
        'validation': [
            _select(item, ('command', 'outcome')) if isinstance(item, dict) else item
            for item in cast('list[object]', payload.get('validation', []))
        ],
    }


def evidence_document(
    document: Mapping[str, object], *, status: str
) -> dict[str, object]:
    """
    Return public integrity metadata with the audit's verification status.

    Withhold future index and retention-marker fields on every audit path.
    """

    return {**_select(document, EVIDENCE_FIELDS), 'status': status}


def retention_policy_document(document: Mapping[str, object]) -> dict[str, object]:
    """
    Return the public retention policy recorded with expired evidence.

    Preserve missing policy values while withholding future marker metadata.
    """

    return _select(document, ('older_than_days',))
