"""Define adapter-neutral runtime usage values."""

from __future__ import annotations

from dataclasses import dataclass

from agent_orchestra.persisted_enum import PersistedEnum


class UsageStatus(PersistedEnum):
    """Whether a runtime reported machine-readable usage for an attempt."""

    REPORTED = 'reported'
    UNAVAILABLE = 'unavailable'


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageValues:
    """Cross-runtime usage values for one explicitly identified scope."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    total_cost_usd: float | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelUsage:
    """Usage values reported for one effective model."""

    model: str
    values: UsageValues


@dataclass(frozen=True, slots=True, kw_only=True)
class RuntimeUsage:
    """Versioned usage reported by a runtime for one attempt."""

    schema_version: int = 1
    turn_count: int | None = None
    totals: UsageValues | None = None
    models: tuple[ModelUsage, ...] = ()
