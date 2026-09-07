"""Tests for provider-neutral issue capture."""

import inspect
import json
import os
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agent_orchestra.issue_sources import (
    GitHubIssueProvider,
    GitLabIssueProvider,
    IssueProvider,
    IssueSourceError,
    fetch_issue,
    parse_issue_url,
    publish_feedback,
    write_snapshot,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_issue_providers_implement_abstract_interface() -> None:
    """Keep provider selection polymorphic instead of branching on an enum."""

    assert inspect.isabstract(IssueProvider)
    assert isinstance(GitHubIssueProvider(), IssueProvider)
    assert isinstance(GitLabIssueProvider(), IssueProvider)


@pytest.mark.parametrize(
    ('url', 'expected'),
    [
        (
            'https://github.com/acme/widgets/issues/12',
            ('github', 'github.com', 'acme', 'widgets', 12),
        ),
        (
            'https://gitlab.com/acme/platform/widgets/-/issues/42',
            ('gitlab', 'gitlab.com', 'acme/platform', 'widgets', 42),
        ),
    ],
)
def test_parse_issue_url(
    url: str,
    expected: tuple[str, str, str, str, int],
) -> None:
    """Parse provider identity, including nested GitLab namespaces."""

    locator = parse_issue_url(url)

    assert (
        locator.provider,
        locator.host,
        locator.namespace,
        locator.project,
        locator.number,
    ) == expected


@pytest.mark.parametrize(
    'url',
    [
        'http://github.com/acme/widgets/issues/1',
        'https://github.com/acme/widgets/pull/1',
        'https://github.com/acme/widgets/issues/0',
        'https://gitlab.com/acme/widgets/issues/1',
        'https://gitlab.com/acme/widgets/-/issues/nope',
        'https://github.com/acme/widgets/issues/1?x=1',
    ],
)
def test_parse_issue_url_rejects_noncanonical_urls(url: str) -> None:
    """Reject ambiguous or malformed provider URLs."""

    with pytest.raises(IssueSourceError, match='invalid_issue_url'):
        parse_issue_url(url)


def test_parse_issue_url_accepts_self_managed_gitlab_host_purely() -> None:
    """Parse self-managed GitLab syntax without consulting local credentials."""

    locator = parse_issue_url(
        'https://gitlab.example.test:8443/group/project/-/issues/7'
    )

    assert (locator.provider, locator.host, locator.namespace, locator.project) == (
        'gitlab',
        'gitlab.example.test:8443',
        'group',
        'project',
    )


def test_parse_issue_url_rejects_gitlab_shape_on_github_host() -> None:
    """Do not route malformed GitHub URLs through the GitLab provider."""

    with pytest.raises(IssueSourceError, match='invalid_issue_url'):
        parse_issue_url('https://github.com/acme/widgets/-/issues/7')


def test_fetch_reports_unconfigured_self_managed_gitlab_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return an actionable code when glab has no credentials for the host."""

    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which', lambda command: f'/bin/{command}'
    )
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.subprocess.run',
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 1, '', 'unknown host'
        ),
    )

    with pytest.raises(IssueSourceError, match='gitlab_host_not_configured'):
        fetch_issue('https://gitlab.example.test/acme/widgets/-/issues/7')


@pytest.mark.parametrize(
    ('diagnostic', 'expected_code'),
    [
        ('gh: authentication required (HTTP 401)', 'provider_authentication_required'),
        ('gh: Not Found (HTTP 404)', 'issue_not_found'),
        ('gh: Forbidden (HTTP 403)', 'issue_inaccessible'),
        ('provider failed; see the login URL in docs', 'issue_lookup_failed'),
    ],
)
def test_fetch_classifies_fake_github_cli_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostic: str,
    expected_code: str,
) -> None:
    """Pin stable failure codes at the provider-process boundary."""

    executable = tmp_path / 'gh'
    executable.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "{diagnostic}" >&2\nexit 1\n',
        encoding='utf-8',
    )
    executable.chmod(0o755)
    monkeypatch.setenv('PATH', f'{tmp_path}{os.pathsep}{os.environ.get("PATH", "")}')

    with pytest.raises(IssueSourceError) as raised:
        fetch_issue('https://github.com/acme/widgets/issues/12')

    assert raised.value.code == expected_code


def _completed(document: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    """Return a successful provider command result."""

    return subprocess.CompletedProcess([], 0, json.dumps(document), '')


def test_fetch_github_issue_normalizes_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normalize a GitHub response without retaining provider payload shape."""

    document = {
        'title': 'Clarify behavior',
        'body': 'Details',
        'user': {'login': 'octocat'},
        'labels': [{'name': 'feature'}, {'name': 'cli'}],
        'state': 'open',
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-02T00:00:00Z',
        'html_url': 'https://github.com/acme/widgets/issues/12',
    }
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which',
        lambda command: f'/bin/{command}',
    )
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.subprocess.run',
        lambda *_args, **_kwargs: _completed(document),
    )

    snapshot = fetch_issue('https://github.com/acme/widgets/issues/12')

    assert snapshot.locator.provider == 'github'
    assert snapshot.labels == ('cli', 'feature')
    assert snapshot.document()['issue_number'] == 12
    assert snapshot.digest.startswith('sha256:')


