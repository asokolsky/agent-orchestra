"""
One base for enum values that are written to evidence and read back.

A persisted enum value cannot be widened with a bare constructor: `EnumT(value)`
raises `ValueError` for anything this build does not recognize, which turns an
unknown persisted value into an unhandled crash rather than a reported one.
`decode` performs that widening through a caller-supplied failure function, so
each subsystem keeps its own stable domain error.

`values` exists because a validator set must never be a second, hand-copied
spelling of the members. It is generated here, so adding a member is one edit.

There is deliberately no generated `Literal` alias. A `Literal` built from
members is correct at runtime but invisible to a type checker, which requires
the members spelled out statically and rejects a computed alias with "Variable
is not valid as a type". Fields therefore carry the enum itself and are decoded
at their read boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Never


class PersistedEnum(StrEnum):
    """A string enum whose members are durable evidence values."""

    @classmethod
    def values(cls) -> frozenset[str]:
        """Return every legal persisted value for this enum."""

        return frozenset(member.value for member in cls)

    @classmethod
    def decode(cls, value: object, *, fail: Callable[[str], Never]) -> Self:
        """Widen one persisted value, failing closed through the caller."""

        if not isinstance(value, str) or value not in cls.values():
            fail(f'invalid persisted {cls.__name__}: {value!r}')
        return cls(value)
