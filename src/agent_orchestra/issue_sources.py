"""Capture normalized immutable issue snapshots from supported providers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Never
from urllib.parse import quote, urlparse
from uuid import uuid4

PROVIDER_TIMEOUT_SECONDS = 60

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


class IssueSourceError(RuntimeError):
    """Raised when an issue source cannot be resolved safely."""

    def __init__(self, code: str, diagnostic: str | None = None) -> None:
        """Create a categorized provider-source failure."""

        self.code = code
        self.diagnostic = diagnostic
        message = f'{code}: {diagnostic}' if diagnostic else code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class IssueLocator:
    """Provider identity parsed from one canonical issue URL."""

    provider: str
    host: str
    namespace: str
    project: str
    number: int
    url: str


@dataclass(frozen=True, slots=True)
class IssueSnapshot:
    """Provider-neutral review fields captured from one issue revision."""

    locator: IssueLocator
    title: str
    body: str
    author: str
    labels: tuple[str, ...]
    state: str
    created_at: str
    updated_at: str
    digest: str

    def document(self) -> dict[str, object]:
        """Return the canonical persisted source document."""

        return {
            'schema_version': 1,
            'provider': self.locator.provider,
            'host': self.locator.host,
            'url': self.locator.url,
            'namespace': self.locator.namespace,
            'project': self.locator.project,
            'issue_number': self.locator.number,
            'title': self.title,
            'body': self.body,
            'author': self.author,
            'labels': list(self.labels),
            'state': self.state,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'source_digest': self.digest,
        }


@dataclass(frozen=True, slots=True)
class ProviderFeedback:
    """Identity returned after publishing or finding one feedback message."""

    provider_id: str
    url: str


class IssueProvider(ABC):
    """Interface implemented by each issue hosting provider."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the stable provider identifier persisted in job evidence."""

    @abstractmethod
    def parse(self, url: str) -> IssueLocator | None:
        """Return a locator when this implementation recognizes the URL."""

    @abstractmethod
    def fetch(self, locator: IssueLocator) -> IssueSnapshot:
        """Fetch and normalize one issue revision."""

    @abstractmethod
    def publish_feedback(
        self, locator: IssueLocator, body: str, *, idempotency_marker: str
    ) -> ProviderFeedback:
        """Publish or recover one idempotently marked feedback message."""


def _fail(
    code: str, cause: BaseException | None = None, *, diagnostic: str | None = None
) -> Never:
    """Raise one stable issue-source error."""

    if cause is not None:
        raise IssueSourceError(code, diagnostic) from cause
    raise IssueSourceError(code, diagnostic)


def _url_parts(url: str) -> tuple[str, tuple[str, ...]] | None:
    """Return validated HTTPS host and path components."""

    parsed = urlparse(url)
    parts = tuple(part for part in parsed.path.split('/') if part)
    invalid = (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.params
        or parsed.query
        or parsed.fragment
    )
    if invalid:
        return None
    host_value = parsed.hostname
    if host_value is None:
        return None
    host = parsed.netloc.lower()
    return host, parts


def _issue_number(number_text: str, url: str) -> int:
    """Parse one positive provider issue number."""

    try:
        number = int(number_text)
    except ValueError as error:
        _fail('invalid_issue_url', error, diagnostic=url)
    if number < 1:
        _fail('invalid_issue_url', diagnostic=url)
    return number


