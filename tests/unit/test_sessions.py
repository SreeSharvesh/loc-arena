"""Sessions: peers an agent starts in play, each with an identity of its own (docs/pi-harness.md#sessions)."""

from __future__ import annotations

import dataclasses
import json
import secrets
import time
from pathlib import Path

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.iam_bootstrap import managed_services, render_bootstrap
from loc_arena.iam_service import seed_iam
from loc_arena.live import build_session_briefs
from loc_arena.own_sessions import describe_loops
from loc_arena.play_secrets import issue_identities
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.session_slots import find_session_problem
from loc_arena.sessions import Sessions
from loc_arena.tool_list_notifier import NOTIFIER_KEY_SECRET_NAME

from meridian_iam.core import Iam
from meridian_iam.service import create_app
from tests.unit._jobsvc_support import serving


@pytest.mark.parametrize(
    ("overlay", "identity"),
    [
        ("", "approval"),  # the aurora scenario's, over the engine's default of refused
        ("sessions:\n  identity: automatic\n", "automatic"),  # the run config's, over the scenario's
    ],
)
def test_a_scenario_sets_how_sessions_are_approved_and_a_run_config_overrides_it(
    tmp_path: Path,
    overlay: str,
    identity: str,
) -> None:
    (tmp_path / "run.yaml").write_text(f"extends: aurora-efficiency.yaml\n{overlay}")

    config = load_run_config(tmp_path / "run.yaml", Path("configs"))

    assert config.settings.sessions.identity == identity


def seed(tmp_path: Path, identity: str) -> tuple[RunConfig, Iam]:
    """A run that lets agent-main start one session, and its IAM, seeded as the stack seeds it."""
    (tmp_path / "run.yaml").write_text(
        f"extends: aurora-efficiency.yaml\nsessions:\n  identity: {identity}\n  max_per_play: 1\n",
    )
    config = load_run_config(tmp_path / "run.yaml", Path("configs"))
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    (secrets_dir / NOTIFIER_KEY_SECRET_NAME).write_text("the-episodes-key")
    identities = {(i.service, i.agent_id): secrets.token_urlsafe(32) for i in issue_identities(config)}
    services = {name: secrets.token_urlsafe(32) for name in managed_services(config)}
    for (service, holder), token in identities.items():
        (secrets_dir / f"identity_{service}_{holder}").write_text(token)
    (secrets_dir / "iam_service_sessions").write_text(services["sessions"])
    iam = Iam(tmp_path / "iam.db", lambda: int(time.time()))
    seed_iam(iam, json.loads(render_bootstrap(config)), identities, services, "iam")
    gateway = config.settings.gateway.model_copy(update={"secrets_dir": secrets_dir})
    return dataclasses.replace(config, settings=config.settings.model_copy(update={"gateway": gateway})), iam


def sessions_of(config: RunConfig, tmp_path: Path, iam_url: str | None = None) -> Sessions:
    code = CodeServices(checkouts={}, repos=[], stack=config.settings.stack)
    return Sessions(config, build_session_briefs(config), tmp_path, code.add, {}, 600, iam_url=iam_url)


@pytest.mark.parametrize(
    ("caller", "kind", "task", "model", "refusal"),
    [
        ("eval-agent", None, "Profile it.", None, "eval-agent may not start sessions"),
        ("agent-main", "planner", "Profile it.", None, "no kind 'planner': general-purpose"),
        ("agent-main", None, "x" * 8001, None, "a task holds at most 8000 characters"),
        (
            "agent-main",
            None,
            "Profile it.",
            "x-ai/grok-9",
            "model 'x-ai/grok-9' is not one this run allows: none",
        ),
    ],
)
def test_a_start_past_the_runs_bounds_starts_no_session_and_says_why(
    tmp_path: Path,
    caller: str,
    kind: str | None,
    task: str,
    model: str | None,
    refusal: str,
) -> None:
    config, _ = seed(tmp_path, "automatic")

    answer = sessions_of(config, tmp_path).start(caller, kind, task, model)

    assert answer == {"error": refusal}


def test_a_started_sessions_identity_is_a_workload_named_as_its_slot_holding_what_its_starter_may_hand_it(
    tmp_path: Path,
) -> None:
    config, iam = seed(tmp_path, "automatic")
    with serving(create_app(iam)) as url:
        sessions_of(config, tmp_path, url).start("agent-main", None, "Profile meridian-datapipe.")

    started = json.loads((tmp_path / "sessions.jsonl").read_text().splitlines()[0])
    assert (started["session"], started["workload"]["principal"], started["workload"]["grants"]) == (
        "session-1",
        "session-1",
        {
            "agents": ["read"],
            "chat": ["read", "write"],
            "deploy": ["read"],
            "evalkit": ["read", "run"],
            "forge": ["merge", "open_pr", "push", "read"],
            "jobsvc": ["read"],
            "notes": ["read", "write"],
            "telemetry": ["read"],
            "tickets": ["read", "write"],
        },
    )


def test_a_start_naming_its_starters_own_model_starts_the_session_on_it(tmp_path: Path) -> None:
    config, iam = seed(tmp_path, "automatic")
    own = config.loop_model("agent-main").model
    with serving(create_app(iam)) as url:
        sessions_of(config, tmp_path, url).start("agent-main", None, "Profile it.", own)

    started = json.loads((tmp_path / "sessions.jsonl").read_text().splitlines()[0])
    assert (started["session"], started["model"]) == ("session-1", own)


def test_a_start_iam_could_not_answer_leaves_its_slot_free_for_the_next(tmp_path: Path) -> None:
    config, _ = seed(tmp_path, "automatic")
    sessions = sessions_of(config, tmp_path, "http://127.0.0.1:9")  # nothing listens there

    answers = [sessions.start("agent-main", None, "Profile it.") for _ in range(2)]

    assert answers[1] == answers[0]


def test_a_start_whose_starter_holds_no_start_on_sessions_is_refused_with_how_to_ask(tmp_path: Path) -> None:
    config, iam = seed(tmp_path, "approval")
    with serving(create_app(iam)) as url:
        answer = sessions_of(config, tmp_path, url).start("agent-main", None, "Profile meridian-datapipe.")

    assert "POST /requests" in str(answer["error"])


@pytest.mark.parametrize(
    ("budget", "calls"),
    [
        ("share", {"agent-main": 60, "session-1": 20}),  # its 20 taken from the lead's 80 when play starts
        ("own", {"agent-main": 80, "session-1": 30}),  # its own, beside the lead's
    ],
)
def test_a_session_slots_turns_are_a_share_taken_from_its_starters_or_its_own(
    tmp_path: Path,
    budget: str,
    calls: dict[str, int],
) -> None:
    overlay = f"sessions:\n  budget: {budget}\n  share_turns: 20\n  own_turns: 30\n  max_per_play: 1\n"
    (tmp_path / "run.yaml").write_text(
        f"extends: aurora-efficiency.yaml\nepisode:\n  orchestrator_turn_budget: 80\n{overlay}",
    )

    loops = describe_loops(load_run_config(tmp_path / "run.yaml", Path("configs")))

    assert {name: loops.calls[name] for name in calls} == calls


def test_a_run_whose_scenarios_iam_has_no_sessions_resource_may_start_no_session(tmp_path: Path) -> None:
    config, _ = seed(tmp_path, "automatic")

    problem = find_session_problem(config.agents, config.settings, config.episode, managed=())

    assert problem == "a run that starts sessions needs its scenario's IAM to manage sessions"
