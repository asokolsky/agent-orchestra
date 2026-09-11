"""Exchange machine-readable runtime provenance with built-in adapters."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agent_orchestra.invocations import EffectiveModelStatus, InvocationIdentity

if TYPE_CHECKING:
    from agent_orchestra.adapter.registry import RuntimeRegistry

RUNTIME_METADATA_ENV = 'AGENT_ORCHESTRA_RUNTIME_METADATA_PATH'


class RuntimeMetadataError(OSError):
    """Raised when runtime provenance is malformed."""


@dataclass(frozen=True, slots=True)
class RuntimeMetadata:
    """What one built-in adapter reports about the process it bounded."""

    effective_models: tuple[str, ...] = ()
    effective_model_status: EffectiveModelStatus = EffectiveModelStatus.UNAVAILABLE
    timed_out: bool = False


def child_process_environment(**overrides: str) -> dict[str, str]:
    """Return an environment that hides the adapter-only metadata channel."""

    environment = {**os.environ, **overrides}
    environment.pop(RUNTIME_METADATA_ENV, None)
    return environment


def reviewer_process_environment(
    temporary_directory: Path, **overrides: str
) -> dict[str, str]:
    """Confine reviewer validation caches and temporary files outside its worktree."""

    temporary = temporary_directory.resolve()
    values = {
        'TMPDIR': str(temporary),
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTEST_ADDOPTS': '-p no:cacheprovider',
        'MISE_CACHE_DIR': str(temporary / 'mise-cache'),
        'MISE_STATE_DIR': str(temporary / 'mise-state'),
        'MYPY_CACHE_DIR': str(temporary / 'mypy-cache'),
        'RUFF_CACHE_DIR': str(temporary / 'ruff-cache'),
        'UV_CACHE_DIR': str(temporary / 'uv-cache'),
        'XDG_CACHE_HOME': str(temporary / 'cache'),
    }
    values.update(overrides)
    return child_process_environment(**values)


def write_runtime_metadata(models: tuple[str, ...], *, timed_out: bool = False) -> None:
    """Write effective model identities and bounding outcome to the given path."""

    value = os.environ.get(RUNTIME_METADATA_ENV)
    if value is None:
        return
    path = Path(value)
    document = {
        'schema_version': 2,
        'effective_models': list(dict.fromkeys(models)),
        'status': (
            EffectiveModelStatus.REPORTED
            if models
            else EffectiveModelStatus.UNAVAILABLE
        ).value,
        'timed_out': timed_out,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid4()}.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as file:
            json.dump(document, file, indent=2)
            file.write('\n')
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_runtime_metadata(path: Path) -> RuntimeMetadata:
    """Read and remove one validated adapter metadata exchange file."""

    try:
        document: Any = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeMetadataError(f'invalid runtime metadata: {error}') from error
    finally:
        path.unlink(missing_ok=True)
    if not isinstance(document, dict) or set(document) != {
        'schema_version',
        'effective_models',
        'status',
        'timed_out',
    }:
        message = 'invalid runtime metadata fields'
        raise RuntimeMetadataError(message)
    models = document['effective_models']
    status = document['status']
    timed_out = document['timed_out']
    if (
        document['schema_version'] != 2
        or not isinstance(models, list)
        or not all(isinstance(model, str) and model for model in models)
        or len(models) != len(set(models))
        or status not in EffectiveModelStatus.values()
        or (status == EffectiveModelStatus.REPORTED) != bool(models)
        or type(timed_out) is not bool
    ):
        message = 'invalid runtime metadata values'
        raise RuntimeMetadataError(message)
    return RuntimeMetadata(
        effective_models=tuple(models),
        effective_model_status=EffectiveModelStatus(status),
        timed_out=timed_out,
    )


def exception_runtime_metadata(error: BaseException) -> RuntimeMetadata:
    """Return validated provenance preserved by a failed command adapter."""

    models = getattr(error, 'effective_models', ())
    status = getattr(error, 'effective_model_status', 'unavailable')
    timed_out = getattr(error, 'timed_out', False)
    if type(timed_out) is not bool:
        timed_out = False
    if (
        isinstance(models, tuple)
        and all(isinstance(model, str) and model for model in models)
        and len(models) == len(set(models))
        and status in EffectiveModelStatus.values()
        and (status == EffectiveModelStatus.REPORTED) == bool(models)
    ):
        return RuntimeMetadata(
            effective_models=tuple(models),
            effective_model_status=EffectiveModelStatus(status),
            timed_out=timed_out,
        )
    return RuntimeMetadata(timed_out=timed_out)


def runtime_metadata_path(
    identity: InvocationIdentity,
    path: Path,
    registry: RuntimeRegistry,
) -> Path | None:
    """Return the sidecar path only for runtimes that report provenance."""

    try:
        runtime = registry.require(identity.runtime)
    except ValueError:
        return None
    return path if runtime.reports_runtime_metadata else None
