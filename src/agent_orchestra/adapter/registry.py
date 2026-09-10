"""Resolve supported runtime identities and role adapters from one registry."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from agent_orchestra.adapter.base import (
    DeveloperAdapter,
    IssueReviewerAdapter,
    ReviewerAdapter,
)
from agent_orchestra.persisted_enum import PersistedEnum

RUNTIME_REGISTRY_EMPTY = 'runtime registry requires non-empty identifiers'
RUNTIME_REGISTRY_DUPLICATE = 'runtime registry contains duplicate identifiers'
RUNTIME_DEFAULT_UNKNOWN = 'runtime registry default is not registered'
RUNTIME_UNKNOWN = 'runtime_unknown'
RUNTIME_ROLE_UNSUPPORTED = 'runtime_role_unsupported'
RUNTIME_ADAPTER_INVALID = 'runtime_adapter_invalid'

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


class RuntimeRole(PersistedEnum):
    """Canonical roles implemented by runtime adapters."""

    REVIEWER = 'reviewer'
    DEVELOPER = 'developer'
    ISSUE_REVIEWER = 'issue_reviewer'


class RuntimeRegistryError(ValueError):
    """Report a stable runtime lookup or capability failure."""

    def __init__(
        self, code: str, runtime: str, role: RuntimeRole | None = None
    ) -> None:
        """Create an error with a stable machine-readable code."""

        self.code = code
        self.runtime = runtime
        self.role = role
        detail = runtime if role is None else f'{runtime} does not support {role}'
        super().__init__(f'{code}: {detail}')


@dataclass(frozen=True, slots=True)
class RuntimeDefinition:
    """Declarative identity, capabilities, and adapters for one runtime."""

    identifier: str
    vendor: str
    module: str
    reviewer_adapter: str | None
    developer_adapter: str | None
    issue_reviewer_adapter: str | None
    manifest_placeholders: frozenset[str]
    reports_runtime_metadata: bool
    skill_home_environment: str
    skill_home_directory: str

    def adapter_path(self, role: RuntimeRole) -> str | None:
        """Return the declared adapter path for one role."""

        return {
            RuntimeRole.REVIEWER: self.reviewer_adapter,
            RuntimeRole.DEVELOPER: self.developer_adapter,
            RuntimeRole.ISSUE_REVIEWER: self.issue_reviewer_adapter,
        }[role]

    def supports(self, role: RuntimeRole) -> bool:
        """Return whether this runtime implements the requested role."""

        return self.adapter_path(role) is not None


class RuntimeRegistry:
    """Ordered immutable collection of supported runtime definitions."""

    def __init__(
        self,
        definitions: Iterable[RuntimeDefinition],
        *,
        default_identifier: str | None = None,
    ) -> None:
        """Validate and retain definitions in deterministic declaration order."""

        ordered = tuple(definitions)
        identifiers = [definition.identifier for definition in ordered]
        if not ordered or any(not identifier for identifier in identifiers):
            raise ValueError(RUNTIME_REGISTRY_EMPTY)
        if len(set(identifiers)) != len(identifiers):
            raise ValueError(RUNTIME_REGISTRY_DUPLICATE)
        selected_default = default_identifier or identifiers[0]
        if selected_default not in identifiers:
            raise ValueError(RUNTIME_DEFAULT_UNKNOWN)
        self._definitions = ordered
        self._by_identifier = {
            definition.identifier: definition for definition in ordered
        }
        self._default_identifier = selected_default

    def default(self, role: RuntimeRole | None = None) -> RuntimeDefinition:
        """Return the selected default, then the first role-capable definition."""

        preferred = self._by_identifier[self._default_identifier]
        if role is None or preferred.supports(role):
            return preferred
        for definition in self._definitions:
            if definition.supports(role):
                return definition
        raise RuntimeRegistryError(RUNTIME_ROLE_UNSUPPORTED, preferred.identifier, role)

    def identifiers(self, role: RuntimeRole | None = None) -> tuple[str, ...]:
        """Return registered identifiers, optionally restricted by capability."""

        return tuple(
            definition.identifier
            for definition in self._definitions
            if role is None or definition.supports(role)
        )

    def require(
        self, runtime: str, role: RuntimeRole | None = None
    ) -> RuntimeDefinition:
        """Resolve one runtime and fail with a stable capability error."""

        definition = self._by_identifier.get(runtime)
        if definition is None:
            raise RuntimeRegistryError(RUNTIME_UNKNOWN, runtime)
        if role is not None and not definition.supports(role):
            raise RuntimeRegistryError(RUNTIME_ROLE_UNSUPPORTED, runtime, role)
        return definition

    def adapter(
        self, runtime: str, role: RuntimeRole, model: str | None = None
    ) -> ReviewerAdapter | DeveloperAdapter | IssueReviewerAdapter:
        """Instantiate the adapter registered for one runtime role."""

        definition = self.require(runtime, role)
        adapter_path = definition.adapter_path(role)
        if adapter_path is None:  # pragma: no cover - guarded by require
            raise RuntimeRegistryError(RUNTIME_ROLE_UNSUPPORTED, runtime, role)
        try:
            module_name, class_name = adapter_path.rsplit('.', 1)
            adapter_class = getattr(importlib.import_module(module_name), class_name)
        except (AttributeError, ImportError, ValueError) as error:
            raise RuntimeRegistryError(
                RUNTIME_ADAPTER_INVALID, runtime, role
            ) from error
        expected = {
            RuntimeRole.REVIEWER: ReviewerAdapter,
            RuntimeRole.DEVELOPER: DeveloperAdapter,
            RuntimeRole.ISSUE_REVIEWER: IssueReviewerAdapter,
        }[role]
        if not isinstance(adapter_class, type) or not issubclass(
            adapter_class, expected
        ):
            raise RuntimeRegistryError(RUNTIME_ADAPTER_INVALID, runtime, role)
        factory = cast(
            'Callable[[str | None], ReviewerAdapter | DeveloperAdapter | IssueReviewerAdapter]',
            adapter_class,
        )
        return factory(model)


DEFAULT_RUNTIME_REGISTRY = RuntimeRegistry(
    (
        RuntimeDefinition(
            identifier='codex',
            vendor='openai',
            module='agent_orchestra.adapter.codex',
            reviewer_adapter='agent_orchestra.adapter.codex.CodexReviewerAdapter',
            developer_adapter='agent_orchestra.adapter.codex.CodexDeveloperAdapter',
            issue_reviewer_adapter=(
                'agent_orchestra.adapter.codex.CodexIssueReviewerAdapter'
            ),
            manifest_placeholders=frozenset({'cwd', 'schema', 'result'}),
            reports_runtime_metadata=True,
            skill_home_environment='CODEX_HOME',
            skill_home_directory='.codex',
        ),
        RuntimeDefinition(
            identifier='claude-code',
            vendor='anthropic',
            module='agent_orchestra.adapter.claude_code',
            reviewer_adapter=(
                'agent_orchestra.adapter.claude_code.ClaudeCodeReviewerAdapter'
            ),
            developer_adapter=(
                'agent_orchestra.adapter.claude_code.ClaudeCodeDeveloperAdapter'
            ),
            issue_reviewer_adapter=(
                'agent_orchestra.adapter.claude_code.ClaudeCodeIssueReviewerAdapter'
            ),
            manifest_placeholders=frozenset({'settings', 'schema'}),
            reports_runtime_metadata=True,
            skill_home_environment='CLAUDE_CONFIG_DIR',
            skill_home_directory='.claude',
        ),
    ),
    default_identifier='codex',
)
