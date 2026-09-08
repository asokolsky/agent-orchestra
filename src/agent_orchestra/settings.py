"""Validated XDG-aware global settings for Agent Orchestra."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


class SettingsError(ValueError):
    """Raised when the global settings file is invalid."""


@dataclass(frozen=True, slots=True)
class Setting:
    """One effective setting and the source that supplied it."""

    value: Path | int
    source: str


@dataclass(frozen=True, slots=True)
class Settings:
    """Effective storage and retention settings."""

    path: Path
    database: Setting
    runs_directory: Setting
    job_evidence_days: Setting


def config_path() -> Path:
    """Return the XDG-aware global settings path."""

    base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    return base.expanduser() / 'agent-orchestra/config.toml'


def _path_value(value: object, field: str) -> Path:
    """Validate and expand one configured filesystem path."""

    if not isinstance(value, str) or not value.strip():
        raise SettingsError(f'{field} must be a non-empty string')
    return Path(value).expanduser()


def load_settings(
    path: Path | None = None,
    *,
    default_database: Path | None = None,
    default_runs_directory: Path | None = None,
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
    if not selected.exists():
        return Settings(selected, database, runs, days)
    if not selected.is_file() or selected.is_symlink():
        raise SettingsError(f'settings path is not a regular file: {selected}')
    try:
        document = tomllib.loads(selected.read_text(encoding='utf-8'))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise SettingsError(f'invalid settings file {selected}: {error}') from error
    if not isinstance(document, dict) or set(document) - {'storage', 'retention'}:
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
    return Settings(selected, database, runs, days)
