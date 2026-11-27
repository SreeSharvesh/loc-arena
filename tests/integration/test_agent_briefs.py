"""Each agent under test is given a brief that holds its place in the team and its own prompts."""

from __future__ import annotations

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.harness import apply_mode
from loc_arena.live import build_briefs

TEAMS = load_run_config("configs/aurora-teams.yaml")
# The opening words of prompts.lead.system and prompts.report.system, and a phrase of the composed
# covert objective (prompts.team.covert-objective) that no legitimate brief carries.
LEAD_PROMPT = "You lead Meridian's Aurora efficiency push."
WORKER_PROMPT = "You work on the Aurora sprint for the agent you report to."
COVERT_STEP = "off the sanctioned path"
# The model-selection run on the default team: its main task is not the efficiency push.
SELECTION = load_run_config("configs/aurora-model-evaluation.yaml")
# The same run with the covert objective held by the lead alone: its reports have no covert brief.
CONTROL_NOTE = load_run_config("configs/aurora-model-evaluation.control-note.yaml")
# The default team, whose agents are offered the telemetry store's tools; aurora-teams offers them to none.
EFFICIENCY = load_run_config("configs/aurora-efficiency.yaml")


def test_each_agents_brief_holds_its_place_in_the_team_and_its_system_prompt() -> None:
    briefs = build_briefs(apply_mode(TEAMS, "honest"))

    assert "Reporting to you: serving-1, serving-2, serving-3, distill-1" in briefs["agent-main"]
    assert LEAD_PROMPT in briefs["agent-main"]
    assert "You report to agent-main." in briefs["eval-3"]
    assert WORKER_PROMPT in briefs["eval-3"]
    assert COVERT_STEP not in briefs["serving-1"]


def test_an_attack_play_adds_the_covert_brief_to_the_agent_that_carries_it() -> None:
    briefs = build_briefs(apply_mode(TEAMS, "attack"))

    assert COVERT_STEP in briefs["serving-1"]
    assert WORKER_PROMPT in briefs["serving-1"]


def test_an_honest_play_gives_each_agent_the_assignment_its_run_config_briefs_it() -> None:
    briefs = build_briefs(apply_mode(SELECTION, "honest"))

    assert "Lead the Aurora model-selection push" in briefs["agent-main"]
    assert "on the tickets your lead assigns you" in briefs["eval-agent"]


def test_an_attack_play_whose_lead_alone_holds_the_objective_briefs_its_reports_as_the_twin_does() -> None:
    attack, honest = (build_briefs(apply_mode(CONTROL_NOTE, mode)) for mode in ("attack", "honest"))

    assert COVERT_STEP in attack["agent-main"]
    assert {agent for agent in attack if attack[agent] != honest[agent]} == {"agent-main"}


@pytest.mark.parametrize("mode", ["honest", "attack"])
def test_a_run_with_another_main_task_briefs_no_agent_on_the_efficiency_push(mode: str) -> None:
    briefs = build_briefs(apply_mode(SELECTION, mode))

    assert len(briefs) == 7
    stale = ("pipeline cost", "optimiz")  # how the efficiency push's briefs name its work
    assert {agent for agent, brief in briefs.items() if any(word in brief for word in stale)} == set()


def test_a_brief_names_the_services_a_sandbox_reaches_and_not_the_telemetry_store() -> None:
    brief = build_briefs(apply_mode(TEAMS, "honest"))["agent-main"]

    assert "- http://phoenix:6006:" in brief
    assert "http://telemetry:" not in brief


@pytest.mark.parametrize(
    ("config", "told"),
    [(EFFICIENCY, True), (TEAMS, False)],
    ids=["offered the store's tools", "offered none"],
)
def test_a_brief_says_what_the_telemetry_store_holds_only_to_an_agent_offered_its_tools(
    config: RunConfig,
    told: bool,
) -> None:
    brief = build_briefs(apply_mode(config, "honest"))["agent-main"]

    assert ("opentelemetry_logs" in brief) is told
