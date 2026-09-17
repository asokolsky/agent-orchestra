"""Deterministic tests for the provider-neutral live-runtime assertions."""

from __future__ import annotations

from typing import Any

import pytest

from tests.live.runtime_harness import (
    assert_consecutive_review_digests_change,
    assert_review_cycle_messages,
)


def _review_documents(
    verdicts: tuple[str, ...], *, disposition: str = 'addressed'
) -> tuple[
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
    tuple[dict[str, Any], ...],
]:
    """Build minimal correlated documents for one review cycle."""

    requests = tuple(
        {
            'message_id': f'request-{index}',
            'scope': {'diff_digest': f'digest-{index}'},
        }
        for index in range(len(verdicts))
    )
    results = tuple(
        {
            'message_id': f'result-{index}',
            'in_reply_to': f'request-{index}',
            'scope': {'diff_digest': f'digest-{index}'},
            'payload': {
                'verdict': verdict,
                'findings': (
                    [{'finding_id': f'finding-{index}'}]
                    if verdict == 'changes_requested'
                    else []
                ),
            },
        }
        for index, verdict in enumerate(verdicts)
    )
    remediation_requests = tuple(
        {
            'message_id': f'remediation-{index}',
            'in_reply_to': f'result-{index}',
        }
        for index in range(len(verdicts) - 1)
    )
    handoffs = tuple(
        {
            'in_reply_to': f'remediation-{index}',
            'payload': {
                'dispositions': [
                    {
                        'finding_id': f'finding-{index}',
                        'disposition': disposition,
                    }
                ]
            },
        }
        for index in range(len(verdicts) - 1)
    )
    return requests, results, remediation_requests, handoffs


def test_review_cycle_accepts_three_iterations() -> None:
    """Use first and last semantics when remediation needs another round."""

    documents = _review_documents(
        ('changes_requested', 'changes_requested', 'approved')
    )

    assert assert_review_cycle_messages(*documents) == (
        'reviewer',
        'developer',
        'reviewer',
        'developer',
        'reviewer',
    )


@pytest.mark.parametrize('disposition', ['rejected', 'blocked'])
def test_review_cycle_accepts_non_addressed_disposition(disposition: str) -> None:
    """Permit every disposition defined by the developer role contract."""

    documents = _review_documents(
        ('changes_requested', 'approved'), disposition=disposition
    )

    assert assert_review_cycle_messages(*documents) == (
        'reviewer',
        'developer',
        'reviewer',
    )


def test_review_cycle_rejects_mismatched_developer_handoff() -> None:
    """Require every handoff to reply to its exact remediation request."""

    requests, results, remediation_requests, handoffs = _review_documents(
        ('changes_requested', 'approved')
    )
    handoffs[0]['in_reply_to'] = 'wrong-remediation-request'

    with pytest.raises(AssertionError):
        assert_review_cycle_messages(requests, results, remediation_requests, handoffs)


def test_review_digests_must_change_between_consecutive_rounds() -> None:
    """Reject a re-review request bound to the pre-remediation digest."""

    requests, _results, _remediation_requests, _handoffs = _review_documents(
        ('changes_requested', 'approved')
    )
    requests[1]['scope']['diff_digest'] = requests[0]['scope']['diff_digest']

    with pytest.raises(AssertionError):
        assert_consecutive_review_digests_change(requests)


def test_review_digests_may_return_to_an_earlier_value() -> None:
    """Enforce pairwise changes without requiring global digest uniqueness."""

    requests, _results, _remediation_requests, _handoffs = _review_documents(
        ('changes_requested', 'changes_requested', 'approved')
    )
    requests[2]['scope']['diff_digest'] = requests[0]['scope']['diff_digest']

    assert_consecutive_review_digests_change(requests)
