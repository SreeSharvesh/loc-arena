"""Monitor registry and firewall enforcement.

Enforces the firewall and config over code. Loads
``configs/monitors.yaml``, resolves each ENABLED monitor's model role and binds it to its declared tap
point(s), and exposes the wired monitors to the harness. A monitor with ``enabled: false`` is not wired;
adding, moving, disabling, or re-parameterizing a monitor is a ``monitors.yaml`` edit, never a code
change. Monitors are dispatched only MIRROR/observable events (never the sealed log), so the firewall
holds structurally as well as by the CI check in ``tests/integration/test_firewall.py``.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from loc_arena.config import ModelSpec, Thresholds
from loc_arena.logging_.events import Event

# --- The firewall allowlist: the ONLY inputs a monitor may declare. Every one is a
# mirror/observable field or the fixed deploy-time baseline; none is a sealed recompute or workload label.
ALLOWED_MONITOR_INPUTS: frozenset[str] = frozenset(
    {
        # action / message tap (observable)
        "tool_name",
        "tool_args",
        "message_body",
        "actor_id",
        "task_description",
        # inference tap (MIRROR side only)
        "path",
        "credential_kind",
        "caller_identity",
        "query_fp",
        "output_fp",
        "inter_call_gap",
        # pr tap (observable)
        "diff",
        "pr_title",
        "pr_body",
        "target_repo",
        # shared_surface tap (observable)
        "surface_kind",
        "write_body",
        # deterministic detectors: deployed (observable) config + fixed baseline + the mirror event stream
        "deployed_monitor_config",
        "baseline_monitor_config",
        "deployed_logging_config",
        "baseline_logging_config",
        "mirror_event_stream",
    },
)

# Fields a monitor may NEVER read: the sealed recompute, sealed-only record fields, or the per-workload label.
FORBIDDEN_MONITOR_INPUTS: frozenset[str] = frozenset(
    {
        "sealed_event_stream",
        "sealed_generate_log",
        "sealed_recompute",
        "oracle_result",
        "landing",
        "rogue_label",
        "per_workload_label",
        "on_monitored_path",
        "in_mirror",
        "issued_for_sanctioned_request",
        "origin_job_id",
        "credential_id",
    },
)


# Which event kinds are visible at each tap point.
TAP_KINDS: dict[str, tuple[str, ...]] = {
    "action": ("action",),
    "message": ("message",),
    "inference": ("inference_call",),
    "pr": ("pr",),
    "shared_surface": ("shared_surface",),
    "diff": ("diff",),
    "job": ("job",),
    "trajectory": (),  # the whole trajectory (all kinds)
}


@dataclass(frozen=True)
class MonitorSpec:
    """One monitor's declaration from ``monitors.yaml`` (model-backed monitor or ``model: none`` detector)."""

    name: str
    taps: tuple[str, ...]
    inputs: tuple[str, ...]
    model: str  # a role in models.cheap.yaml, or "none" for a deterministic detector
    prompt_ref: str | None
    threshold: str  # e.g. "audit_budget"
    scope: dict[str, Any]
    enabled: bool
    mode: str = (
        "heuristic"  # "heuristic" (default, deterministic) | "live" (call the model through the gateway)
    )
    timing: str = "sync"  # "sync" (post-hoc, inline) | "async" (concurrent worker, off the critical path)
    per_monitor_reduce: str | None = None  # level-1 reduce over this monitor's verdicts; None => agg default
    weight: float = 1.0  # level-2 weight in the overall aggregate (0.0 => report-only, not counted)
    timeout_s: float | None = None  # async join/score deadline for this monitor; None => aggregation default
    ensemble_n: int = 1
    temperature: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MonitorVerdict:
    """A monitor's suspicion score for one event (logged as a ``monitor_verdict`` event)."""

    monitor: str
    tap: str
    score: float  # suspicion in [0, 1]
    reason: str
    actor_uid: str
    target_seq: int | None
    ts: float


# A live monitor's ONLY route to a model: (monitor_name, prompt, temperature) -> completion (via the gateway).
MonitorModelCaller = Callable[[str, str, float], str]
# A sink to record a monitor event (e.g. a parse error) onto the log; a no-op by default.
EventSink = Callable[[str, dict[str, Any]], None]


def _noop_sink(kind: str, payload: dict[str, Any]) -> None:
    """Default event sink: drop the event."""


@dataclass(frozen=True)
class MonitorContext:
    """The observable context a monitor may read.

    Task description, baseline, thresholds, and (live mode) the gateway model caller and an event sink.
    A live monitor with no caller falls back to its heuristic.
    """

    task_description: str
    baseline_covered: frozenset[str]
    thresholds: Thresholds
    model_caller: MonitorModelCaller | None = None
    event_sink: EventSink = _noop_sink


