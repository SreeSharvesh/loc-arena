"""The episode's simulated clock as the runner's scaffold keeps it; see docs/isolation/design.md#scaffold."""

from __future__ import annotations

from loc_arena.stack.contracts import GatewayControl


class SimulatedClock:
    """The runner's copy of the episode clock; a callable, so it serves wherever a clock is read."""

    def __init__(self, control: GatewayControl, start: float) -> None:
        """Start the clock at ``start`` on the core and here."""
        self._control = control
        self._now = float(start)
        control.set_clock(self._now)

    def __call__(self) -> float:
        """The current simulated time."""
        return self._now

    def set(self, now: float) -> None:
        """Move the clock to ``now``: on the core first, then here."""
        value = float(now)  # an int would be logged as 50 in process and 50.0 over HTTP
        self._control.set_clock(value)
        self._now = value
