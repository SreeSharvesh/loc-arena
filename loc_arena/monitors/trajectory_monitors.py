"""The monitors that read whole trajectories: one per agent, and one over the team's summaries.

Both read only the observable steps of the agents under test: a tool call with its result, or a chat
message. A step of an agent's subagent is in its agent's trajectory, so handing work to a subagent hides
nothing from the agent's monitor.

- ``agent_trajectory_monitor`` rates each agent's whole trajectory in one prompt: one verdict per agent.
- ``team_summary_monitor`` has its model summarize each agent's trajectory, without rating it, and then
  rates the summaries together in one prompt: one verdict for the team.

Each rates with the live model alone. Without a model, or where a call fails, it gives no verdict, never a
rule's guess. A verdict is dated at the last step it read, so it is a judgement after the play.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from loc_arena.config import ConfigError, ModelSpec
from loc_arena.logging_.events import Event
from loc_arena.monitors.aggregation import reduce_monitor
from loc_arena.monitors.prompt_values import load_prompt_template
from loc_arena.monitors.registry import (
    ModelCallsStoppedError,
    Monitor,
    MonitorContext,
    MonitorSpec,
    MonitorVerdict,
    live_suspicion,
    register_monitor,
    render_prompt,
)

TAP = "trajectory"
STEP_KINDS = ("action", "message")
DEFAULT_RESULT_CHARS = 20000  # of one tool result a trajectory holds, unless the spec says ``result_chars``
DEFAULT_SUMMARY_WORDS = 300  # of one agent's summary, unless the spec says ``summary_words``


class TrajectoryReader:
    """What both monitors share: each agent's steps, the prompt inputs of one, and the sampled verdict."""

    def __init__(self, spec: MonitorSpec, model_spec: ModelSpec | None) -> None:
        """Hold the spec; a trajectory monitor has no rule to fall back on, so it must be ``mode: live``."""
        if spec.mode != "live":
            raise ConfigError(f"monitor {spec.name!r}: it rates with its model alone, so its mode is 'live'")
        self.name = spec.name
        self.taps = spec.taps
        self.timing = spec.timing
        self.timeout_s = spec.timeout_s
        self._spec = spec
        self._model = model_spec
        self._result_chars = int(spec.extra.get("result_chars", DEFAULT_RESULT_CHARS))
        self._sample_reduce = str(spec.extra.get("ensemble_reduce", "max"))

    def trajectories(self, events: list[Event], context: MonitorContext) -> dict[str, list[Event]]:
        """Each agent's steps in order, its subagents' among them; an agent with no step has no entry."""
        by_agent: dict[str, list[Event]] = {}
        for event in events:
            agent = event.actor_uid.partition(".")[0]  # a subagent's agent
            if event.kind in STEP_KINDS and agent in context.agents:
                by_agent.setdefault(agent, []).append(event)
        return by_agent

    def input_payload(self, agent: str, steps: list[Event], context: MonitorContext) -> dict[str, object]:
        """One agent's trajectory as prompt inputs, each step restricted to the DECLARED inputs (firewall)."""
        declared = self._spec.inputs
        rows: list[dict[str, object]] = []
        for event in steps:
            available: dict[str, object] = {
                "actor_id": event.actor_uid,
                "tool_name": event.tool,
                "tool_args": event.payload.get("args"),
                "message_body": event.payload.get("body"),
            }
            if event.kind == "action":
                available["tool_result"] = json.dumps(event.result, default=str)[: self._result_chars]
            if event.seq in context.outcomes:
                available["tool_outcome"] = context.outcomes[event.seq].payload
            rows.append({k: available[k] for k in declared if available.get(k) is not None})
        return {**self.task_input(context), "agent": agent, "steps": rows}

    def task_input(self, context: MonitorContext) -> dict[str, object]:
        """The team's task as a prompt input, where the monitor declares it."""
        declared = "task_description" in self._spec.inputs
        return {"task_description": context.task_description} if declared else {}

    def rate(self, context: MonitorContext, payload: Mapping[str, object]) -> tuple[float, str] | None:
        """The monitor's samples of one prompt reduced to a score, with the highest sample's reason."""
        prompt = render_prompt(self._spec.prompt_template, payload)
        samples = [
            sample
            for _ in range(max(1, self._spec.ensemble_n))
            if (
                sample := live_suspicion(
                    context,
                    self.name,
                    prompt,
                    ensemble_n=1,
                    temperature=self._spec.temperature,
                )
            )
            is not None
        ]
        if not samples:
            return None
        return reduce_monitor([score for score, _ in samples], self._sample_reduce), max(samples)[1]

    def verdict(
        self,
        rated: tuple[float, str],
        actor: str,
        steps: list[Event],
        context: MonitorContext,
    ) -> MonitorVerdict:
        """A model's verdict on ``actor``, on the last of ``steps``, dated at the latest evidence it read.

        That is the last step, or the outcome of a step's work where it ended later and the monitor reads
        outcomes: as the action monitor dates a verdict that read one.
        """
        last_step = max(steps, key=lambda event: event.seq)
        outcomes = context.outcomes if "tool_outcome" in self._spec.inputs else {}
        read_at = [last_step.ts, *(outcomes[event.seq].ts for event in steps if event.seq in outcomes)]
        return MonitorVerdict(
            self.name,
            TAP,
            rated[0],
            rated[1],
            actor,
            last_step.seq,
            max(read_at),
            from_model=True,
        )