def _require_gitlab_host(executable: str, host: str) -> None:
    """Require a non-default GitLab host to have local glab credentials."""

    if host == 'gitlab.com':
        return
    try:
        completed = subprocess.run(
            [executable, 'auth', 'status', '--hostname', host],
            check=False,
            capture_output=True,
            text=True,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        _fail('provider_timeout', error)
    except OSError as error:
        _fail('provider_execution_failed', error, diagnostic=str(error))
    if completed.returncode != 0:
        remedy = f'run glab auth login --hostname {host}'
        _fail('gitlab_host_not_configured', diagnostic=remedy)


def _source_digest(fields: dict[str, object]) -> str:
    """Hash normalized review-relevant fields using canonical JSON."""

    encoded = json.dumps(
        fields, ensure_ascii=False, separators=(',', ':'), sort_keys=True
    ).encode()
    return f'sha256:{hashlib.sha256(encoded).hexdigest()}'


def _run(command: Sequence[str]) -> dict[str, Any]:
    """Run one provider CLI lookup and parse its JSON object response."""

    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        _fail('provider_timeout', error)
    except OSError as error:
        _fail('provider_execution_failed', error, diagnostic=str(error))
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or 'issue lookup failed'
        lowered = diagnostic.lower()
        code = (
            'issue_not_found'
            if re.search(r'\b404\b', lowered) or 'not found' in lowered
            else 'issue_inaccessible'
            if re.search(r'\b403\b', lowered) or 'forbidden' in lowered
            else 'provider_authentication_required'
            if re.search(r'\b401\b', lowered)
            or any(
                phrase in lowered
                for phrase in (
                    'authentication required',
                    'not authenticated',
                    'not logged in',
                    'gh auth login',
                    'glab auth login',
                )
            )
            else 'issue_lookup_failed'
        )
        _fail(code, diagnostic=diagnostic)
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        _fail('invalid_provider_document', error)
    if not isinstance(result, dict):
        _fail('invalid_provider_document')
    return result


def _required_text(document: dict[str, Any], path: tuple[str, ...]) -> str:
    """Read one required nested string from a provider document."""

    value: Any = document
    try:
        for component in path:
            value = value[component]
    except (KeyError, TypeError) as error:
        _fail('invalid_provider_document', error)
    if not isinstance(value, str):
        _fail('invalid_provider_document')
    return value


def _normalized_snapshot(
    locator: IssueLocator,
    document: dict[str, Any],
    *,
    body_field: str,
    author_path: tuple[str, ...],
    url_field: str,
    labels_are_objects: bool,
) -> IssueSnapshot:
    """Normalize common provider document fields into one snapshot."""

    title = _required_text(document, ('title',))
    body_value = document.get(body_field)
    if body_value is not None and not isinstance(body_value, str):
        _fail('invalid_provider_document')
    author = _required_text(document, author_path)
    raw_labels = document.get('labels')
    if not isinstance(raw_labels, list):
        _fail('invalid_provider_document')
    if labels_are_objects:
        try:
            labels = tuple(label['name'] for label in raw_labels)
        except (KeyError, TypeError) as error:
            _fail('invalid_provider_document', error)
    else:
        labels = tuple(raw_labels)
    if not all(isinstance(label, str) for label in labels):
        _fail('invalid_provider_document')
    labels = tuple(sorted(set(labels)))
    state = _required_text(document, ('state',))
    created_at = _required_text(document, ('created_at',))
    updated_at = _required_text(document, ('updated_at',))
    canonical_url = _required_text(document, (url_field,))
    try:
        canonical_locator = issue_provider(locator.provider).parse(canonical_url)
    except IssueSourceError as error:
        _fail('invalid_provider_document', error)
    if canonical_locator is None or (
        canonical_locator.provider,
        canonical_locator.host,
        canonical_locator.namespace,
        canonical_locator.project,
        canonical_locator.number,
    ) != (
        locator.provider,
        locator.host,
        locator.namespace,
        locator.project,
        locator.number,
    ):
        _fail('invalid_provider_document', diagnostic='canonical URL identity mismatch')
    body = body_value or ''
    digest = _source_digest(
        {'title': title, 'body': body, 'labels': list(labels), 'state': state}
    )
    return IssueSnapshot(
        canonical_locator,
        title,
        body,
        author,
        labels,
        state,
        created_at,
        updated_at,
        digest,
    )


def _provider_command(executable_name: str, locator: IssueLocator) -> list[str]:
    """Return a provider CLI API prefix."""

    executable = shutil.which(executable_name)
    if executable is None:
        _fail('provider_cli_not_found', diagnostic=executable_name)
    command = [executable, 'api']
    if executable_name == 'glab':
        _require_gitlab_host(executable, locator.host)
        command.extend(['--hostname', locator.host])
    return command


def _publish(
    command: list[str],
    endpoint: str,
    locator: IssueLocator,
    body: str,
    idempotency_marker: str,
    *,
    url_field: str,
) -> ProviderFeedback:
    """Publish or recover one provider message using a hidden marker."""

    for page in range(1, 1001):
        existing = _run_feedback([*command, f'{endpoint}?per_page=100&page={page}'])
        if not isinstance(existing, list):
            _fail('invalid_provider_document')
        for item in existing:
            if isinstance(item, dict) and idempotency_marker in str(
                item.get('body', '')
            ):
                provider_id = item.get('id')
                remote_url = item.get(url_field)
                if isinstance(provider_id, int):
                    resolved_url = (
                        remote_url
                        if isinstance(remote_url, str)
                        else f'{locator.url}#note_{provider_id}'
                    )
                    return ProviderFeedback(str(provider_id), resolved_url)
        if len(existing) < 100:
            break
    else:
        _fail('provider_pagination_limit')
    created = _run_feedback(
        [
            *command,
            endpoint,
            '--method',
            'POST',
            '--raw-field',
            f'body={body}\n\n{idempotency_marker}',
        ]
    )
    if not isinstance(created, dict):
        _fail('invalid_provider_document')
    provider_id = created.get('id')
    remote_url = created.get(url_field)
    if not isinstance(provider_id, int):
        _fail('invalid_provider_document')
    resolved_url = (
        remote_url
        if isinstance(remote_url, str)
        else f'{locator.url}#note_{provider_id}'
    )
    return ProviderFeedback(str(provider_id), resolved_url)


class GitHubIssueProvider(IssueProvider):
    """GitHub issue access implemented through the authenticated gh CLI."""

    @property
    def name(self) -> str:
        """Return the persisted provider identifier."""

        return 'github'

    def parse(self, url: str) -> IssueLocator | None:
        """Parse a canonical github.com issue URL."""

        parsed = _url_parts(url)
        if parsed is None:
            return None
        host, parts = parsed
        if host != 'github.com' or len(parts) != 4 or parts[2] != 'issues':
            return None
        return IssueLocator(
            self.name, host, parts[0], parts[1], _issue_number(parts[3], url), url
        )

    def fetch(self, locator: IssueLocator) -> IssueSnapshot:
        """Fetch and normalize one GitHub issue."""

        command = _provider_command('gh', locator)
        document = _run(
            [
                *command,
                f'repos/{locator.namespace}/{locator.project}/issues/{locator.number}',
            ]
        )
        if 'pull_request' in document:
            _fail('invalid_provider_document', diagnostic='pull request returned')
        return _normalized_snapshot(
            locator,
            document,
            body_field='body',
            author_path=('user', 'login'),
            url_field='html_url',
            labels_are_objects=True,
        )

    def publish_feedback(
        self, locator: IssueLocator, body: str, *, idempotency_marker: str
    ) -> ProviderFeedback:
        """Publish or recover a GitHub issue comment."""

        endpoint = (
            f'repos/{locator.namespace}/{locator.project}/issues/'
            f'{locator.number}/comments'
        )
        return _publish(
            _provider_command('gh', locator),
            endpoint,
            locator,
            body,
            idempotency_marker,
            url_field='html_url',
        )


class GitLabIssueProvider(IssueProvider):
    """GitLab issue access implemented through the authenticated glab CLI."""

    @property
    def name(self) -> str:
        """Return the persisted provider identifier."""

        return 'gitlab'

    def parse(self, url: str) -> IssueLocator | None:
        """Parse a canonical GitLab issue URL, including nested namespaces."""

        parsed = _url_parts(url)
        if parsed is None:
            return None
        host, parts = parsed
        if len(parts) < 5 or parts[-3:-1] != ('-', 'issues'):
            return None
        if host == 'github.com':
            return None
        namespace = '/'.join(parts[:-4])
        if not namespace:
            return None
        return IssueLocator(
            self.name,
            host,
            namespace,
            parts[-4],
            _issue_number(parts[-1], url),
            url,
        )

    def fetch(self, locator: IssueLocator) -> IssueSnapshot:
        """Fetch and normalize one GitLab issue."""

        project = quote(f'{locator.namespace}/{locator.project}', safe='')
        document = _run(
            [
                *_provider_command('glab', locator),
                f'projects/{project}/issues/{locator.number}',
            ]
        )
        return _normalized_snapshot(
            locator,
            document,
            body_field='description',
            author_path=('author', 'username'),
            url_field='web_url',
            labels_are_objects=False,
        )

    def publish_feedback(
        self, locator: IssueLocator, body: str, *, idempotency_marker: str
    ) -> ProviderFeedback:
        """Publish or recover a GitLab issue note."""

        project = quote(f'{locator.namespace}/{locator.project}', safe='')
        endpoint = f'projects/{project}/issues/{locator.number}/notes'
        return _publish(
            _provider_command('glab', locator),
            endpoint,
            locator,
            body,
            idempotency_marker,
            url_field='web_url',
        )


ISSUE_PROVIDERS: tuple[IssueProvider, ...] = (
    GitHubIssueProvider(),
    GitLabIssueProvider(),
)


def issue_provider(name: str) -> IssueProvider:
    """Return the registered provider implementation by stable identifier."""

    for provider in ISSUE_PROVIDERS:
        if provider.name == name:
            return provider
    _fail('unsupported_issue_provider', diagnostic=name)


def parse_issue_url(url: str) -> IssueLocator:
    """Parse a canonical issue URL using the registered providers."""

    for provider in ISSUE_PROVIDERS:
        locator = provider.parse(url)
        if locator is not None:
            return locator
    _fail('invalid_issue_url', diagnostic=url)


def fetch_issue(url: str) -> IssueSnapshot:
    """Fetch and normalize one issue through its provider implementation."""

    locator = parse_issue_url(url)
    return issue_provider(locator.provider).fetch(locator)


def write_snapshot(path: Path, snapshot: IssueSnapshot) -> None:
    """Write one source snapshot atomically without following a target symlink."""

    if path.is_symlink():
        _fail('unsafe_evidence_path', diagnostic='issue snapshot path is a symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(snapshot.document(), file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _run_feedback(command: Sequence[str]) -> Any:
    """Run a provider feedback request and decode its JSON response."""

    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        _fail('provider_timeout', error)
    except OSError as error:
        _fail('provider_execution_failed', error, diagnostic=str(error))
    if completed.returncode != 0:
        _fail(
            'feedback_publication_failed',
            diagnostic=completed.stderr.strip() or 'provider write failed',
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        _fail('invalid_provider_document', error)


def publish_feedback(
    locator: IssueLocator, body: str, *, idempotency_marker: str
) -> ProviderFeedback:
    """Publish feedback once, recovering an existing marked provider message."""

    return issue_provider(locator.provider).publish_feedback(
        locator, body, idempotency_marker=idempotency_marker
    )
