"""
Project one invocation record into its two public attempt vocabularies.

The audit and CLI attempt documents serve different consumers and deliberately
differ. They are built here by one mechanism so that difference is stated rather
than inherited from a construction style: each document names the record fields
it publishes and the fields it withholds, and every `InvocationRecord` field must
appear in exactly one of those two lists. Adding a field to the record therefore
forces a decision for each document instead of defaulting into one of them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from agent_orchestra.invocations import InvocationRecord

# Correlation fields audit publishes and the CLI does not: audit output is read
# against durable evidence on disk, so it must name the task and iteration a
# record belongs to.
AUDIT_ATTEMPT_FIELDS: tuple[str, ...] = (
    'task_id',
    'role',
    'agent_vendor',
    'requested_model',
    'effective_models',
    'effective_model_status',
    'runtime',
    'iteration',
    'started_at',
    'finished_at',
    'exit_code',
    'timed_out',
    'interrupted',
    'attempt',
    'status',
    'conclusion',
    'response_received_at',
    'validation_started_at',
    'reviewer_id',
)
AUDIT_WITHHELD_FIELDS: tuple[str, ...] = (
    # Retired vocabulary. Schema 8 replaced run_id with job_id across the public
    # surface; storage keeps the implementation-level name.
    'run_id',
    # The invocation record schema version, which is a different namespace from
    # the document's own schema_version and would collide with it under one key.
    'schema_version',
    # Published as attempt_id by the CLI vocabulary; audit carries task_id for
    # correlation and does not need the internal attempt identifier.
    'invocation_id',
    # Replaced by the streams object each document builds for its own consumer.
    'stdout_path',
    'stderr_path',
)

# The CLI publishes the public attempt vocabulary. Fields are listed in output
# order; the split exists because `legacy` and `streams` are not record fields
# and are inserted between the two groups.
CLI_ATTEMPT_HEAD_FIELDS: tuple[str, ...] = (
    'invocation_id',
    'attempt',
    'status',
    'conclusion',
    'agent_vendor',
    'requested_model',
    'effective_models',
    'effective_model_status',
    'runtime',
    'started_at',
    'finished_at',
    'response_received_at',
    'validation_started_at',
    'exit_code',
    'timed_out',
    'interrupted',
)
CLI_ATTEMPT_TAIL_FIELDS: tuple[str, ...] = ('reviewer_id',)
CLI_ATTEMPT_FIELDS: tuple[str, ...] = (
    *CLI_ATTEMPT_HEAD_FIELDS,
    *CLI_ATTEMPT_TAIL_FIELDS,
)
CLI_WITHHELD_FIELDS: tuple[str, ...] = (
    # Correlation fields belonging to the evidence layout rather than to the
    # job, task, and attempt vocabulary the CLI documents.
    'task_id',
    'role',
    'iteration',
    'run_id',
    'schema_version',
    'stdout_path',
    'stderr_path',
)
# The CLI renames the internal identifier to the documented public name.
CLI_ATTEMPT_RENAMES: Mapping[str, str] = {'invocation_id': 'attempt_id'}

# Fields omitted entirely when unset, rather than published as null.
OMITTED_WHEN_NONE: tuple[str, ...] = ('reviewer_id',)


def project_attempt(
    record: InvocationRecord,
    fields: Sequence[str],
    *,
    renames: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return the named record fields under one document's vocabulary."""

    renamed = renames or {}
    document: dict[str, object] = {}
    for field in fields:
        value = getattr(record, field)
        if field in OMITTED_WHEN_NONE and value is None:
            continue
        document[renamed.get(field, field)] = (
            list(value) if isinstance(value, tuple) else value
        )
    return document
