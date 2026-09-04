"""Exchange machine-readable runtime provenance with built-in adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

RUNTIME_METADATA_ENV = 'AGENT_ORCHESTRA_RUNTIME_METADATA_PATH'


class RuntimeMetadataError(OSError):
    """Raised when runtime provenance is malformed."""


def child_process_environment(**overrides: str) -> dict[str, str]:
    """Return an environment that hides the adapter-only metadata channel."""

    environment = {**os.environ, **overrides}
    environment.pop(RUNTIME_METADATA_ENV, None)
    return environment


def write_runtime_metadata(models: tuple[str, ...]) -> None:
    """Write effective model identities to the orchestrator-provided path."""

    value = os.environ.get(RUNTIME_METADATA_ENV)
    if value is None:
        return
    path = Path(value)
    document = {
        'schema_version': 1,
        'effective_models': list(dict.fromkeys(models)),
        'status': 'reported' if models else 'unavailable',
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


def read_runtime_metadata(
    path: Path,
) -> tuple[tuple[str, ...], Literal['reported', 'unavailable']]:
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
    }:
        message = 'invalid runtime metadata fields'
        raise RuntimeMetadataError(message)
    models = document['effective_models']
    status = document['status']
    if (
        document['schema_version'] != 1
        or not isinstance(models, list)
        or not all(isinstance(model, str) and model for model in models)
        or len(models) != len(set(models))
        or status not in {'reported', 'unavailable'}
        or (status == 'reported') != bool(models)
    ):
        message = 'invalid runtime metadata values'
        raise RuntimeMetadataError(message)
    return tuple(models), cast('Literal["reported", "unavailable"]', status)
