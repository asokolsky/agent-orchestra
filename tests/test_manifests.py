"""Tests for packaged declarative knowledge manifests."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from agent_orchestra import manifests

if TYPE_CHECKING:
    from collections.abc import Callable

from agent_orchestra.manifests import (
    MANIFEST_IDS,
    ManifestError,
    adapter_arguments,
    canonical_evidence_type,
    canonical_message_evidence,
    evidence_ordinal,
    evidence_path,
    load_manifest,
    parse_manifest,
)


def test_reviewer_qualified_messages_are_canonical_evidence() -> None:
    """Render and recognize reviewer-qualified request and result paths."""

    request = evidence_path('review_request', ordinal=3, reviewer_id='security')
    result = evidence_path('review_result', ordinal=4, reviewer_id='security')

    assert request == 'messages/000003-security-review-request.json'
    assert result == 'messages/000004-security-review-result.json'
    assert canonical_evidence_type(request) == 'review_request'
    assert canonical_evidence_type(result) == 'review_result'
    assert canonical_message_evidence(request) == ('review_request', 3)
    assert canonical_message_evidence(result) == ('review_result', 4)
    assert evidence_ordinal('review_request', request) == 3
    assert evidence_ordinal('review_result', result) == 4


@pytest.fixture(autouse=True)
def clear_manifest_cache() -> None:
    """Keep loader substitutions isolated while production callers reuse manifests."""

    load_manifest.cache_clear()


def test_every_packaged_manifest_is_schema_valid() -> None:
    """Validate every shipped manifest through the production loader."""

    assert [
        load_manifest(manifest_id).manifest_id for manifest_id in MANIFEST_IDS
    ] == list(MANIFEST_IDS)


def test_loader_reuses_validated_manifest() -> None:
    """Avoid reparsing immutable packaged data for each evidence file."""

    assert load_manifest('evidence') is load_manifest('evidence')


def test_manifest_rejects_newer_engine_and_malformed_schema() -> None:
    """Fail closed with stable codes for unusable manifests."""

    with pytest.raises(ManifestError, match='manifest_engine_too_old') as newer:
        parse_manifest(
            'future',
            'id="future"\nkind="provider"\nschema_version=1\nmin_engine_version=2\n',
        )
    assert newer.value.code == 'manifest_engine_too_old'
    with pytest.raises(ManifestError, match='manifest_malformed') as malformed:
        parse_manifest('broken', 'id="broken"\n')
    assert malformed.value.code == 'manifest_malformed'

    with pytest.raises(
        ManifestError, match='manifest_schema_version_unsupported'
    ) as unknown:
        parse_manifest(
            'future',
            'id="future"\nkind="provider"\nschema_version=2\nmin_engine_version=1\n',
        )
    assert unknown.value.code == 'manifest_schema_version_unsupported'


@pytest.mark.parametrize(
    'mutation',
    [
        lambda content: content.replace('kind = "evidence"', 'kind = "runtime"'),
        lambda content: content.replace(
            '\n[evidence.developer_handoff]',
            '\n[unknown]\nvalue = 1\n\n[evidence.developer_handoff]',
        ),
        lambda content: content.split('\n[evidence.developer_handoff]', 1)[0],
        lambda content: content.replace(
            'messages/{ordinal:06d}-review-request.json',
            '../../outside-{ordinal:06d}.json',
        ),
        lambda content: content.replace('messages/', 'archive/'),
    ],
)
def test_evidence_manifest_rejects_inconsistent_or_incomplete_shapes(
    mutation: Callable[[str], str],
) -> None:
    """Reject identity drift, unknown fields, omissions, and escaping paths."""

    content = (
        Path(__file__).parents[1] / 'src/agent_orchestra/manifests/evidence.toml'
    ).read_text(encoding='utf-8')
    with pytest.raises(ManifestError, match='manifest_malformed'):
        parse_manifest('evidence', mutation(content))


@pytest.mark.parametrize('argument', ['{cwd', '{unknown}', '{cwd!r}', '{cwd:>10}'])
def test_runtime_manifest_rejects_invalid_format_fields(argument: str) -> None:
    """Reject malformed, unknown, converted, and formatted runtime fields."""

    content = (
        Path(__file__).parents[1] / 'src/agent_orchestra/manifests/codex.toml'
    ).read_text(encoding='utf-8')
    content = content.replace('{cwd}', argument, 1)

    with pytest.raises(ManifestError, match='manifest_malformed'):
        parse_manifest('codex', content)


@pytest.mark.parametrize('runtime', ['codex', 'claude-code'])
@pytest.mark.parametrize('profile', ['reviewer', 'issue_reviewer', 'developer'])
def test_runtime_manifest_rejects_incomplete_profiles(
    runtime: str, profile: str
) -> None:
    """Require every adapter profile to supply all of its dynamic inputs."""

    path = Path(__file__).parents[1] / f'src/agent_orchestra/manifests/{runtime}.toml'
    content = path.read_text(encoding='utf-8')
    content = re.sub(
        rf'^{profile} = .*$',
        f'{profile} = ["exec"]',
        content,
        flags=re.MULTILINE,
    )

    with pytest.raises(ManifestError, match='manifest_malformed'):
        parse_manifest(runtime, content)


def test_loader_normalizes_invalid_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    """Convert package-data decoding failures to the stable malformed code."""

    resource = MagicMock()
    resource.joinpath.return_value = resource
    resource.read_text.side_effect = UnicodeDecodeError(
        'utf-8', b'\xff', 0, 1, 'invalid start byte'
    )
    monkeypatch.setattr(manifests, 'files', lambda _package: resource)

    with pytest.raises(ManifestError, match='manifest_malformed') as malformed:
        load_manifest('evidence')
    assert malformed.value.code == 'manifest_malformed'


def test_adapter_profiles_render_dynamic_values(tmp_path: Path) -> None:
    """Render ordered runtime flags without embedding them in adapter control flow."""

    codex = adapter_arguments(
        'codex', 'reviewer', cwd=str(tmp_path), schema='s', result='r'
    )
    claude = adapter_arguments(
        'claude-code', 'issue_reviewer', settings='{}', schema='s'
    )
    assert codex[codex.index('--output-schema') + 1] == 's'
    assert codex[codex.index('--output-last-message') + 1] == 'r'
    assert claude[claude.index('--settings') + 1] == '{}'
    assert claude[claude.index('--mcp-config') + 1] == '{"mcpServers":{}}'
