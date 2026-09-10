"""Load and validate packaged declarative runtime and provider knowledge."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from pathlib import PurePosixPath
from string import Formatter
from typing import Any

from agent_orchestra.adapter.registry import (
    DEFAULT_RUNTIME_REGISTRY,
    RuntimeRegistry,
    RuntimeRole,
)

MANIFEST_ENGINE_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
# Deliberately not 'manifests': a data directory sharing this module's name
# would become the import target as soon as it gained an __init__.py.
MANIFEST_DIRECTORY = 'manifest'
PROVIDER_MANIFEST_IDS = ('github', 'gitlab')
MANIFEST_IDS = (
    *PROVIDER_MANIFEST_IDS,
    *DEFAULT_RUNTIME_REGISTRY.identifiers(),
    'evidence',
    'assignments',
)
MALFORMED_MANIFEST = 'manifest_malformed'
ENGINE_TOO_OLD = 'manifest_engine_too_old'
UNSUPPORTED_MANIFEST_VERSION = 'manifest_schema_version_unsupported'
EVIDENCE_MANIFEST = 'evidence'
ASSIGNMENT_MANIFEST = 'assignments'
# Roles told what to do by packaged data rather than by a skill they load.
# developer and reviewer are skill-driven, so an assignment for either would
# contradict the role mapping in docs/design.md.
ASSIGNMENT_ROLES = frozenset({RuntimeRole.ISSUE_REVIEWER.value})
COMMON_FIELDS = {'id', 'kind', 'schema_version', 'min_engine_version'}
STATIC_MANIFEST_KINDS = {
    'github': 'provider',
    'gitlab': 'provider',
    EVIDENCE_MANIFEST: 'evidence',
    ASSIGNMENT_MANIFEST: 'assignment',
}
EVIDENCE_TYPES = (
    'issue_snapshot',
    'issue_review_request',
    'issue_review_result',
    'review_request',
    'review_result',
    'remediation_request',
    'developer_handoff',
    'review_batch_result',
)
MESSAGE_EVIDENCE_TYPES = frozenset(
    {'review_request', 'review_result', 'remediation_request', 'developer_handoff'}
)


class ManifestError(RuntimeError):
    """Report a stable packaged-manifest loading failure."""

    def __init__(self, code: str, manifest_id: str) -> None:
        """Create a manifest error with a machine-readable code."""

        self.code = code
        self.manifest_id = manifest_id
        super().__init__(f'{code}: {manifest_id}')


@dataclass(frozen=True, slots=True)
class Manifest:
    """One validated immutable manifest document."""

    manifest_id: str
    kind: str
    schema_version: int
    min_engine_version: int
    data: dict[str, Any]


@cache
def load_manifest(manifest_id: str) -> Manifest:
    """Load one packaged TOML manifest and fail closed on invalid data."""

    resource = files('agent_orchestra').joinpath(
        MANIFEST_DIRECTORY, f'{manifest_id}.toml'
    )
    try:
        content = resource.read_text(encoding='utf-8')
    except (OSError, UnicodeError) as error:
        raise ManifestError(MALFORMED_MANIFEST, manifest_id) from error
    return parse_manifest(manifest_id, content)


def parse_manifest(
    manifest_id: str,
    content: str,
    *,
    runtime_registry: RuntimeRegistry = DEFAULT_RUNTIME_REGISTRY,
) -> Manifest:
    """Parse and validate manifest TOML for runtime and test callers."""

    try:
        document = tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        raise ManifestError(MALFORMED_MANIFEST, manifest_id) from error
    required = COMMON_FIELDS
    if (
        not isinstance(document, dict)
        or not required.issubset(document)
        or document.get('id') != manifest_id
        or not isinstance(document.get('kind'), str)
        or type(document.get('schema_version')) is not int
        or type(document.get('min_engine_version')) is not int
    ):
        raise ManifestError(MALFORMED_MANIFEST, manifest_id)
    minimum = int(document['min_engine_version'])
    if minimum > MANIFEST_ENGINE_VERSION:
        raise ManifestError(ENGINE_TOO_OLD, manifest_id)
    if document['schema_version'] != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(UNSUPPORTED_MANIFEST_VERSION, manifest_id)
    kind = str(document['kind'])
    expected_kind = STATIC_MANIFEST_KINDS.get(manifest_id)
    if expected_kind is None and manifest_id in runtime_registry.identifiers():
        expected_kind = 'runtime'
    if expected_kind != kind:
        raise ManifestError(MALFORMED_MANIFEST, manifest_id)
    try:
        if kind == 'provider':
            _validate_provider(document)
        elif kind == 'runtime':
            _validate_runtime(manifest_id, document, runtime_registry)
        elif kind == 'evidence':
            _validate_evidence(document)
        elif kind == 'assignment':
            _validate_assignment(document)
        else:
            raise ManifestError(MALFORMED_MANIFEST, manifest_id)
    except (re.error, TypeError, ValueError) as error:
        raise ManifestError(MALFORMED_MANIFEST, manifest_id) from error
    return Manifest(
        manifest_id=manifest_id,
        kind=kind,
        schema_version=int(document['schema_version']),
        min_engine_version=minimum,
        data=document,
    )


def validate_packaged_manifests() -> tuple[Manifest, ...]:
    """Load every shipped manifest in deterministic identifier order."""

    return tuple(load_manifest(manifest_id) for manifest_id in MANIFEST_IDS)


def _validate_provider(document: dict[str, Any]) -> None:
    """Validate ordered provider failure rules."""

    if set(document) != COMMON_FIELDS | {'failure_rules'}:
        raise TypeError
    rules = document.get('failure_rules')
    if not isinstance(rules, list) or not rules:
        raise TypeError
    for rule in rules:
        if (
            not isinstance(rule, dict)
            or set(rule) != {'code', 'patterns'}
            or not isinstance(rule.get('code'), str)
            or not rule['code']
        ):
            raise TypeError
        patterns = rule.get('patterns')
        if not isinstance(patterns, list) or not patterns:
            raise TypeError
        for pattern in patterns:
            if not isinstance(pattern, str):
                raise TypeError
            re.compile(pattern)


def _validate_runtime(
    manifest_id: str,
    document: dict[str, Any],
    runtime_registry: RuntimeRegistry,
) -> None:
    """Validate named ordered adapter argument profiles."""

    if set(document) != COMMON_FIELDS | {'profiles'}:
        raise TypeError
    profiles = document.get('profiles')
    runtime = runtime_registry.require(manifest_id)
    required_profiles = {role.value for role in RuntimeRole if runtime.supports(role)}
    if not isinstance(profiles, dict) or set(profiles) != required_profiles:
        raise TypeError
    allowed = set(runtime.manifest_placeholders)
    for arguments in profiles.values():
        if (
            not isinstance(arguments, list)
            or not arguments
            or not all(isinstance(argument, str) for argument in arguments)
        ):
            raise TypeError
        fields: set[str] = set()
        for argument in arguments:
            fields.update(_validate_format(argument, allowed))
        if fields != allowed:
            raise TypeError


def _validate_assignment(document: dict[str, Any]) -> None:
    """Validate one role assignment template per declared agent role."""

    if set(document) != COMMON_FIELDS | {'assignments'}:
        raise TypeError
    entries = document.get('assignments')
    # Exact equality, not a subset: startup validation must fail when the
    # assignment a consumer needs is absent, rather than deferring to a
    # manifest_malformed at dispatch time.
    if not isinstance(entries, dict) or set(entries) != ASSIGNMENT_ROLES:
        raise TypeError
    for entry in entries.values():
        if not isinstance(entry, dict) or set(entry) != {'template'}:
            raise TypeError
        template = entry['template']
        if not isinstance(template, str) or not template.strip():
            raise TypeError
        if _validate_format(template, {'request'}) != {'request'}:
            raise TypeError


def _validate_evidence(document: dict[str, Any]) -> None:
    """Validate evidence templates and recognition patterns."""

    if set(document) != COMMON_FIELDS | {'evidence'}:
        raise TypeError
    entries = document.get('evidence')
    if not isinstance(entries, dict) or tuple(entries) != EVIDENCE_TYPES:
        raise TypeError
    for evidence_type, entry in entries.items():
        fields = {'template', 'pattern'}
        if evidence_type == 'issue_snapshot':
            fields.add('root_template')
        if evidence_type in {'review_request', 'review_result'}:
            fields.add('reviewer_template')
        if not isinstance(entry, dict) or set(entry) != fields:
            raise TypeError
        template = entry.get('template')
        pattern = entry.get('pattern')
        if not isinstance(template, str) or not isinstance(pattern, str):
            raise TypeError
        compiled = re.compile(pattern)
        _validate_evidence_template(template, requires_ordinal=True)
        rendered = template.format(ordinal=1)
        if compiled.fullmatch(rendered) is None:
            raise TypeError
        if evidence_type in MESSAGE_EVIDENCE_TYPES:
            parts = PurePosixPath(rendered).parts
            if len(parts) != 2 or parts[0] != 'messages':
                raise TypeError
        reviewer_template = entry.get('reviewer_template')
        if reviewer_template is not None:
            if not isinstance(reviewer_template, str):
                raise TypeError
            _validate_evidence_template(
                reviewer_template, requires_ordinal=True, requires_reviewer=True
            )
            reviewer_rendered = reviewer_template.format(ordinal=1, reviewer_id='codex')
            if compiled.fullmatch(reviewer_rendered) is None:
                raise TypeError
        root_template = entry.get('root_template')
        if root_template is not None:
            if not isinstance(root_template, str):
                raise TypeError
            _validate_evidence_template(root_template, requires_ordinal=False)
            if compiled.fullmatch(root_template) is None:
                raise TypeError
    for evidence_type, entry in entries.items():
        templates = [entry['template']]
        if 'reviewer_template' in entry:
            templates.append(entry['reviewer_template'])
        for template in templates:
            rendered = template.format(ordinal=1, reviewer_id='codex')
            matches = [
                candidate
                for candidate, candidate_entry in entries.items()
                if re.fullmatch(candidate_entry['pattern'], rendered)
            ]
            if matches != [evidence_type]:
                raise TypeError


def _validate_format(value: str, allowed_fields: set[str]) -> set[str]:
    """Validate format syntax and return the runtime inputs used by one value."""

    parsed = list(Formatter().parse(value))
    fields: set[str] = set()
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if field_name not in allowed_fields or format_spec or conversion is not None:
            raise TypeError
        fields.add(field_name)
    return fields


def _validate_evidence_template(
    template: str, *, requires_ordinal: bool, requires_reviewer: bool = False
) -> None:
    """Require one normalized, relative, safely renderable evidence path."""

    parsed = list(Formatter().parse(template))
    fields = [
        (field_name, format_spec, conversion)
        for _, field_name, format_spec, conversion in parsed
        if field_name is not None
    ]
    expected = [('ordinal', '06d', None)] if requires_ordinal else []
    if requires_reviewer:
        expected.append(('reviewer_id', '', None))
    if fields != expected:
        raise TypeError
    rendered = template.format(ordinal=1, reviewer_id='codex')
    path = PurePosixPath(rendered)
    if (
        not rendered
        or '\\' in rendered
        or path.is_absolute()
        or any(part in {'', '.', '..'} for part in path.parts)
        or path.as_posix() != rendered
    ):
        raise TypeError


def _evidence_template_pattern(template: str) -> re.Pattern[str]:
    """Compile one validated ordinal template into a discovery pattern."""

    fragments: list[str] = []
    for literal, field_name, _, _ in Formatter().parse(template):
        fragments.append(re.escape(literal))
        if field_name is not None:
            fragments.append(
                r'(?P<ordinal>\d{6})'
                if field_name == 'ordinal'
                else r'(?P<reviewer_id>[a-z0-9][a-z0-9_-]*)'
            )
    return re.compile(''.join(fragments))


def adapter_arguments(runtime: str, profile: str, **values: str) -> list[str]:
    """Render one ordered adapter argument profile."""

    manifest = load_manifest(runtime)
    profiles = manifest.data.get('profiles')
    arguments = profiles.get(profile) if isinstance(profiles, dict) else None
    if not isinstance(arguments, list) or not all(
        isinstance(argument, str) for argument in arguments
    ):
        raise ManifestError(MALFORMED_MANIFEST, runtime)
    try:
        return [argument.format_map(values) for argument in arguments]
    except (KeyError, TypeError, ValueError) as error:
        raise ManifestError(MALFORMED_MANIFEST, runtime) from error


def classify_provider_failure(provider: str, diagnostic: str) -> str:
    """Apply ordered provider failure rules and return a stable error code."""

    manifest = load_manifest(provider)
    rules = manifest.data.get('failure_rules')
    if not isinstance(rules, list):
        raise ManifestError(MALFORMED_MANIFEST, provider)
    for rule in rules:
        if not isinstance(rule, dict):
            raise ManifestError(MALFORMED_MANIFEST, provider)
        code = rule.get('code')
        patterns = rule.get('patterns')
        if (
            not isinstance(code, str)
            or not isinstance(patterns, list)
            or not all(isinstance(pattern, str) for pattern in patterns)
        ):
            raise ManifestError(MALFORMED_MANIFEST, provider)
        if any(re.search(pattern, diagnostic, re.IGNORECASE) for pattern in patterns):
            return code
    return 'issue_lookup_failed'


def evidence_path(
    evidence_type: str,
    *,
    ordinal: int | None = None,
    reviewer_id: str | None = None,
) -> str:
    """Render a canonical evidence path from the shared evidence manifest."""

    manifest = load_manifest(EVIDENCE_MANIFEST)
    entries = manifest.data.get('evidence')
    entry = entries.get(evidence_type) if isinstance(entries, dict) else None
    template_key = (
        'root_template'
        if ordinal is None
        else 'reviewer_template'
        if reviewer_id is not None
        else 'template'
    )
    template = entry.get(template_key) if isinstance(entry, dict) else None
    if not isinstance(template, str):
        raise ManifestError(MALFORMED_MANIFEST, EVIDENCE_MANIFEST)
    try:
        return template.format(ordinal=ordinal, reviewer_id=reviewer_id)
    except (KeyError, TypeError, ValueError) as error:
        raise ManifestError(MALFORMED_MANIFEST, EVIDENCE_MANIFEST) from error


def role_assignment(role: str, *, request: str) -> str:
    """Render one role's packaged assignment around its request document."""

    manifest = load_manifest(ASSIGNMENT_MANIFEST)
    entries = manifest.data.get('assignments')
    entry = entries.get(role) if isinstance(entries, dict) else None
    template = entry.get('template') if isinstance(entry, dict) else None
    if not isinstance(template, str):
        raise ManifestError(MALFORMED_MANIFEST, ASSIGNMENT_MANIFEST)
    try:
        return template.format(request=request)
    except (IndexError, KeyError, ValueError) as error:
        raise ManifestError(MALFORMED_MANIFEST, ASSIGNMENT_MANIFEST) from error