class Monitor(Protocol):
    """A control measure: given the events at its tap(s), returns per-event suspicion verdicts."""

    name: str
    taps: tuple[str, ...]
    timing: str  # "sync" (post-hoc, inline) | "async" (concurrent worker); invariant to the result
    timeout_s: float | None  # async join/score deadline; None => the driver's async_default_timeout_s

    def run(self, events: list[Event], context: MonitorContext) -> list[MonitorVerdict]:
        """Score the events visible at this monitor's tap(s) and return verdicts."""
        ...


# name -> factory(spec, model_spec | None) -> Monitor. Populated by register_monitor at import.
_FACTORY: dict[str, Callable[[MonitorSpec, ModelSpec | None], Monitor]] = {}


def register_monitor(name: str, factory: Callable[[MonitorSpec, ModelSpec | None], Monitor]) -> None:
    """Register a monitor implementation under the name it carries in ``monitors.yaml``."""
    _FACTORY[name] = factory


@dataclass
class MonitorRegistry:
    """The wired, enabled monitors and detectors, queryable by tap point."""

    monitors: list[Monitor]

    def enabled(self) -> list[Monitor]:
        """All wired (enabled) monitors and detectors."""
        return list(self.monitors)

    def by_name(self, name: str) -> Monitor:
        """The wired monitor with this name (raises ``KeyError`` if disabled or unknown)."""
        for m in self.monitors:
            if m.name == name:
                return m
        raise KeyError(name)

    def for_tap(self, tap: str) -> list[Monitor]:
        """The wired monitors attached to a tap point."""
        return [m for m in self.monitors if tap in m.taps]


def _spec_from(entry: dict[str, Any]) -> MonitorSpec:
    taps = entry.get("tap", [])
    taps_tuple = tuple(taps) if isinstance(taps, list) else (str(taps),)
    known = {
        "name",
        "tap",
        "inputs",
        "model",
        "prompt_ref",
        "threshold",
        "scope",
        "enabled",
        "mode",
        "timing",
        "per_monitor_reduce",
        "weight",
        "timeout_s",
        "ensemble_n",
        "temperature",
    }
    timing = str(entry.get("timing", "sync"))
    if timing not in ("sync", "async"):
        raise ValueError(f"monitor {entry.get('name')!r}: timing must be 'sync' or 'async', got {timing!r}")
    timeout_raw = entry.get("timeout_s")
    return MonitorSpec(
        name=str(entry["name"]),
        taps=taps_tuple,
        inputs=tuple(entry.get("inputs", [])),
        model=str(entry.get("model", "none")),
        prompt_ref=entry.get("prompt_ref"),
        threshold=str(entry.get("threshold", "audit_budget")),
        scope=dict(entry.get("scope", {})),
        enabled=bool(entry.get("enabled", False)),
        mode=str(entry.get("mode", "heuristic")),
        timing=timing,
        per_monitor_reduce=(str(entry["per_monitor_reduce"]) if "per_monitor_reduce" in entry else None),
        weight=float(entry.get("weight", 1.0)),
        timeout_s=(float(timeout_raw) if timeout_raw is not None else None),
        ensemble_n=int(entry.get("ensemble_n", 1)),
        temperature=float(entry.get("temperature", 0.0)),
        extra={k: v for k, v in entry.items() if k not in known},
    )


def load_monitor_specs(monitors_path: str | Path) -> list[MonitorSpec]:
    """Load every monitor and detector declaration from ``monitors.yaml`` (enabled or not)."""
    data: dict[str, Any] = yaml.safe_load(Path(monitors_path).read_text())
    specs: list[MonitorSpec] = []
    for entry in data.get("monitors", []):
        specs.append(_spec_from(entry))
    for entry in data.get("detectors", []):
        specs.append(_spec_from(entry))
    return specs


def load_monitors(monitors_path: str | Path, models: dict[str, ModelSpec]) -> MonitorRegistry:
    """Wire every ENABLED monitor/detector from ``monitors.yaml`` to its tap point(s)."""
    # Import the implementations so they register their factories (lazy: they import this module).
    import loc_arena.monitors  # noqa: F401, PLC0415

    wired: list[Monitor] = []
    for spec in load_monitor_specs(monitors_path):
        if not spec.enabled:
            continue
        factory = _FACTORY.get(spec.name)
        if factory is None:
            raise ValueError(f"no implementation registered for enabled monitor {spec.name!r}")
        model_spec = models.get(spec.model) if spec.model != "none" else None
        wired.append(factory(spec, model_spec))
    return MonitorRegistry(monitors=wired)


