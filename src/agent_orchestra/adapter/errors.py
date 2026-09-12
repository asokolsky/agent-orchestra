"""Shared failure type for built-in role adapters."""

from __future__ import annotations

from agent_orchestra.errors import AgentOrchestraError


class AdapterError(AgentOrchestraError):
    """Raised when a role adapter cannot produce its canonical response."""

    def __init__(self, *args: object, timed_out: bool = False) -> None:
        """Record the failure and whether the adapter's own bound expired."""

        # An adapter runs as its own process, so a caller sees only a non-zero
        # exit. This flag is what distinguishes "my child exceeded the bound I
        # gave it" from any other failure, and the adapter reports it back
        # through the runtime metadata sidecar.
        super().__init__(*args)
        self.timed_out = timed_out
