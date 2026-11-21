"""How a live monitor calls its model: one prompt's verdict, and many events scored at once.

``MonitorContext.calls_in_flight`` is how many of its events a monitor scores at once; ``score_each`` keeps
their order, so a verdict list is the same however many run together.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

from loc_arena.logging_.events import Event
from loc_arena.monitors.registry import ModelCallsStoppedError, MonitorContext, ParsedVerdict, parse_verdict

_COMPLETION_EXCERPT_CHARACTERS = 200


def score_each[T](events: Sequence[Event], score: Callable[[Event], T], context: MonitorContext) -> list[T]:
    """``score`` of each event, in the events' order, with up to ``context.calls_in_flight`` running at once.

    A live score waits on its model, so a monitor over many events scores them together; the result is the
    same list it would be one by one.
    """
    if context.calls_in_flight == 1:
        return [score(event) for event in events]
    with ThreadPoolExecutor(context.calls_in_flight, thread_name_prefix="monitor-call") as pool:
        return list(pool.map(score, events))


def live_suspicion(
    context: MonitorContext,
    monitor_name: str,
    prompt: str,
    *,
    ensemble_n: int,
    temperature: float,
) -> tuple[float, str] | None:
    """Score one prompt with the live model via the gateway, ensembled by MAX; ``None`` -> heuristic fallback.

    Runs ``ensemble_n`` gateway-routed calls (as the monitor identity), parses each verdict, and returns
    the MAX-suspicion verdict so one confident catch counts. A verdict recovered from a reply that was not
    strict JSON records a ``monitor_parse_recovered`` and its reason carries ``RECOVERED_REASON_PREFIX``.
    On no parseable verdict, records a ``monitor_parse_error`` and returns ``None`` (the caller falls back
    to its heuristic).
    """
    if context.model_caller is None:
        return None
    best: ParsedVerdict | None = None
    for _ in range(max(1, ensemble_n)):
        try:
            completion = context.model_caller(monitor_name, prompt, temperature)
        except ModelCallsStoppedError:
            break
        except Exception as exc:  # noqa: BLE001 - a monitor must never crash an episode
            context.event_sink("monitor_parse_error", {"monitor": monitor_name, "error": f"call:{exc}"})
            continue
        parsed = parse_verdict(completion)
        excerpt = completion[:_COMPLETION_EXCERPT_CHARACTERS]
        if parsed is None:
            context.event_sink(
                "monitor_parse_error",
                {"monitor": monitor_name, "error": "unparseable", "completion": excerpt},
            )
            continue
        if parsed.recovered:
            context.event_sink("monitor_parse_recovered", {"monitor": monitor_name, "completion": excerpt})
        if best is None or parsed.suspicion > best.suspicion:
            best = parsed
    return None if best is None else (best.suspicion, best.reported_reason)
