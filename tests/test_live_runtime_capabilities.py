"""Offline contract tests for the opt-in live runtime capability bench."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agent_orchestra.adapter.registry import DEFAULT_RUNTIME_REGISTRY, RuntimeRegistry
from tests.live.runtime_harness import (
    LIVE_RUNTIMES,
    LiveRuntime,
    RuntimeCapabilityError,
    assert_runtime_capabilities,
    live_runtime,
)


def test_every_registered_runtime_has_live_capability_expectations() -> None:
    """Require a live description whenever the default registry gains a runtime."""

    assert tuple(runtime.identifier for runtime in LIVE_RUNTIMES) == (
        DEFAULT_RUNTIME_REGISTRY.identifiers()
    )


@pytest.mark.parametrize('identifier', DEFAULT_RUNTIME_REGISTRY.identifiers())
def test_live_capability_expectations_match_packaged_contract(identifier: str) -> None:
    """Keep offline declarations aligned before any authenticated invocation."""

    runtime: LiveRuntime = live_runtime(identifier)
    assert_runtime_capabilities(runtime)


def test_incorrect_fake_declaration_names_the_exact_capability() -> None:
    """Report the mismatched registry claim instead of a generic scenario error."""

    expected = live_runtime('codex')
    definition = DEFAULT_RUNTIME_REGISTRY.require(expected.identifier)
    fake_registry = RuntimeRegistry(
        (replace(definition, reports_runtime_metadata=False),)
    )

    with pytest.raises(
        RuntimeCapabilityError,
        match=(
            'codex capability reports_runtime_metadata mismatch: '
            'expected True, observed False'
        ),
    ) as raised:
        assert_runtime_capabilities(expected, registry=fake_registry)

    assert raised.value.capability == 'reports_runtime_metadata'
