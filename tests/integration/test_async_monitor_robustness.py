"""Robustness / non-blocking: an async worker never hangs, crashes, or blocks the run.

A worker that sleeps past its ``timeout_s`` floors (times out under the bounded join); a worker that raises
in ``run`` floors; both contribute ZERO verdicts and record a ``monitor_error`` via the event sink. The
driver returns under a bounded wall time (not the full sleep), the other monitors' verdicts are present and
correct, and ``feed`` never blocks the producer (``put_nowait`` on an unbounded queue).
"""

from __future__ import annotations

import time

from loc_arena.config import load_run_config
from loc_arena.logging_.events import Event
from loc_arena.monitors.registry import (
    AsyncMonitorWorker,
    MonitorContext,
    MonitorRegistry,
    MonitorVerdict,
    load_monitors,
    run_monitors_split,
)
from loc_arena.protocol import monitor_context

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


class _SlowMonitor:
    """An async monitor whose ``run`` sleeps far past its deadline (must floor, not hang the driver)."""

    name = "m_slow"
    taps: tuple[str, ...] = ("action",)
    timing = "async"
    timeout_s: float | None = 0.3

    def run(self, events: list[Event], context: MonitorContext) -> list[MonitorVerdict]:
        time.sleep(5.0)
        return [MonitorVerdict("m_slow", "action", 1.0, "too late to count", "x", 1, 1.0)]


class _RaisingMonitor:
    """An async monitor that raises in ``run`` (must floor with a recorded error, not crash the driver)."""

    name = "m_raise"
    taps: tuple[str, ...] = ("action",)
    timing = "async"
    timeout_s: float | None = 0.3

    def run(self, events: list[Event], context: MonitorContext) -> list[MonitorVerdict]:
        raise RuntimeError("boom")


def _action_events(n: int) -> list[Event]:
    return [
        Event(
            episode_id="ep-robust",
            seq=i,
            ts=float(i),
            actor_uid="agent-main",
            actor_role="untrusted",
            kind="action",
            tool="open_pr" if i == 0 else "submit_job",
            payload={"target": "monitor-policy" if i == 0 else "aurora", "args": {}},
        )
        for i in range(n)
    ]


def test_hung_and_raising_async_monitors_floor_without_hanging_the_run() -> None:
    recorded: list[tuple[str, dict[str, object]]] = []

    def sink(kind: str, payload: dict[str, object]) -> None:
        recorded.append((kind, payload))

    ctx = monitor_context(CFG, event_sink=sink)  # heuristic -> the real action_monitor is deterministic
    action_monitor = load_monitors("configs/monitors.yaml", CFG.models).by_name("action_monitor")
    action_monitor.timing = "sync"
    registry = MonitorRegistry([action_monitor, _SlowMonitor(), _RaisingMonitor()])
    events = _action_events(3)

    started = time.monotonic()
    verdicts = run_monitors_split(registry, events, ctx, async_default_timeout_s=20.0)
    elapsed = time.monotonic() - started

    # bounded wall time: ~ the (small) per-worker deadline, NOT the 5s sleep
    assert elapsed < 0.3 + 2.0

    # the hung and raising monitors contribute ZERO verdicts
    assert not [v for v in verdicts if v.monitor in ("m_slow", "m_raise")]
    # the healthy sync monitor's verdicts are present and correct (one per action event)
    action_verdicts = [v for v in verdicts if v.monitor == "action_monitor"]
    assert len(action_verdicts) == len(events)
    assert all(0.0 <= v.score <= 1.0 for v in action_verdicts)

    # a monitor_error is recorded for each floored monitor, with the precise reason
    errors = {p["monitor"]: p.get("error") for k, p in recorded if k == "monitor_error"}
    assert errors.get("m_slow") == "timeout"
    assert errors.get("m_raise") == "exception"


def test_feed_is_non_blocking_even_while_the_worker_is_busy() -> None:
    ctx = monitor_context(CFG)
    worker = AsyncMonitorWorker(_SlowMonitor(), ctx, timeout_s=0.1)
    events = _action_events(1) * 5000

    started = time.monotonic()
    for e in events:
        worker.feed(e)  # put_nowait on an unbounded queue: never blocks the producer
    elapsed = time.monotonic() - started
    worker.close()

    assert elapsed < 1.0  # feeding 5000 events is near-instant because the producer never blocks
