"""Deterministic tests for the provider-neutral live-runtime assertions."""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING, Any

import pytest

from tests.live.runtime_harness import (
    LIVE_FIXTURE_DIRECTORY,
    LIVE_VALIDATION_COMMAND,
    assert_consecutive_review_digests_change,
    assert_review_cycle_messages,
    create_defective_repository,
    run_command,
)

if TYPE_CHECKING:
    from pathlib import Path


def _review_documents(
    verdicts: tuple[str, ...],
    *,
    disposition: str = 'addressed',
    validation: tuple[dict[str, str], ...] = (),
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
                ],
                'validation': [dict(item) for item in validation],
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


@pytest.mark.parametrize(
    'validation',
    [
        (),
        ({'command': LIVE_VALIDATION_COMMAND, 'outcome': 'failed'},),
        ({'command': 'python -m unittest', 'outcome': 'passed'},),
    ],
)
def test_review_cycle_rejects_missing_live_validation(
    validation: tuple[dict[str, str], ...],
) -> None:
    """Reject missing, failed, or substituted live validation evidence."""

    requests, results, remediation_requests, handoffs = _review_documents(
        ('changes_requested', 'approved'), validation=validation
    )

    with pytest.raises(AssertionError):
        assert_review_cycle_messages(
            requests,
            results,
            remediation_requests,
            handoffs,
            required_validation_command=LIVE_VALIDATION_COMMAND,
        )


def test_review_cycle_accepts_exact_passed_live_validation() -> None:
    """Accept the exact fixture-owned validation command when it passed."""

    documents = _review_documents(
        ('changes_requested', 'approved'),
        validation=({'command': LIVE_VALIDATION_COMMAND, 'outcome': 'passed'},),
    )

    assert assert_review_cycle_messages(
        *documents, required_validation_command=LIVE_VALIDATION_COMMAND
    ) == ('reviewer', 'developer', 'reviewer')


def test_fixture_validation_uses_selected_interpreter_without_python_on_path(
    tmp_path: Path,
) -> None:
    """Execute the selected interpreter and forward its arguments without PATH."""

    repository = create_defective_repository(tmp_path)
    shutil.copy2(
        LIVE_FIXTURE_DIRECTORY / 'remediated' / 'calculator.py',
        repository / 'calculator.py',
    )
    empty_path = tmp_path / 'empty-path'
    empty_path.mkdir()
    environment = dict(os.environ, PATH=str(empty_path))

    assert shutil.which('python', path=environment['PATH']) is None
    completed = run_command(
        ['./validate', '-m', 'unittest'],
        cwd=repository,
        environment=environment,
    )

    assert completed.returncode == 0, completed.stderr
    forwarded = run_command(
        ['./validate', '-c', 'raise SystemExit(7)'],
        cwd=repository,
        environment=environment,
    )
    assert forwarded.returncode == 7


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
