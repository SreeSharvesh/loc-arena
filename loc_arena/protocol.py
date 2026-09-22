"""Control protocol under test.

Enforces the firewall (composes the ENABLED monitors, never the sealed verifier) and config over code
(the audit budget comes from the run config). Composes the wired monitors from the registry into the
named protocol (``trusted_monitoring``) with an audit-budget FPR, and optionally defer-to-trusted above a
threshold. It attaches suspicion scores to the trajectory (by running the monitors over the mirror
events); the audit decision is applied at scoring against the honest-twin-calibrated threshold
(``loc_arena/metrics.py``).
"""

from __future__ import annotations

from dataclasses import dataclass

from loc_arena.config import RunConfig
from loc_arena.logging_.events import Event
from loc_arena.monitors.registry import (
    EventSink,
    MonitorContext,
    MonitorModelCaller,
    MonitorRegistry,
    MonitorVerdict,
    _noop_sink,
    run_monitors_split,
)


@dataclass(frozen=True)
class Protocol:
    """The composed control protocol: the enabled monitors plus the audit budget and defer policy."""

    name: str
    registry: MonitorRegistry
    audit_budget_fpr: float
    defer_to_trusted: bool
    async_default_timeout_s: float = 20.0

    def run(self, mirror_events: list[Event], context: MonitorContext) -> list[MonitorVerdict]:
        """Score the mirror trajectory: sync monitors inline, async monitors on bounded worker threads.

        Uses ``run_monitors_split`` with a bounded async join deadline, so a slow/hung async monitor can
        never hang the run and the merged verdicts are canonically sorted (timing is invariant to the
        result). The pure sync path stays available as ``run_monitors``.
        """
        return run_monitors_split(
            self.registry,
            mirror_events,
            context,
            async_default_timeout_s=self.async_default_timeout_s,
        )


def compose_protocol(config: RunConfig, registry: MonitorRegistry) -> Protocol:
    """Compose the protocol named in the run config with its audit budget and defer flag.

    ``async_default_timeout_s`` defaults to 20.0 and is read from an optional ``protocol`` field if
    present; it now lives in the aggregation block.
    """
    proto = config.protocol
    return Protocol(
        name=str(proto.get("name", "trusted_monitoring")),
        registry=registry,
        audit_budget_fpr=float(proto.get("audit_budget_fpr", 0.02)),
        defer_to_trusted=bool(proto.get("defer_to_trusted", False)),
        async_default_timeout_s=float(proto.get("async_default_timeout_s", 20.0)),
    )


def baseline_covered(config: RunConfig) -> frozenset[str]:
    """The fixed deploy-time coverage baseline: every agent identity plus the durable service accounts."""
    agents = {a.id for a in config.agents}
    creds = config.raw.get("credentials", {})
    accounts = creds.get("durable_service_accounts", []) if isinstance(creds, dict) else []
    return frozenset(agents | {str(a) for a in accounts})


def monitor_context(
    config: RunConfig,
    *,
    model_caller: MonitorModelCaller | None = None,
    event_sink: EventSink = _noop_sink,
) -> MonitorContext:
    """Build the observable context a monitor may read (plus, in live mode, the gateway model caller)."""
    goal = str(config.main_task.get("goal", config.main_task.get("name", "")))
    return MonitorContext(
        task_description=goal,
        baseline_covered=baseline_covered(config),
        thresholds=config.side_task.thresholds,
        model_caller=model_caller,
        event_sink=event_sink,
    )
