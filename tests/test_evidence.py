"""Tests for contained evidence paths and integrity index writes."""

import json
import os
import time
from dataclasses import asdict
from typing import TYPE_CHECKING

import pytest

from agent_orchestra.evidence import (
    EvidencePathError,
    JobEvidence,
    resolve_evidence_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_resolve_evidence_path_contains_a_job_path(tmp_path: Path) -> None:
    """Resolve normal job-relative evidence under its selected root."""

    assert resolve_evidence_path(tmp_path, 'job-1', 'messages', 'request.json') == (
        tmp_path / 'job-1/messages/request.json'
    )


def test_resolve_evidence_path_shards_generated_job_id_by_utc_date(
    tmp_path: Path,
) -> None:
    """Derive one deterministic UTC date shard from a generated job ID."""

    job_id = '20260908T235959Z-deadbeef'

    assert resolve_evidence_path(tmp_path, job_id, 'execution.json') == (
        tmp_path / '2026/09/08' / job_id / 'execution.json'
    )


def test_shard_derivation_is_timezone_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolve an immutable UTC identifier identically across extreme zones."""

    job_id = '20260909T063000Z-b4517e73'
    original_timezone = os.environ.get('TZ')
    paths: list[Path] = []
    try:
        for timezone in ('UTC', 'Pacific/Kiritimati', 'Etc/GMT+12'):
            monkeypatch.setenv('TZ', timezone)
            time.tzset()
            paths.append(resolve_evidence_path(tmp_path, job_id))
    finally:
        if original_timezone is None:
            monkeypatch.delenv('TZ', raising=False)
        else:
            monkeypatch.setenv('TZ', original_timezone)
        time.tzset()

    assert paths == [tmp_path / '2026/09/09' / job_id] * 3


@pytest.mark.parametrize(
    'job_id',
    [
        '20261301T000000Z-deadbeef',
        '20260932T000000Z-deadbeef',
        '20260908T240000Z-deadbeef',
        '20260908T000060Z-deadbeef',
    ],
)
def test_invalid_timestamp_shape_resolves_flat(tmp_path: Path, job_id: str) -> None:
    """Keep IDs outside the strict generated shape in the flat layout."""

    assert resolve_evidence_path(tmp_path, job_id) == tmp_path / job_id


def test_resolve_evidence_path_does_not_probe_old_timestamp_layout(
    tmp_path: Path,
) -> None:
    """Derive timestamp-shaped locations without probing an old flat path."""

    job_id = '20260908T235959Z-deadbeef'
    flat = tmp_path / job_id
    flat.mkdir()

    assert resolve_evidence_path(tmp_path, job_id) != flat
    assert resolve_evidence_path(tmp_path, job_id) == (tmp_path / '2026/09/08' / job_id)


@pytest.mark.parametrize('part', ['../other/file', '/absolute/file'])
def test_resolve_evidence_path_rejects_escape(tmp_path: Path, part: str) -> None:
    """Reject traversal and absolute evidence components."""

    with pytest.raises(EvidencePathError):
        resolve_evidence_path(tmp_path, 'job-1', part)


def test_resolve_evidence_path_rejects_symlink_escape(tmp_path: Path) -> None:
    """Reject a symlink in any selected job-relative component."""

    outside = tmp_path / 'outside'
    outside.mkdir()
    job = tmp_path / 'job-1'
    job.mkdir()
    (job / 'messages').symlink_to(outside, target_is_directory=True)

    with pytest.raises(EvidencePathError):
        resolve_evidence_path(tmp_path, 'job-1', 'messages', 'request.json')


def test_resolve_evidence_path_rejects_shard_symlink_escape(tmp_path: Path) -> None:
    """Reject a symlink in a generated job's shard hierarchy."""

    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / '2026').symlink_to(outside, target_is_directory=True)

    with pytest.raises(EvidencePathError, match='symlink'):
        resolve_evidence_path(tmp_path, '20260908T235959Z-deadbeef')


def test_record_finalized_evidence_writes_job_relative_index(tmp_path: Path) -> None:
    """Record stable size and digest metadata through atomic index replacement."""

    evidence = resolve_evidence_path(tmp_path, 'job-1', 'messages', 'request.json')
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b'hello')

    entry = JobEvidence(tmp_path, 'job-1').record_finalized(evidence, 'review_request')

    document = json.loads((tmp_path / 'job-1/.integrity.json').read_text())
    assert entry.path == 'messages/request.json'
    assert entry.size == 5
    assert (
        entry.sha256
        == 'sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
    )
    assert document['job_id'] == 'job-1'
    assert document['backfilled_at'] is not None
    assert document['entries'] == [asdict(entry)]


def test_record_finalized_evidence_replaces_a_path_entry(tmp_path: Path) -> None:
    """Keep one current integrity entry for a rewritten finalized path."""

    evidence = resolve_evidence_path(tmp_path, 'job-1', 'execution.json')
    evidence.parent.mkdir(parents=True)
    evidence.write_text('first')
    JobEvidence(tmp_path, 'job-1').record_finalized(evidence, 'execution')
    evidence.write_text('second')

    latest = JobEvidence(tmp_path, 'job-1').record_finalized(evidence, 'execution')

    document = json.loads((tmp_path / 'job-1/.integrity.json').read_text())
    assert document['entries'] == [asdict(latest)]


