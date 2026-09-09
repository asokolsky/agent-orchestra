"""Validated XDG-aware global settings for Agent Orchestra."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from agent_orchestra.adapter.registry import (
    DEFAULT_RUNTIME_REGISTRY,
    RuntimeRegistry,
    RuntimeRegistryError,
    RuntimeRole,
)


class SettingsError(ValueError):
    """Raised when the global settings file is invalid."""


@dataclass(frozen=True, slots=True)
class Setting:
    """One effective setting and the source that supplied it."""

    value: Path | int
    source: str


@dataclass(frozen=True, slots=True)
class ReviewerMember:
    """One required reviewer in an ordered configured reviewer set."""

    identifier: str
    runtime: str
    vendor: str
    model: str | None


@dataclass(frozen=True, slots=True)
class ReviewerSet:
    """An ordered named set of reviewers that must all complete."""

    identifier: str
    members: tuple[ReviewerMember, ...]


@dataclass(frozen=True, slots=True)
class Settings:
    """Effective storage and retention settings."""

    path: Path
    database: Setting
    runs_directory: Setting
    job_evidence_days: Setting
    reviewer_sets: tuple[ReviewerSet, ...]


def config_path() -> Path:
    """Return the XDG-aware global settings path."""

    base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return base.expanduser() / 'agent-orchestra/config.toml'


def _path_value(value: object, field: str) -> Path:
    """Validate and expand one configured filesystem path."""

    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f'{field} must be a non-empty string')
    return Path(value).expanduser()


def _reviewer_sets(value: object, registry: RuntimeRegistry) -> tuple[ReviewerSet, ...]:
    """Validate ordered reviewer sets against the runtime registry."""

    if not isinstance(value, dict):
        message = 'reviewer_sets must be a table'
        raise SettingsError(message)
    if not value:
        message = 'reviewer_sets must contain at least one set'
        raise SettingsError(message)
    reviewer_sets: list[ReviewerSet] = []
    for set_id, configured in value.items():
        if not isinstance(set_id, str) or not re.fullmatch(
            r'[a-z0-9][a-z0-9_-]*', set_id
        ):
            raise SettingsError(f'invalid reviewer set ID: {set_id!r}')
        if not isinstance(configured, dict):
            raise SettingsError(f'reviewer_sets.{set_id} must be a table')
        if 'members' not in configured:
            raise SettingsError(f'reviewer_sets.{set_id} must contain members')
        unknown_fields = set(configured) - {'members'}
        if unknown_fields:
            unknown = ', '.join(sorted(unknown_fields))
            raise SettingsError(
                f'reviewer_sets.{set_id} contains unknown fields: {unknown}'
            )
        members = configured['members']
        if not isinstance(members, list) or len(members) < 2:
            raise SettingsError(
                f'reviewer_sets.{set_id}.members must contain at least two reviewers'
            )
        resolved: list[ReviewerMember] = []
        member_ids: set[str] = set()
        for index, member in enumerate(members):
            field = f'reviewer_sets.{set_id}.members[{index}]'
            if not isinstance(member, dict) or set(member) - {
                'id',
                'runtime',
                'model',
                'required',
            }:
                raise SettingsError(f'{field} contains missing or unknown fields')
            member_id = member.get('id')
            runtime = member.get('runtime')
            model = member.get('model')
            required = member.get('required', True)
            if not isinstance(member_id, str) or not re.fullmatch(
                r'[a-z0-9][a-z0-9_-]*', member_id
            ):
                raise SettingsError(f'{field}.id is invalid')
            if member_id in member_ids:
                raise SettingsError(f'duplicate reviewer ID in {set_id}: {member_id}')
            if not isinstance(runtime, str):
                raise SettingsError(f'{field}.runtime must be a string')
            if model is not None and (not isinstance(model, str) or not model.strip()):
                raise SettingsError(f'{field}.model must be a non-empty string')
            if required is not True:
                raise SettingsError(f'{field}.required must be true')
            try:
                definition = registry.require(runtime, RuntimeRole.REVIEWER)
            except RuntimeRegistryError as error:
                raise SettingsError(f'{field}.runtime: {error}') from error
            member_ids.add(member_id)
            resolved.append(
                ReviewerMember(member_id, runtime, definition.vendor, model)
            )
        reviewer_sets.append(ReviewerSet(set_id, tuple(resolved)))
    return tuple(reviewer_sets)


def load_settings(
    path: Path | None = None,
    *,
    default_database: Path | None = None,
    default_runs_directory: Path | None = None,
    runtime_registry: RuntimeRegistry = DEFAULT_RUNTIME_REGISTRY,
) -> Settings:
    """Load validated settings, falling back to built-in defaults."""

    selected = (path or config_path()).expanduser()
    database = Setting(
        default_database or Path.home() / '.local/state/agent-orchestra/state.db',
        'built_in',
    )
    runs = Setting(
        default_runs_directory or Path.home() / '.local/state/agent-orchestra/runs',
        'built_in',
    )
    days = Setting(90, 'built_in')
    reviewer_sets: tuple[ReviewerSet, ...] = ()
    if not selected.exists():
        return Settings(selected, database, runs, days, reviewer_sets)
    if not selected.is_file() or selected.is_symlink():
        raise SettingsError(f'settings path is not a regular file: {selected}')
    try:
        document = tomllib.loads(selected.read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SettingsError(f'invalid settings file {selected}: {error}') from error
    if not isinstance(document, dict) or set(document) - {
        'storage',
        'retention',
        'reviewer_sets',
    }:
        message = 'settings contain unknown top-level fields'
        raise SettingsError(message)
    storage = document.get('storage', {})
    retention = document.get('retention', {})
    if not isinstance(storage, dict) or set(storage) - {'database', 'runs_directory'}:
        message = 'storage settings contain unknown fields'
        raise SettingsError(message)
    if not isinstance(retention, dict) or set(retention) - {'job_evidence_days'}:
        message = 'retention settings contain unknown fields'
        raise SettingsError(message)
    if 'database' in storage:
        database = Setting(_path_value(storage['database'], 'storage.database'), 'file')
    if 'runs_directory' in storage:
        runs = Setting(
            _path_value(storage['runs_directory'], 'storage.runs_directory'), 'file'
        )
    if 'job_evidence_days' in retention:
        value = retention['job_evidence_days']
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            message = 'retention.job_evidence_days must be a positive integer'
            raise SettingsError(message)
        days = Setting(value, 'file')
    if 'reviewer_sets' in document:
        reviewer_sets = _reviewer_sets(document['reviewer_sets'], runtime_registry)
    return Settings(selected, database, runs, days, reviewer_sets)