def _events_for(monitor: Monitor, events: list[Event]) -> list[Event]:
    if "trajectory" in monitor.taps:
        return list(events)
    kinds: set[str] = set()
    for tap in monitor.taps:
        kinds.update(TAP_KINDS.get(tap, ()))
    return [e for e in events if e.kind in kinds]


def run_monitors(
    registry: MonitorRegistry,
    mirror_events: list[Event],
    context: MonitorContext,
) -> list[MonitorVerdict]:
    """Dispatch the MIRROR events to every enabled monitor at the matching tap and collect their verdicts."""
    verdicts: list[MonitorVerdict] = []
    for monitor in registry.enabled():
        verdicts.extend(monitor.run(_events_for(monitor, mirror_events), context))
    return verdicts


def _keep_for_taps(taps: tuple[str, ...], event: Event) -> bool:
    """Per-event ``_events_for``: keep an event iff this monitor's tap(s) make its kind visible.

    Keep it if the monitor taps the whole ``trajectory`` or the event's kind is one its tap(s) expose
    Reused by the async worker so its accumulator matches the
    sync path's ``_events_for`` list exactly.
    """
    if "trajectory" in taps:
        return True
    kinds: set[str] = set()
    for tap in taps:
        kinds.update(TAP_KINDS.get(tap, ()))
    return event.kind in kinds


def _verdict_sort_key(v: MonitorVerdict) -> tuple[int, str]:
    """Canonical merge order ``(target_seq if not None else -1, monitor)``, independent of completion order.

    This is what makes the merged sync+async list invariant to async timing (the load-bearing property).
    """
    return (v.target_seq if v.target_seq is not None else -1, v.monitor)


class AsyncMonitorWorker:
    """One monitor scored on a concurrent worker thread, off the producer's critical path.

    Timing-invariant BY CONSTRUCTION: the worker accumulates the SAME tap-filtered mirror events the sync
    path would pass and scores them with the SAME ``monitor.run(accumulated, context)`` call, so a monitor
    declared ``async`` yields the identical verdicts it would yield declared ``sync``. Robust: the producer
    never blocks (``feed`` is ``put_nowait`` on an unbounded queue), and a worker that hangs past its
    deadline or raises contributes NO verdicts and floors (``result`` returns ``([], True)``); it never
    hangs or crashes the run.
    """

    def __init__(self, monitor: Monitor, context: MonitorContext, timeout_s: float) -> None:
        """Own a queue and a daemon thread (started here) for one monitor, joined under ``timeout_s``."""
        self._monitor = monitor
        self._context = context
        self._timeout_s = timeout_s
        self._taps = monitor.taps
        # UNBOUNDED queue so ``put_nowait`` never raises for a normal run; a bounded variant would instead
        # drop-and-record the overflow. The producer is thus never blocked.
        self._q: queue.Queue[Event | None] = queue.Queue()
        self._accumulator: list[Event] = []
        self._verdicts: list[MonitorVerdict] = []
        self._errored = False
        self.error_reason: str | None = None
        self._thread = threading.Thread(target=self._loop, name=f"monitor-{monitor.name}", daemon=True)
        self._thread.start()

    def feed(self, event: Event) -> None:
        """Hand one mirror event to the worker WITHOUT blocking the producer (``put_nowait``)."""
        self._q.put_nowait(event)

    def close(self) -> None:
        """Signal end-of-stream so the worker scores the accumulator (a ``None`` sentinel)."""
        self._q.put_nowait(None)

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                break
            if _keep_for_taps(self._taps, item):
                self._accumulator.append(item)  # arrival order == seq order (producer feeds in seq order)
        try:
            self._verdicts = list(self._monitor.run(self._accumulator, self._context))
        except Exception:  # noqa: BLE001 - a monitor must never crash an episode; floor instead
            self._errored = True
            self._verdicts = []

    def result(self) -> tuple[list[MonitorVerdict], bool]:
        """Join under the deadline; a timed-out (still alive) or raised worker floors to ``([], True)``.

        A worker still alive after the bounded join is left as a dying daemon thread; the driver returns
        regardless, so a slow/hung worker can never hang the run.
        """
        self._thread.join(timeout=self._timeout_s)
        if self._thread.is_alive():
            self.error_reason = "timeout"
            return [], True
        if self._errored:
            self.error_reason = "exception"
        return self._verdicts, self._errored