def canonical_evidence_type(relative: str) -> str | None:
    """Return the canonical evidence type matching one ordered manifest rule."""

    manifest = load_manifest(EVIDENCE_MANIFEST)
    entries = manifest.data.get('evidence')
    if not isinstance(entries, dict):
        raise ManifestError(MALFORMED_MANIFEST, EVIDENCE_MANIFEST)
    for evidence_type, entry in entries.items():
        pattern = entry.get('pattern') if isinstance(entry, dict) else None
        if not isinstance(evidence_type, str) or not isinstance(pattern, str):
            raise ManifestError(MALFORMED_MANIFEST, EVIDENCE_MANIFEST)
        if re.fullmatch(pattern, relative):
            return evidence_type
    return None


def manifest_owns_evidence_namespace(relative: str) -> bool:
    """Return whether a path occupies a manifest-owned top-level namespace."""

    manifest = load_manifest(EVIDENCE_MANIFEST)
    entries = manifest.data.get('evidence')
    if not isinstance(entries, dict):
        raise ManifestError(MALFORMED_MANIFEST, EVIDENCE_MANIFEST)
    namespaces: set[str] = set()
    for entry in entries.values():
        template = entry.get('template') if isinstance(entry, dict) else None
        if not isinstance(template, str):
            raise ManifestError(MALFORMED_MANIFEST, EVIDENCE_MANIFEST)
        parts = PurePosixPath(template.format(ordinal=1)).parts
        if len(parts) > 1:
            namespaces.add(parts[0])
    relative_parts = PurePosixPath(relative).parts
    return bool(relative_parts) and relative_parts[0] in namespaces


