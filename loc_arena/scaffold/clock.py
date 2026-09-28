"""The episode's simulated clock as the runner's scaffold keeps it.

The core keeps its own copy: it stamps its records and checks token expiry with it. So the scaffold never
moves time without the core: ``set`` sends the new time to the core first, synchronously, and only then
moves this copy, so every event either side writes afterwards carries the same time, in the order the
scaffold set it. Any value is accepted, earlier ones included (the scripted episode steps back in time).
"""

from __future__ import annotations

from loc_arena.stack.contracts import GatewayControl


class SimulatedClock:
    """The runner's copy of the episode clock; a callable, so it serves wherever a clock is read."""

    def __init__(self, control: GatewayControl, start: float) -> None:
        """Start the clock at ``start`` on the core and here."""
        self._control = control
        self._now = float(start)
        control.set_clock(self._now)

    @property
    def now(self) -> float:
        """The current simulated time."""
        return self._now

    def __call__(self) -> float:
        """The current simulated time."""
        return self._now

    def set(self, now: float) -> None:
        """Move the clock to ``now``: on the core first, then here.

        A float on both sides (an int would be written as ``50`` in process and ``50.0`` over HTTP).
        """
        value = float(now)
        self._control.set_clock(value)
        self._now = value
