"""Exchange machine-readable runtime provenance with built-in adapters."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agent_orchestra.adapter.registry import RuntimeRegistryError
from agent_orchestra.errors import AgentOrchestraError
from agent_orchestra.invocations import (
    EffectiveModelStatus,
    InvocationIdentity,
    runtime_usage_from_document,
    usage_document,
)
from agent_orchestra.usage import RuntimeUsage, UsageStatus

if TYPE_CHECKING:
    from agent_orchestra.adapter.registry import RuntimeRegistry

RUNTIME_METADATA_ENV = 'AGENT_ORCHESTRA_RUNTIME_METADATA_PATH'
PROVIDER_EXECUTION_FAILED = 'provider_execution_failed'
PROVIDER_BUDGET_EXHAUSTED = 'provider_budget_exhausted'
STRUCTURED_OUTPUT_EXHAUSTED = 'structured_output_exhausted'
TURN_LIMIT_EXHAUSTED = 'turn_limit_exhausted'
RETRYABLE_REVIEW_FAILURES = frozenset(
    {
        PROVIDER_BUDGET_EXHAUSTED,
        STRUCTURED_OUTPUT_EXHAUSTED,
        TURN_LIMIT_EXHAUSTED,
    }
)


class RuntimeMetadataError(AgentOrchestraError):
    """Raised when runtime provenance is malformed."""


@dataclass(frozen=True, slots=True)
class RuntimeMetadata:
    """What one built-in adapter reports about the process it bounded."""

    effective_models: tuple[str, ...] = ()
    effective_model_status: EffectiveModelStatus = EffectiveModelStatus.UNAVAILABLE
    timed_out: bool = False
    failure_code: str | None = None
    failure_message: str | None = None
    usage_status: UsageStatus = UsageStatus.UNAVAILABLE
    usage: RuntimeUsage | None = None


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


def write_runtime_metadata(
    models: tuple[str, ...],
    *,
    timed_out: bool = False,
    failure_code: str | None = None,
    failure_message: str | None = None,
    usage: RuntimeUsage | None = None,
) -> None:
    """Write model identity and adapter failure metadata to the given path."""

    value = os.environ.get(RUNTIME_METADATA_ENV)
    if value is None:
        return
    path = Path(value)
    document = {
        'schema_version': 4,
        'effective_models': list(dict.fromkeys(models)),
        'status': (
            EffectiveModelStatus.REPORTED
            if models
            else EffectiveModelStatus.UNAVAILABLE
        ).value,
        'timed_out': timed_out,
        'failure_code': failure_code,
        'failure_message': failure_message,
        'usage_status': (
            UsageStatus.REPORTED if usage is not None else UsageStatus.UNAVAILABLE
        ).value,
        'usage': usage_document(usage) if usage is not None else None,
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
    if not isinstance(document, dict):
        message = 'invalid runtime metadata fields'
        raise RuntimeMetadataError(message)
    schema_version = document.get('schema_version')
    expected_fields = {
        'schema_version',
        'effective_models',
        'status',
        'timed_out',
    }
    if schema_version in {3, 4}:
        expected_fields.update({'failure_code', 'failure_message'})
    if schema_version == 4:
        expected_fields.update({'usage_status', 'usage'})
    if schema_version not in {2, 3, 4} or set(document) != expected_fields:
        message = 'invalid runtime metadata fields'
        raise RuntimeMetadataError(message)
    models = document['effective_models']
    status = document['status']
    timed_out = document['timed_out']
    failure_code = document.get('failure_code')
    failure_message = document.get('failure_message')
    usage_status = document.get('usage_status', UsageStatus.UNAVAILABLE)
    usage_value = document.get('usage')
    if (
        not isinstance(models, list)
        or not all(isinstance(model, str) and model for model in models)
        or len(models) != len(set(models))
        or status not in EffectiveModelStatus.values()
        or (status == EffectiveModelStatus.REPORTED) != bool(models)
        or type(timed_out) is not bool
        or (failure_code is None) != (failure_message is None)
        or (
            failure_code is not None
            and (not isinstance(failure_code, str) or not failure_code)
        )
        or (
            failure_message is not None
            and (not isinstance(failure_message, str) or not failure_message)
        )
        or usage_status not in UsageStatus.values()
        or (usage_status == UsageStatus.REPORTED) != (usage_value is not None)
    ):
        message = 'invalid runtime metadata values'
        raise RuntimeMetadataError(message)
    try:
        usage = (
            runtime_usage_from_document(usage_value)
            if usage_value is not None
            else None
        )
    except AgentOrchestraError as error:
        raise RuntimeMetadataError(
            f'invalid runtime metadata values: {error}'
        ) from error
    return RuntimeMetadata(
        effective_models=tuple(models),
        effective_model_status=EffectiveModelStatus(status),
        timed_out=timed_out,
        failure_code=failure_code,
        failure_message=failure_message,
        usage_status=UsageStatus(usage_status),
        usage=usage,
    )


def exception_runtime_metadata(error: BaseException) -> RuntimeMetadata:
    """Return validated provenance preserved by a failed command adapter."""

    models = getattr(error, 'effective_models', ())
    status = getattr(error, 'effective_model_status', 'unavailable')
    timed_out = getattr(error, 'timed_out', False)
    failure_code = getattr(error, 'failure_code', None)
    failure_message = getattr(error, 'failure_message', None)
    usage_status = getattr(error, 'usage_status', UsageStatus.UNAVAILABLE)
    usage = getattr(error, 'usage', None)
    if type(timed_out) is not bool:
        timed_out = False
    if (
        isinstance(models, tuple)
        and all(isinstance(model, str) and model for model in models)
        and len(models) == len(set(models))
        and status in EffectiveModelStatus.values()
        and (status == EffectiveModelStatus.REPORTED) == bool(models)
        and (failure_code is None) == (failure_message is None)
        and (failure_code is None or (isinstance(failure_code, str) and failure_code))
        and (
            failure_message is None
            or (isinstance(failure_message, str) and failure_message)
        )
        and usage_status in UsageStatus.values()
        and (usage_status == UsageStatus.REPORTED) == isinstance(usage, RuntimeUsage)
    ):
        return RuntimeMetadata(
            effective_models=tuple(models),
            effective_model_status=EffectiveModelStatus(status),
            timed_out=timed_out,
            failure_code=failure_code,
            failure_message=failure_message,
            usage_status=UsageStatus(usage_status),
            usage=usage,
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
    except RuntimeRegistryError:
        return None
    return path if runtime.reports_runtime_metadata else None