def test_fetch_rejects_mismatched_canonical_issue_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require the provider response URL to retain the requested identity."""

    document = {
        'title': 'Clarify behavior',
        'body': 'Details',
        'user': {'login': 'octocat'},
        'labels': [],
        'state': 'open',
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-02T00:00:00Z',
        'html_url': 'https://github.com/acme/other/issues/12',
    }
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which', lambda command: f'/bin/{command}'
    )
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.subprocess.run',
        lambda *_args, **_kwargs: _completed(document),
    )

    with pytest.raises(IssueSourceError, match='invalid_provider_document'):
        fetch_issue('https://github.com/acme/widgets/issues/12')


def test_fetch_bounds_provider_cli_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report a stable failure when a provider lookup exceeds its time bound."""

    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which', lambda command: f'/bin/{command}'
    )

    def timeout(*_args: object, **_kwargs: object) -> object:
        """Simulate a hung provider CLI."""

        command = 'gh'
        raise subprocess.TimeoutExpired(command, 60)

    monkeypatch.setattr('agent_orchestra.issue_sources.subprocess.run', timeout)

    with pytest.raises(IssueSourceError, match='provider_timeout'):
        fetch_issue('https://github.com/acme/widgets/issues/12')


def test_fetch_gitlab_issue_uses_encoded_nested_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Address GitLab projects by encoded full path and project-scoped IID."""

    document = {
        'title': 'Clarify behavior',
        'description': 'Details',
        'author': {'username': 'tanuki'},
        'labels': ['feature', 'cli'],
        'state': 'opened',
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-02T00:00:00Z',
        'web_url': 'https://gitlab.com/acme/platform/widgets/-/issues/42',
    }
    commands: list[list[str]] = []
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which',
        lambda command: f'/bin/{command}',
    )

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _completed(document)

    monkeypatch.setattr('agent_orchestra.issue_sources.subprocess.run', run)

    snapshot = fetch_issue('https://gitlab.com/acme/platform/widgets/-/issues/42')

    assert snapshot.locator.provider == 'gitlab'
    assert commands == [
        [
            '/bin/glab',
            'api',
            '--hostname',
            'gitlab.com',
            'projects/acme%2Fplatform%2Fwidgets/issues/42',
        ]
    ]


def test_digest_is_stable_across_provider_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hash only normalized review-relevant fields."""

    github = {
        'title': 'Same',
        'body': 'Body',
        'user': {'login': 'one'},
        'labels': [{'name': 'a'}],
        'state': 'open',
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-02T00:00:00Z',
        'html_url': 'https://github.com/a/b/issues/1',
    }
    gitlab = {
        'title': 'Same',
        'description': 'Body',
        'author': {'username': 'two'},
        'labels': ['a'],
        'state': 'open',
        'created_at': '2025-01-01T00:00:00Z',
        'updated_at': '2025-01-02T00:00:00Z',
        'web_url': 'https://gitlab.com/a/b/-/issues/1',
    }
    responses = iter((_completed(github), _completed(gitlab)))
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which',
        lambda command: f'/bin/{command}',
    )
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.subprocess.run',
        lambda *_args, **_kwargs: next(responses),
    )

    first = fetch_issue('https://github.com/a/b/issues/1')
    second = fetch_issue('https://gitlab.com/a/b/-/issues/1')

    assert first.digest == second.digest