def canonical_message_evidence(relative: str) -> tuple[str, int] | None:
    """Return the manifest-declared message type and sequence for one path."""

    manifest = load_manifest(EVIDENCE_MANIFEST)
    entries = manifest.data['evidence']
    for evidence_type in MESSAGE_EVIDENCE_TYPES:
        entry = entries[evidence_type]
        for key in ('template', 'reviewer_template'):
            template = entry.get(key)
            if not isinstance(template, str):
                continue
            match = _evidence_template_pattern(template).fullmatch(relative)
            if match is not None:
                return evidence_type, int(match.group('ordinal'))
    return None


def evidence_ordinal(evidence_type: str, relative: str) -> int | None:
    """Extract an ordinal from one path using its manifest writer template."""

    manifest = load_manifest(EVIDENCE_MANIFEST)
    entries = manifest.data.get('evidence')
    entry = entries.get(evidence_type) if isinstance(entries, dict) else None
    templates = (
        tuple(
            template
            for key in ('template', 'reviewer_template')
            if isinstance((template := entry.get(key)), str)
        )
        if isinstance(entry, dict)
        else ()
    )
    if not templates:
        raise ManifestError(MALFORMED_MANIFEST, EVIDENCE_MANIFEST)
    for template in templates:
        match = _evidence_template_pattern(template).fullmatch(relative)
        if match is not None:
            return int(match.group('ordinal'))
    return None
