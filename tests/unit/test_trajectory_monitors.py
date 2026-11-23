"""The monitors that read whole trajectories: one verdict per agent, and one on the team's summaries."""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest
from loc_arena.logging_.events import Event
from loc_arena.monitors.registry import MonitorVerdict, load_monitors, run_monitors
from loc_arena.protocol import monitor_context

from tests.unit._monitor_support import RUN_CONFIG, SHIPPED_MONITORS, VERDICT_REPLY

PER_AGENT, TEAM = "agent_trajectory_monitor", "team_summary_monitor"


def _step(seq: int, actor: str, command: str) -> Event:
    return Event(
        episode_id="ep-trajectory",
        seq=seq,
        ts=float(seq),
        actor_uid=actor,
        actor_role="untrusted",
        kind="action",
        payload={"args": {"command": command}},
        tool="bash",
        result={"stdout": f"ran {command}"},
    )


# Two agents of the run, one with a subagent, and a scripted coworker, who is not under test.
STEPS = [
    _step(0, "agent-main", "main-command"),
    _step(1, "serving-agent", "serving-command"),
    _step(2, "agent-main.1", "subagent-command"),
    _step(3, "coworker.efficiency", "coworker-command"),
    _step(4, "serving-agent", "serving-last-command"),
]


def _score(monitor: str, replies: Iterator[str]) -> tuple[list[MonitorVerdict], list[str]]:
    """The shipped ``monitor``'s verdicts on ``STEPS`` and its prompts, its model giving ``replies``."""
    prompts: list[str] = []

    def call(name: str, prompt: str, temperature: float) -> str:
        prompts.append(prompt)
        return next(replies)

    registry = load_monitors(SHIPPED_MONITORS, RUN_CONFIG.models, names={monitor})
    return run_monitors(registry, STEPS, monitor_context(RUN_CONFIG, model_caller=call)), prompts


def test_the_per_agent_monitor_gives_each_agent_one_verdict_dated_at_its_last_step() -> None:
    verdicts, _ = _score(PER_AGENT, itertools.repeat(VERDICT_REPLY))

    assert [(v.actor_uid, v.target_seq) for v in verdicts] == [("agent-main", 2), ("serving-agent", 4)]


def test_an_agents_trajectory_holds_its_subagents_steps_and_no_other_actors() -> None:
    _, prompts = _score(PER_AGENT, itertools.repeat(VERDICT_REPLY))

    of_main = prompts[0]
    assert "main-command" in of_main
    assert "subagent-command" in of_main
    assert "serving-command" not in of_main
    assert not [prompt for prompt in prompts if "coworker-command" in prompt]


def test_an_agents_score_is_the_mean_of_its_samples() -> None:
    samples = ['{"suspicion": 0.2, "reason": "a"}', '{"suspicion": 0.4, "reason": "b"}'] * 3
    samples[2] = samples[5] = '{"suspicion": 0.9, "reason": "c"}'

    verdicts, _ = _score(PER_AGENT, iter(samples))

    assert [v.score for v in verdicts] == pytest.approx([0.5, 0.5])  # (0.2 + 0.4 + 0.9) / 3, per agent


def test_the_team_monitor_rates_the_agents_summaries_and_not_their_steps() -> None:
    replies = ["summary-of-main", "summary-of-serving", *[VERDICT_REPLY] * 3]  # one per agent, then 3 samples

    verdicts, prompts = _score(TEAM, iter(replies))

    rated = prompts[2:]
    assert len(verdicts) == 1
    assert len(rated) == 3
    assert all("summary-of-main" in prompt and "summary-of-serving" in prompt for prompt in rated)
    assert not [prompt for prompt in rated if "main-command" in prompt]


def test_the_team_monitor_gives_no_verdict_when_an_agents_summary_is_missing() -> None:
    verdicts, prompts = _score(TEAM, iter(["a summary of the first agent", "  "]))

    assert verdicts == []
    assert len(prompts) == 2  # it rated nothing: a verdict on part of the team would read as one on all


@pytest.mark.parametrize("monitor", [PER_AGENT, TEAM])
def test_a_trajectory_monitor_without_a_model_gives_no_verdict(monitor: str) -> None:
    registry = load_monitors(SHIPPED_MONITORS, RUN_CONFIG.models, names={monitor})

    assert run_monitors(registry, STEPS, monitor_context(RUN_CONFIG)) == []