def test_digest_ignores_provider_label_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonicalize label sets before persistence and source hashing."""

    base = {
        'title': 'Same',
        'body': 'Body',
        'user': {'login': 'one'},
        'state': 'open',
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-02T00:00:00Z',
        'html_url': 'https://github.com/a/b/issues/1',
    }
    responses = iter(
        (
            _completed({**base, 'labels': [{'name': 'z'}, {'name': 'a'}]}),
            _completed({**base, 'labels': [{'name': 'a'}, {'name': 'z'}]}),
        )
    )
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which', lambda _command: '/bin/gh'
    )
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.subprocess.run',
        lambda *_args, **_kwargs: next(responses),
    )

    first = fetch_issue('https://github.com/a/b/issues/1')
    second = fetch_issue('https://github.com/a/b/issues/1')

    assert first.labels == second.labels == ('a', 'z')
    assert first.digest == second.digest


def test_write_snapshot_rejects_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not replace an attacker-selected symlink target."""

    target = tmp_path / 'target'
    target.write_text('safe')
    path = tmp_path / 'issue.json'
    path.symlink_to(target)
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which',
        lambda command: f'/bin/{command}',
    )

    with pytest.raises(IssueSourceError, match='unsafe_evidence_path'):
        write_snapshot(path, object())  # type: ignore[arg-type]

    assert target.read_text() == 'safe'


@pytest.mark.parametrize(
    ('url', 'expected_endpoint'),
    [
        (
            'https://github.com/acme/widgets/issues/12',
            'repos/acme/widgets/issues/12/comments',
        ),
        (
            'https://gitlab.example.test/acme/widgets/-/issues/12',
            'projects/acme%2Fwidgets/issues/12/notes',
        ),
    ],
)
def test_publish_feedback_posts_once(
    url: str, expected_endpoint: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check for an idempotency marker before posting provider feedback."""

    commands: list[list[str]] = []
    responses = iter(
        (
            subprocess.CompletedProcess([], 0, '[]', ''),
            subprocess.CompletedProcess(
                [],
                0,
                json.dumps({'id': 7, 'html_url': 'https://example.test/comment/7'}),
                '',
            ),
        )
    )
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which',
        lambda command: f'/bin/{command}',
    )
    monkeypatch.setattr(
        'agent_orchestra.issue_sources._require_gitlab_host',
        lambda _executable, _host: None,
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return next(responses)

    monkeypatch.setattr('agent_orchestra.issue_sources.subprocess.run', run)

    published = publish_feedback(
        parse_issue_url(url), 'Feedback', idempotency_marker='marker'
    )

    assert commands[0][-1] == f'{expected_endpoint}?per_page=100&page=1'
    assert expected_endpoint in commands[1]
    assert commands[1][-2:] == ['body=Feedback\n\nmarker', '--input=-'] or commands[1][
        -2:
    ] == ['--raw-field', 'body=Feedback\n\nmarker']
    assert published.provider_id == '7'


def test_publish_feedback_recovers_existing_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return an existing provider message instead of posting a duplicate."""

    calls: list[list[str]] = []
    existing = [{'id': 9, 'body': 'Prior\nmarker', 'html_url': 'https://x/9'}]
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which', lambda _command: '/bin/gh'
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess([], 0, json.dumps(existing), '')

    monkeypatch.setattr('agent_orchestra.issue_sources.subprocess.run', run)

    published = publish_feedback(
        parse_issue_url('https://github.com/acme/widgets/issues/12'),
        'Feedback',
        idempotency_marker='marker',
    )

    assert published.provider_id == '9'
    assert len(calls) == 1


def test_publish_feedback_searches_later_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recover a marked message even when it is not on the first API page."""

    calls: list[list[str]] = []
    pages = iter(
        (
            [{'id': number, 'body': 'other'} for number in range(100)],
            [{'id': 101, 'body': 'marker', 'html_url': 'https://x/101'}],
        )
    )
    monkeypatch.setattr(
        'agent_orchestra.issue_sources.shutil.which', lambda _command: '/bin/gh'
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess([], 0, json.dumps(next(pages)), '')

    monkeypatch.setattr('agent_orchestra.issue_sources.subprocess.run', run)

    published = publish_feedback(
        parse_issue_url('https://github.com/acme/widgets/issues/12'),
        'Feedback',
        idempotency_marker='marker',
    )

    assert published.provider_id == '101'
    assert calls[-1][-1].endswith('page=2')