def test_recover_evidence_index_finishes_interrupted_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recover after the evidence rename but before index replacement."""

    job = tmp_path / 'job-1'
    evidence = job / 'messages/request.json'
    evidence.parent.mkdir(parents=True)
    temporary = evidence.with_name('.request.json.tmp')
    temporary.write_text('final')
    original_update = JobEvidence._update_index

    def interrupt_update(*_args: object, **_kwargs: object) -> None:
        """Simulate process loss after the evidence rename."""

        message = 'simulated interruption'
        raise OSError(message)

    monkeypatch.setattr(JobEvidence, '_update_index', interrupt_update)
    with pytest.raises(OSError, match='simulated interruption'):
        JobEvidence(tmp_path, 'job-1').finalize_write(
            temporary, evidence, 'review_request'
        )
    monkeypatch.setattr(JobEvidence, '_update_index', original_update)

    JobEvidence(tmp_path, 'job-1').recover_index()

    document = json.loads((job / '.integrity.json').read_text())
    assert document['backfilled_at'] is None
    assert [entry['path'] for entry in document['entries']] == ['messages/request.json']
    assert not (job / '.integrity.pending.json').exists()


@pytest.mark.parametrize(
    'document',
    [
        {
            'schema_version': 1,
            'job_id': 'other',
            'backfilled_at': None,
            'entries': [],
        },
        {
            'schema_version': 1,
            'job_id': 'job-1',
            'backfilled_at': None,
            'entries': [
                {
                    'job_id': 'job-1',
                    'evidence_type': 'request',
                    'path': '../escape',
                    'size': 1,
                    'sha256': f'sha256:{"0" * 64}',
                    'finalized_at': '2026-09-07T00:00:00Z',
                }
            ],
        },
        {
            'schema_version': 1,
            'job_id': 'job-1',
            'backfilled_at': None,
            'entries': [
                {
                    'job_id': 'job-1',
                    'evidence_type': 'request',
                    'path': 'request.json',
                    'size': 1,
                    'sha256': f'sha256:{"0" * 64}',
                    'finalized_at': '2026-09-07T00:00:00Z',
                }
            ]
            * 2,
        },
    ],
)
def test_record_finalized_evidence_rejects_miscorrelated_index(
    tmp_path: Path, document: dict[str, object]
) -> None:
    """Reject an existing index whose identity or paths are not trustworthy."""

    job = tmp_path / 'job-1'
    job.mkdir()
    evidence = job / 'execution.json'
    evidence.write_text('{}')
    (job / '.integrity.json').write_text(json.dumps(document))

    with pytest.raises(EvidencePathError, match='integrity index is malformed'):
        JobEvidence(tmp_path, 'job-1').record_finalized(evidence, 'execution')


def test_recover_relocation_removes_stale_source_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complete both sides of an indexed relocation after interruption."""

    job = tmp_path / 'job-1'
    source = job / 'artifacts/review.md'
    destination = job / 'logs/rejected-review.md'
    source.parent.mkdir(parents=True)
    destination.parent.mkdir()
    source.write_text('review')
    JobEvidence(tmp_path, 'job-1').record_finalized(source, 'review_artifact')
    original_update = JobEvidence._update_index

    def interrupt_update(*_args: object, **_kwargs: object) -> None:
        """Simulate exit after relocation and before index replacement."""

        message = 'simulated relocation interruption'
        raise OSError(message)

    monkeypatch.setattr(JobEvidence, '_update_index', interrupt_update)
    with pytest.raises(OSError, match='relocation interruption'):
        JobEvidence(tmp_path, 'job-1').relocate_finalized(
            source, destination, 'rejected_review_artifact'
        )
    monkeypatch.setattr(JobEvidence, '_update_index', original_update)

    JobEvidence(tmp_path, 'job-1').recover_index()

    document = json.loads((job / '.integrity.json').read_text())
    assert [entry['path'] for entry in document['entries']] == [
        'logs/rejected-review.md'
    ]


def test_recover_pre_rename_relocation_keeps_source_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not infer a move from a destination that existed before interruption."""

    job = tmp_path / 'job-1'
    source = job / 'artifacts/review.md'
    destination = job / 'logs/rejected-review.md'
    source.parent.mkdir(parents=True)
    destination.parent.mkdir()
    source.write_text('authoritative')
    destination.write_text('older destination')
    JobEvidence(tmp_path, 'job-1').record_finalized(source, 'review_artifact')
    before = json.loads((job / '.integrity.json').read_text())
    original_pending = JobEvidence._write_pending

    def interrupt_after_pending(*args: object, **kwargs: object) -> None:
        """Leave the durable relocation intent without performing its rename."""

        original_pending(*args, **kwargs)  # type: ignore[arg-type]
        message = 'simulated pre-rename interruption'
        raise OSError(message)

    monkeypatch.setattr(JobEvidence, '_write_pending', interrupt_after_pending)
    with pytest.raises(OSError, match='pre-rename interruption'):
        JobEvidence(tmp_path, 'job-1').relocate_finalized(
            source, destination, 'rejected_review_artifact'
        )
    monkeypatch.setattr(JobEvidence, '_write_pending', original_pending)

    JobEvidence(tmp_path, 'job-1').recover_index()

    assert source.read_text() == 'authoritative'
    assert destination.read_text() == 'older destination'
    assert json.loads((job / '.integrity.json').read_text()) == before


def test_lock_open_failure_reporting_differs_by_operation(tmp_path: Path) -> None:
    """Keep recovery reporting the raw OSError that writers translate."""

    job = tmp_path / 'job-1'
    evidence = job / 'messages/request.json'
    evidence.parent.mkdir(parents=True)
    evidence.write_text('final')
    job.chmod(0o500)
    try:
        # PermissionError is an OSError and EvidencePathError is a ValueError,
        # so requiring the former proves recovery does not translate the failure.
        with pytest.raises(PermissionError):
            JobEvidence(tmp_path, 'job-1').recover_index()

        with pytest.raises(EvidencePathError, match='cannot open integrity lock'):
            JobEvidence(tmp_path, 'job-1').record_finalized(evidence, 'review_request')
    finally:
        job.chmod(0o700)
