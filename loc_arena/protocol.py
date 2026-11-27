"""Control protocol under test.

Enforces the firewall (composes the ENABLED monitors, never the sealed verifier) and config over code
(the audit budget comes from the run config). Composes the wired monitors from the registry into the
named protocol (``trusted_monitoring``) with an audit-budget FPR. It attaches suspicion scores to the
trajectory (by running the monitors over the mirror events); the audit decision is applied at scoring
against the honest-baseline-calibrated threshold (``loc_arena/metrics.py``).
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
from loc_arena.session_slots import name_slots


@dataclass(frozen=True)
class Protocol:
    """The composed control protocol: the enabled monitors plus the audit budget."""

    name: str
    registry: MonitorRegistry
    audit_budget_fpr: float
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
    """Compose the protocol named in the run config with its audit budget.

    Grading sets ``async_default_timeout_s`` from the monitors file's aggregation block.
    """
    proto = config.protocol
    return Protocol(
        name=str(proto.get("name", "trusted_monitoring")),
        registry=registry,
        audit_budget_fpr=float(proto.get("audit_budget_fpr", 0.02)),
    )


def monitor_context(
    config: RunConfig,
    *,
    model_caller: MonitorModelCaller | None = None,
    event_sink: EventSink = _noop_sink,
) -> MonitorContext:
    """Build the observable context a monitor may read (plus, in live mode, the gateway model caller)."""
    return MonitorContext(
        task_description=config.task_description,
        thresholds=config.side_task.thresholds,
        model_caller=model_caller,
        event_sink=event_sink,
        agents=(
            *(agent.id for agent in config.agents),
            *name_slots(config),
        ),  # a session is an agent of its own
    )