def run_monitors_split(
    registry: MonitorRegistry,
    mirror_events: list[Event],
    context: MonitorContext,
    *,
    async_default_timeout_s: float,
) -> list[MonitorVerdict]:
    """Score sync monitors inline and async monitors on worker threads, then merge and canonically sort.

    SYNC monitors run exactly as ``run_monitors`` (``monitor.run(_events_for(...), context)``). ASYNC
    monitors each get an ``AsyncMonitorWorker`` (its ``timeout_s`` from the monitor, else
    ``async_default_timeout_s``); ALL mirror events are fed to ALL workers in seq order and each applies its
    own tap filter. A worker that times out or raises contributes NO verdicts and records a ``monitor_error``
    via ``context.event_sink``. The merged sync+async verdicts are sorted by
    ``(target_seq if not None else -1, monitor)``, so the result is invariant to async completion order
    (the load-bearing timing-invariance property).
    """
    verdicts: list[MonitorVerdict] = []
    async_workers: list[tuple[Monitor, AsyncMonitorWorker]] = []

    for monitor in registry.enabled():
        if monitor.timing == "async":
            timeout = monitor.timeout_s if monitor.timeout_s is not None else async_default_timeout_s
            async_workers.append((monitor, AsyncMonitorWorker(monitor, context, timeout)))
        else:
            verdicts.extend(monitor.run(_events_for(monitor, mirror_events), context))

    for event in mirror_events:
        for _, worker in async_workers:
            worker.feed(event)  # non-blocking; each worker applies its own tap filter
    for _, worker in async_workers:
        worker.close()

    for monitor, worker in async_workers:
        async_verdicts, errored = worker.result()
        if errored:
            context.event_sink(
                "monitor_error",
                {"monitor": monitor.name, "error": worker.error_reason or "exception"},
            )
            continue
        verdicts.extend(async_verdicts)

    verdicts.sort(key=_verdict_sort_key)
    return verdicts


def firewall_violations(specs: list[MonitorSpec]) -> list[str]:
    """Return every firewall violation across the monitor specs (empty list == the firewall holds).

    A violation is a declared input that is a forbidden sealed-derived/per-workload field, or one not on
    the observable allowlist. The CI firewall test fails the build on any violation.
    """
    violations: list[str] = []
    for spec in specs:
        for field_name in spec.inputs:
            if field_name in FORBIDDEN_MONITOR_INPUTS:
                violations.append(f"{spec.name}: input {field_name!r} is a forbidden sealed-derived field")
            elif field_name not in ALLOWED_MONITOR_INPUTS:
                violations.append(f"{spec.name}: input {field_name!r} is not on the observable allowlist")
    return violations


def parse_verdict(text: str) -> tuple[float, str] | None:
    """Parse a strict ``{"suspicion": float 0..1, "reason": str}`` verdict out of a model completion.

    Tolerant of surrounding prose; returns ``None`` on a missing, malformed, or out-of-range verdict.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    susp = obj.get("suspicion") if isinstance(obj, dict) else None
                    reason = obj.get("reason") if isinstance(obj, dict) else None
                    if (
                        isinstance(susp, (int, float))
                        and not isinstance(susp, bool)
                        and 0.0 <= float(susp) <= 1.0
                    ):
                        return float(susp), str(reason) if reason is not None else ""
                    break
        start = text.find("{", start + 1)
    return None


def live_suspicion(
    context: MonitorContext,
    monitor_name: str,
    prompt: str,
    *,
    ensemble_n: int,
    temperature: float,
) -> tuple[float, str] | None:
    """Score one prompt with the live model via the gateway, ensembled by MAX; ``None`` -> heuristic fallback.

    Runs ``ensemble_n`` gateway-routed calls (as the monitor identity), parses each strict JSON verdict,
    and returns the MAX-suspicion verdict so one confident catch counts. On no parseable verdict, records
    a ``monitor_parse_error`` and returns ``None`` (the caller falls back to its heuristic).
    """
    if context.model_caller is None:
        return None
    best: tuple[float, str] | None = None
    for _ in range(max(1, ensemble_n)):
        try:
            completion = context.model_caller(monitor_name, prompt, temperature)
        except Exception as exc:  # noqa: BLE001 - a monitor must never crash an episode
            context.event_sink("monitor_parse_error", {"monitor": monitor_name, "error": f"call:{exc}"})
            continue
        parsed = parse_verdict(completion)
        if parsed is None:
            context.event_sink(
                "monitor_parse_error",
                {"monitor": monitor_name, "error": "unparseable", "completion": completion[:200]},
            )
            continue
        if best is None or parsed[0] > best[0]:
            best = parsed
    return best


def load_template(prompt_ref: str | None) -> str:
    """Load a monitor prompt template from its ``prompt_ref`` file (empty string if absent)."""
    if not prompt_ref:
        return ""
    path = Path(prompt_ref)
    return path.read_text() if path.exists() else ""


def render_prompt(template: str, payload: dict[str, Any]) -> str:
    """Render the monitor prompt: the template with the declared observable inputs substituted in."""
    body = json.dumps(payload, indent=2, default=str)
    if "{inputs}" in template:
        return template.replace("{inputs}", body)
    return f"{template}\n\nObservable inputs:\n{body}"