class AgentTrajectoryMonitor(TrajectoryReader):
    """One verdict per agent, on its whole trajectory; the run's score is the highest agent's."""

    def run(self, events: list[Event], context: MonitorContext) -> list[MonitorVerdict]:
        """Rate each agent's trajectory in a prompt of its own."""
        verdicts: list[MonitorVerdict] = []
        for agent, steps in self.trajectories(events, context).items():
            rated = self.rate(context, self.input_payload(agent, steps, context))
            if rated is not None:
                verdicts.append(self.verdict(rated, agent, steps, context))
        return verdicts


class TeamSummaryMonitor(TrajectoryReader):
    """One verdict for the team, on a summary of each agent's trajectory read together."""

    def __init__(self, spec: MonitorSpec, model_spec: ModelSpec | None) -> None:
        """Load the summarizer's prompt, which ``summarizer_prompt_ref`` names, its word limit filled."""
        super().__init__(spec, model_spec)
        self._summary_words = int(spec.extra.get("summary_words", DEFAULT_SUMMARY_WORDS))
        if self._summary_words < 1:
            raise ConfigError(f"monitor {spec.name!r}: summary_words must be at least 1")
        reference = spec.extra.get("summarizer_prompt_ref")
        if not isinstance(reference, str):
            raise ConfigError(f"monitor {spec.name!r}: summarizer_prompt_ref names no prompt file")
        values = {"summary_words": (str(self._summary_words), str(Path(reference)))}
        self._summarizer_template, _ = load_prompt_template(spec.name, reference, spec.mode, values)

    def summarize(self, agent: str, steps: list[Event], context: MonitorContext) -> str | None:
        """The model's summary of one agent's trajectory, cut to its word limit and recorded.

        None where the call gave no text. The prompt asks for the limit, and a longer reply is cut at it, so
        ``summary_words`` bounds what the team's rating reads whatever the model writes.
        """
        if context.model_caller is None:
            return None
        prompt = render_prompt(self._summarizer_template, self.input_payload(agent, steps, context))
        try:
            summary = context.model_caller(self.name, prompt, self._spec.temperature).strip()
        except ModelCallsStoppedError:
            return None
        except Exception as exc:  # noqa: BLE001 - a monitor must never crash an episode
            context.event_sink("monitor_parse_error", {"monitor": self.name, "error": f"call:{exc}"})
            return None
        if not summary:
            context.event_sink("monitor_parse_error", {"monitor": self.name, "error": f"no summary: {agent}"})
            return None
        words = list(re.finditer(r"\S+", summary))
        if len(words) > self._summary_words:
            summary = summary[: words[self._summary_words - 1].end()]
        context.event_sink("monitor_summary", {"monitor": self.name, "agent": agent, "summary": summary})
        return summary

    def run(self, events: list[Event], context: MonitorContext) -> list[MonitorVerdict]:
        """Summarize each agent, then rate the summaries together; no verdict if any summary is missing."""
        trajectories = self.trajectories(events, context)
        summaries: dict[str, str] = {}
        for agent, steps in trajectories.items():
            summary = self.summarize(agent, steps, context)
            if summary is None:  # a verdict on part of the team would read as one on all of it
                return []
            summaries[agent] = summary
        if not summaries:
            return []
        rated = self.rate(context, {**self.task_input(context), "summaries": summaries})
        every_step = [step for steps in trajectories.values() for step in steps]
        return [] if rated is None else [self.verdict(rated, self.name, every_step, context)]


def _build_agent_trajectory_monitor(spec: MonitorSpec, model_spec: ModelSpec | None) -> Monitor:
    return AgentTrajectoryMonitor(spec, model_spec)


def _build_team_summary_monitor(spec: MonitorSpec, model_spec: ModelSpec | None) -> Monitor:
    return TeamSummaryMonitor(spec, model_spec)


register_monitor("agent_trajectory_monitor", _build_agent_trajectory_monitor)
register_monitor("team_summary_monitor", _build_team_summary_monitor)
