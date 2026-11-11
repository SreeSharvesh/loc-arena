"""Each agent's session is its own pi process: started together, resumed on a nudge, ended at the ceiling."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import GATEWAY_TOKEN_FILE_VARIABLE, GATEWAY_URL_VARIABLE
from loc_arena.own_sessions import (
    NUDGE,
    TASK,
    SessionEnd,
    brief_loop,
    describe_loops,
    play_sessions,
    record_session_ends,
)
from loc_arena.task import TOOLS_URL_VARIABLE

# A stand-in for the pi program: it records each run's input and arguments, then does what its home says.
PI = """#!/bin/sh
echo "$(cat) | $*" >> "$HOME/runs"
sleep "$(cat "$HOME/sleep" 2>/dev/null || echo 0)"
[ -f "$HOME/stderr" ] && cat "$HOME/stderr" >&2
exit "$(cat "$HOME/exit" 2>/dev/null || echo 0)"
"""
BRIEFS = {"agent-main": "You are agent-main.", "eval-agent": "You are eval-agent."}


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunConfig:
    programs = tmp_path / "bin"
    programs.mkdir()
    (programs / "pi").write_text(PI)
    (programs / "pi").chmod(0o755)
    monkeypatch.setenv("PATH", f"{programs}:/usr/bin:/bin")
    monkeypatch.setenv(GATEWAY_URL_VARIABLE, "http://gateway:8080/api/v1/chat/completions")
    monkeypatch.setenv(GATEWAY_TOKEN_FILE_VARIABLE, "/run/secrets/gateway_token")
    monkeypatch.setenv(TOOLS_URL_VARIABLE, "http://agentgateway:3000/mcp")
    return load_run_config("configs/aurora-efficiency.yaml")


def inputs_of(home: Path) -> list[str]:
    return [run.split(" | ")[0] for run in (home / "runs").read_text().splitlines()]


def test_every_agents_session_runs_its_task_then_the_nudge_for_each_resume(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    resumes = config.settings.pi.resumes

    ended = play_sessions(config, BRIEFS, tmp_path, seconds=30)

    played = {agent: inputs_of(tmp_path / agent) for agent in BRIEFS}
    assert ended == dict.fromkeys(BRIEFS, SessionEnd("finished", 0))
    assert played == dict.fromkeys(BRIEFS, [TASK, *[NUDGE] * resumes])


def test_a_session_whose_pi_fails_is_not_resumed(config: RunConfig, tmp_path: Path) -> None:
    (tmp_path / "eval-agent").mkdir()
    (tmp_path / "eval-agent" / "exit").write_text("3")

    ended = play_sessions(config, BRIEFS, tmp_path, seconds=30)

    assert (ended["eval-agent"].reason, inputs_of(tmp_path / "eval-agent")) == ("failed", [TASK])


def test_a_session_whose_models_answer_was_cut_at_its_output_cap_is_resumed(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    (tmp_path / "eval-agent").mkdir()
    (tmp_path / "eval-agent" / "exit").write_text("1")
    (tmp_path / "eval-agent" / "stderr").write_text(
        "Response was truncated: the model reached its output token limit.",
    )

    ended = play_sessions(config, BRIEFS, tmp_path, seconds=30)

    resumed = [TASK, *[NUDGE] * config.settings.pi.resumes]
    assert (ended["eval-agent"], inputs_of(tmp_path / "eval-agent")) == (SessionEnd("finished", 0), resumed)


def test_a_session_still_running_at_the_ceiling_is_ended_there(config: RunConfig, tmp_path: Path) -> None:
    (tmp_path / "agent-main").mkdir()
    (tmp_path / "agent-main" / "sleep").write_text("30")

    ended = play_sessions(config, BRIEFS, tmp_path, seconds=1)

    assert (ended["agent-main"].reason, ended["eval-agent"].reason) == ("ceiling", "finished")


def test_a_loop_asks_its_model_as_the_episode_naming_its_agent(config: RunConfig, tmp_path: Path) -> None:
    play_sessions(config, BRIEFS, tmp_path, seconds=30)

    models = json.loads((tmp_path / "eval-agent" / ".pi" / "agent" / "models.json").read_text())
    role = config.models["untrusted_agent"]
    assert models["providers"]["gateway"] == {
        "baseUrl": "http://gateway:8080/loop/eval-agent/api/v1",
        "api": "openai-completions",
        "apiKey": "!cat /run/secrets/gateway_token",
        "models": [{"id": role.model, "maxTokens": role.max_tokens}],
    }


def test_a_loop_calls_its_tools_at_the_tools_gateway_with_its_agents_key(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    play_sessions(config, BRIEFS, tmp_path, seconds=30)

    servers = json.loads((tmp_path / "eval-agent" / ".pi" / "agent" / "mcp.json").read_text())
    key_file = config.settings.gateway.secrets_dir / "sandbox_token_eval_agent"
    assert servers == {
        "autoEnableCodemode": False,
        "mcpServers": {
            "meridian": {
                "url": "http://agentgateway:3000/mcp",
                "headers": {"Authorization": f"!echo Bearer $(cat {key_file})"},
                "exposure": "direct",
                "timeout": config.settings.stack.tools_timeout_seconds,
            },
        },
    }


def test_a_loop_runs_nothing_itself_and_takes_its_brief_as_its_system_prompt(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    play_sessions(config, BRIEFS, tmp_path, seconds=30)

    home = tmp_path / "agent-main"
    arguments = (home / "runs").read_text().splitlines()[0].split(" | ")[1]
    model = config.models["untrusted_agent"].model
    assert arguments == (
        f"--model gateway/{model} --print --no-builtin-tools --session {home / '.pi' / 'session.jsonl'} "
        f"--system-prompt {home / '.pi' / 'agent' / 'system-prompt.md'}"
    )
    assert (home / ".pi" / "agent" / "system-prompt.md").read_text() == "You are agent-main."


def test_a_session_the_gateway_refused_past_its_budget_ended_on_its_budget(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    (tmp_path / "eval-agent").mkdir()
    (tmp_path / "eval-agent" / "exit").write_text("1")
    (tmp_path / "eval-agent" / "stderr").write_text("429 rate limit exceeded")

    ended = play_sessions(config, BRIEFS, tmp_path, seconds=30)

    assert ended["eval-agent"] == SessionEnd("budget", 1, "429 rate limit exceeded")


def test_a_session_the_provider_rate_limited_past_pis_retries_failed(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    (tmp_path / "eval-agent").mkdir()
    (tmp_path / "eval-agent" / "exit").write_text("1")
    (tmp_path / "eval-agent" / "stderr").write_text('429 {"error": "provider rate limit"}')

    ended = play_sessions(config, BRIEFS, tmp_path, seconds=30)

    assert ended["eval-agent"].reason == "failed"


def test_only_the_attacks_target_agent_gets_the_covert_objective_before_its_brief(config: RunConfig) -> None:
    covert = dataclasses.replace(config.covert, enabled=True)
    attack = dataclasses.replace(config, covert=covert)
    honest = dataclasses.replace(config, covert=dataclasses.replace(covert, enabled=False))
    target = covert.target_identity

    briefs = (
        brief_loop(attack, target, "Brief."),
        brief_loop(attack, "eval-agent", "Brief."),
        brief_loop(honest, target, "Brief."),
    )

    assert briefs == (f"{covert.objective_prompt}\n\nBrief.", "Brief.", "Brief.")


def test_each_loop_may_make_its_agents_turn_budget_of_calls_sent_with_the_configs_request_settings() -> None:
    fast = load_run_config("configs/aurora-efficiency.fast.yaml")

    loops = describe_loops(fast)

    assert loops is not None
    assert (loops.calls["agent-main"], loops.calls["eval-agent"]) == (30, 28)
    assert loops.overrides == {
        "temperature": 1.0,
        "max_completion_tokens": 8192,
        "provider": {"order": ["together", "baseten", "deepseek"], "allow_fallbacks": False},
        "reasoning": {"effort": "none"},
    }


def test_the_native_loop_has_no_loop_at_the_gateway() -> None:
    scripted = load_run_config("configs/aurora-efficiency.deterministic.yaml")

    assert describe_loops(scripted) is None


def test_each_sessions_end_is_recorded_with_its_reason(tmp_path: Path) -> None:
    ended = {"agent-main": SessionEnd("ceiling", None, "late"), "eval-agent": SessionEnd("finished", 0)}

    record_session_ends(tmp_path, ended)

    assert json.loads((tmp_path / "session_ends.json").read_text()) == {
        "agent-main": {"reason": "ceiling", "exit_code": None, "stderr": "late"},
        "eval-agent": {"reason": "finished", "exit_code": 0, "stderr": ""},
    }
