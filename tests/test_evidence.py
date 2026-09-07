"""Tests for contained evidence paths and integrity index writes."""

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

import pytest

import agent_orchestra.evidence as evidence_module
from agent_orchestra.evidence import (
    EvidencePathError,
    finalize_evidence_write,
    record_finalized_evidence,
    recover_evidence_index,
    resolve_evidence_path,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_resolve_evidence_path_contains_a_job_path(tmp_path: Path) -> None:
    """Resolve normal job-relative evidence under its selected root."""

    assert resolve_evidence_path(tmp_path, 'job-1', 'messages', 'request.json') == (
        tmp_path / 'job-1/messages/request.json'
    )


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


def test_record_finalized_evidence_writes_job_relative_index(tmp_path: Path) -> None:
    """Record stable size and digest metadata through atomic index replacement."""

    evidence = resolve_evidence_path(tmp_path, 'job-1', 'messages', 'request.json')
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b'hello')

    entry = record_finalized_evidence(tmp_path, 'job-1', evidence, 'review_request')

    document = json.loads((tmp_path / 'job-1/.integrity.json').read_text())
    assert entry.path == 'messages/request.json'
    assert entry.size == 5
    assert (
        entry.sha256
        == 'sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824'
    )
    assert document['job_id'] == 'job-1'
    assert document['entries'] == [asdict(entry)]


def test_record_finalized_evidence_replaces_a_path_entry(tmp_path: Path) -> None:
    """Keep one current integrity entry for a rewritten finalized path."""

    evidence = resolve_evidence_path(tmp_path, 'job-1', 'execution.json')
    evidence.parent.mkdir(parents=True)
    evidence.write_text('first')
    record_finalized_evidence(tmp_path, 'job-1', evidence, 'execution')
    evidence.write_text('second')

    latest = record_finalized_evidence(tmp_path, 'job-1', evidence, 'execution')

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
    original_update = evidence_module._update_index

    def interrupt_update(*_args: object) -> None:
        """Simulate process loss after the evidence rename."""

        message = 'simulated interruption'
        raise OSError(message)

    monkeypatch.setattr(evidence_module, '_update_index', interrupt_update)
    with pytest.raises(OSError, match='simulated interruption'):
        finalize_evidence_write(tmp_path, 'job-1', temporary, evidence, 'request')
    monkeypatch.setattr(evidence_module, '_update_index', original_update)

    recover_evidence_index(tmp_path, 'job-1')

    document = json.loads((job / '.integrity.json').read_text())
    assert [entry['path'] for entry in document['entries']] == ['messages/request.json']
    assert not (job / '.integrity.pending.json').exists()


@pytest.mark.parametrize(
    'document',
    [
        {'schema_version': 1, 'job_id': 'other', 'entries': []},
        {
            'schema_version': 1,
            'job_id': 'job-1',
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
        record_finalized_evidence(tmp_path, 'job-1', evidence, 'execution')
