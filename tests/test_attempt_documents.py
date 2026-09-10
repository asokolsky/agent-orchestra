"""Tests for the shared attempt-document projection."""

from __future__ import annotations

from dataclasses import fields

import pytest

from agent_orchestra.adapter.registry import RuntimeRole
from agent_orchestra.attempt_documents import (
    AUDIT_ATTEMPT_FIELDS,
    AUDIT_WITHHELD_FIELDS,
    CLI_ATTEMPT_FIELDS,
    CLI_WITHHELD_FIELDS,
    OMITTED_WHEN_NONE,
    project_attempt,
)
from agent_orchestra.invocations import (
    AttemptConclusion,
    AttemptStatus,
    EffectiveModelStatus,
    InvocationRecord,
)


def _record(*, reviewer_id: str | None = None) -> InvocationRecord:
    """Build one representative completed attempt record."""

    return InvocationRecord(
        schema_version=5,
        run_id='job-1',
        task_id='job-1:000001-reviewer',
        invocation_id='job-1:000001-reviewer:attempt-0001',
        role=RuntimeRole.REVIEWER,
        agent_vendor='vendor',
        requested_model=None,
        effective_models=('model-a',),
        effective_model_status=EffectiveModelStatus.REPORTED,
        runtime='runtime',
        iteration=1,
        started_at='2026-09-10T10:00:00Z',
        finished_at='2026-09-10T10:01:00Z',
        exit_code=0,
        timed_out=False,
        interrupted=False,
        stdout_path='logs/000001-reviewer.stdout.log',
        stderr_path='logs/000001-reviewer.stderr.log',
        attempt=1,
        status=AttemptStatus.COMPLETED,
        conclusion=AttemptConclusion.SUCCEEDED,
        response_received_at='2026-09-10T10:00:30Z',
        validation_started_at='2026-09-10T10:00:45Z',
        reviewer_id=reviewer_id,
    )


@pytest.mark.parametrize(
    ('published', 'withheld'),
    [
        (AUDIT_ATTEMPT_FIELDS, AUDIT_WITHHELD_FIELDS),
        (CLI_ATTEMPT_FIELDS, CLI_WITHHELD_FIELDS),
    ],
)
def test_every_record_field_is_classified_for_each_document(
    published: tuple[str, ...], withheld: tuple[str, ...]
) -> None:
    """Force a decision per document when the record gains or loses a field."""

    record_fields = {field.name for field in fields(InvocationRecord)}
    classified = set(published) | set(withheld)

    assert not set(published) & set(withheld)
    assert len(published) + len(withheld) == len(classified)
    assert classified - record_fields == set()
    assert record_fields - classified == set()


def test_audit_attempt_publishes_its_exact_declared_keys() -> None:
    """Fail when a field reaches the audit document without being listed."""

    attempt = project_attempt(_record(reviewer_id='security'), AUDIT_ATTEMPT_FIELDS)

    assert list(attempt) == list(AUDIT_ATTEMPT_FIELDS)
    assert 'run_id' not in attempt
    assert 'schema_version' not in attempt
    assert 'invocation_id' not in attempt


def test_cli_attempt_publishes_the_public_vocabulary() -> None:
    """Rename the internal identifier and withhold correlation fields."""

    attempt = project_attempt(
        _record(reviewer_id='security'),
        CLI_ATTEMPT_FIELDS,
        renames={'invocation_id': 'attempt_id'},
    )

    assert attempt['attempt_id'] == 'job-1:000001-reviewer:attempt-0001'
    assert 'invocation_id' not in attempt
    assert 'task_id' not in attempt
    assert 'role' not in attempt
    assert 'iteration' not in attempt


@pytest.mark.parametrize('published', [AUDIT_ATTEMPT_FIELDS, CLI_ATTEMPT_FIELDS])
def test_unset_optional_fields_are_omitted_rather_than_null(
    published: tuple[str, ...],
) -> None:
    """Keep an absent reviewer out of the document instead of publishing null."""

    attempt = project_attempt(_record(), published)

    for field in OMITTED_WHEN_NONE:
        assert field not in attempt
    assert attempt['requested_model'] is None


def test_tuple_values_project_as_json_ready_lists() -> None:
    """Serialize the ordered effective models without a per-caller conversion."""

    attempt = project_attempt(_record(), AUDIT_ATTEMPT_FIELDS)

    assert attempt['effective_models'] == ['model-a']
