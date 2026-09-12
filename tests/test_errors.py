"""Package-wide exception hierarchy coverage."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import TYPE_CHECKING

import agent_orchestra
from agent_orchestra import AgentOrchestraError
from agent_orchestra.store import RunNotFoundError

if TYPE_CHECKING:
    from types import ModuleType


def _package_modules() -> tuple[ModuleType, ...]:
    """Import every package module so its declared exception classes are visible."""

    discovered = pkgutil.walk_packages(
        agent_orchestra.__path__, prefix=f'{agent_orchestra.__name__}.'
    )
    return (
        agent_orchestra,
        *(importlib.import_module(item.name) for item in discovered),
    )


def _declared_exception_classes() -> tuple[type[BaseException], ...]:
    """Return exception classes declared directly by package modules."""

    return tuple(
        member
        for module in _package_modules()
        for _, member in inspect.getmembers(module, inspect.isclass)
        if member.__module__ == module.__name__ and issubclass(member, BaseException)
    )


def test_every_declared_exception_uses_package_base() -> None:
    """Prevent new package exceptions from bypassing the shared hierarchy."""

    exceptions = _declared_exception_classes()

    assert exceptions
    assert all(issubclass(exception, AgentOrchestraError) for exception in exceptions)


def test_package_discovery_reaches_nested_modules() -> None:
    """Keep the hierarchy check representative of the complete package."""

    module_names = {module.__name__ for module in _package_modules()}

    assert 'agent_orchestra.store' in module_names
    assert 'agent_orchestra.adapter.errors' in module_names


def test_only_missing_records_retain_a_builtin_semantic_base() -> None:
    """Keep builtin capture deliberate instead of inherited by accident."""

    exceptions = _declared_exception_classes()

    assert issubclass(RunNotFoundError, LookupError)
    assert not any(issubclass(exception, ValueError) for exception in exceptions)
    assert not any(issubclass(exception, RuntimeError) for exception in exceptions)
    assert not any(issubclass(exception, OSError) for exception in exceptions)
