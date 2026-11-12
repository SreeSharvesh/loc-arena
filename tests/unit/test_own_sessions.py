"""Each agent's session is its own pi process: started together, woken by a notice, ended at the ceiling."""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import GATEWAY_TOKEN_FILE_VARIABLE, GATEWAY_URL_VARIABLE
from loc_arena.notices import Notices
from loc_arena.own_sessions import (
    TASK,
    SessionEnd,
    brief_loop,
    describe_loops,
    play_sessions,
    record_session_ends,
)
from loc_arena.settings import AgentsUpstream
from loc_arena.task import TOOLS_URL_VARIABLE
from loc_arena.tool_records import append_to
from pydantic import HttpUrl

# A stand-in for pi in RPC mode: it records each prompt and its arguments, then does what its home says.
PI = f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path

home = Path(os.environ["HOME"])


def read(name, default=""):
    return (home / name).read_text().strip() if (home / name).exists() else default


def say(**event):
    print(json.dumps(event), flush=True)


for line in sys.stdin:
    prompt = json.loads(line)
    with (home / "runs").open("a") as runs:
        runs.write(prompt["message"] + " | " + " ".join(sys.argv[1:]) + "\\n")
    if read("exit"):
        sys.exit(int(read("exit")))
    say(type="response", command="prompt", success=True, data=dict(disposition="started"))
    time.sleep(float(read("sleep", "0")))
    if read("writes"):  # what a tool call of its own made a service record
        with Path(read("writes")).open("a") as records:
            records.write(read("record") + "\\n")
    answer = dict(role="assistant", stopReason=read("stop", "stop"), errorMessage=read("error"))
    say(type="message_end", message=answer)
    say(type="agent_settled")
    if read("deaf"):  # it stops reading its input, and goes on living for a while
        os.close(0)
        time.sleep(float(read("deaf")))
        sys.exit(0)
"""
BRIEFS = {"agent-main": "You are agent-main.", "eval-agent": "You are eval-agent."}
NOTICE = "Notice: agent-main sent you a direct message."


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
    config = load_run_config("configs/aurora-efficiency.yaml")
    (tmp_path / "records").mkdir()
    stack = config.settings.stack.model_copy(update={"records_directory": tmp_path / "records"})
    pi = config.settings.pi.model_copy(update={"minimum_play_seconds": 0.0})  # the run config's is minutes
    return dataclasses.replace(config, settings=config.settings.model_copy(update={"stack": stack, "pi": pi}))


def play(config: RunConfig, tmp_path: Path, seconds: float = 30) -> dict[str, SessionEnd]:
    noticed = append_to(tmp_path / "notices.jsonl")
    return play_sessions(config, BRIEFS, tmp_path, seconds, Notices(config, BRIEFS), noticed)


def write_home(home: Path, **files: str) -> None:
    home.mkdir()
    for name, text in files.items():
        (home / name).write_text(text)


def message_from_agent_main(config: RunConfig, tmp_path: Path) -> dict[str, str]:
    """What makes agent-main's pi leave chat's record of its direct message to eval-agent, after a moment."""
    chat = next(service.name for service in config.live_services if "send_dm" in service.tools)
    record = {
        "time": datetime.now(UTC).isoformat(),
        "gen_ai.tool.name": "send_dm",
        "gen_ai.tool.call.arguments": {"member": "eval-agent", "body": "the numbers are in"},
        "gen_ai.tool.call.result": {},
        "gen_ai.agent.name": "agent-main",
    }
    records = tmp_path / "records" / f"{chat}.jsonl"
    return {"sleep": "0.5", "writes": str(records), "record": json.dumps(record)}


def inputs_of(home: Path) -> list[str]:
    return [run.split(" | ")[0] for run in (home / "runs").read_text().splitlines()]


def test_every_agents_session_runs_its_task_and_has_finished_once_all_are_idle(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    ended = play(config, tmp_path)

    played = {agent: inputs_of(tmp_path / agent) for agent in BRIEFS}
    assert ended == dict.fromkeys(BRIEFS, SessionEnd("finished", 0))
    assert played == dict.fromkeys(BRIEFS, [TASK])


def test_a_notice_of_a_topic_an_idle_agent_subscribed_to_starts_its_session_again_to_its_end(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    write_home(tmp_path / "agent-main", **message_from_agent_main(config, tmp_path))

    ended = play(config, tmp_path)

    woken = (inputs_of(tmp_path / "eval-agent"), ended["eval-agent"])
    assert woken == ([TASK, NOTICE], SessionEnd("finished", 0))  # play went on until it was idle again


def lasting(config: RunConfig, minimum: float) -> RunConfig:
    pi = config.settings.pi.model_copy(update={"minimum_play_seconds": minimum})
    return dataclasses.replace(config, settings=config.settings.model_copy(update={"pi": pi}))


def test_play_lasts_its_minimum_though_every_session_is_idle(config: RunConfig, tmp_path: Path) -> None:
    start = monotonic()

    ended = play(lasting(config, 2.0), tmp_path)

    assert (monotonic() - start >= 2.0, ended) == (True, dict.fromkeys(BRIEFS, SessionEnd("finished", 0)))


def test_the_wall_clock_ends_play_before_a_longer_minimum(config: RunConfig, tmp_path: Path) -> None:
    start = monotonic()

    play(lasting(config, 40.0), tmp_path, seconds=1)

    assert monotonic() - start < 30


def test_a_notice_that_comes_while_every_session_is_idle_starts_its_session_within_the_minimum(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    files = message_from_agent_main(config, tmp_path)
    later = threading.Timer(1.0, lambda: append_to(Path(files["writes"]))(files["record"]))
    later.start()

    play(lasting(config, 3.0), tmp_path)

    assert inputs_of(tmp_path / "eval-agent") == [TASK, NOTICE]


def test_play_with_no_session_to_run_ends_at_once_whatever_its_minimum(
    config: RunConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(GATEWAY_URL_VARIABLE)  # no agent's pi home can be written
    start = monotonic()

    ended = play(lasting(config, 20.0), tmp_path)

    assert (monotonic() - start < 10, {end.reason for end in ended.values()}) == (True, {"failed"})


def test_an_agent_that_unsubscribed_from_a_topic_gets_no_notice_of_it(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    write_home(tmp_path / "agent-main", **message_from_agent_main(config, tmp_path))
    notices = Notices(config, BRIEFS)
    notices.unsubscribe("eval-agent", "dm")

    play_sessions(config, BRIEFS, tmp_path, 30, notices, lambda _: None)

    assert inputs_of(tmp_path / "eval-agent") == [TASK]


def test_each_notice_is_recorded_with_whether_its_agents_session_took_it(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    write_home(tmp_path / "agent-main", **message_from_agent_main(config, tmp_path))
    write_home(tmp_path / "eval-agent", stop="error", error="429 rate limit exceeded")

    play(config, tmp_path)

    noticed = [json.loads(line) for line in (tmp_path / "notices.jsonl").read_text().splitlines()]
    assert [{**notice, "time": None} for notice in noticed] == [
        {
            "time": None,
            "agent": "eval-agent",
            "topic": "dm",
            "text": "agent-main sent you a direct message.",
            "sent": False,
        },
    ]


def test_play_ends_every_session_though_one_stopped_reading_its_notices(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    write_home(tmp_path / "agent-main", **message_from_agent_main(config, tmp_path))
    write_home(tmp_path / "eval-agent", deaf="2")

    ended = play(config, tmp_path)

    assert {agent: end.reason for agent, end in ended.items()} == dict.fromkeys(BRIEFS, "finished")


def test_a_session_whose_last_run_failed_still_takes_a_notice(config: RunConfig, tmp_path: Path) -> None:
    write_home(tmp_path / "agent-main", **message_from_agent_main(config, tmp_path))
    write_home(tmp_path / "eval-agent", stop="error", error="500 upstream failed")

    ended = play(config, tmp_path)

    taken = (inputs_of(tmp_path / "eval-agent"), ended["eval-agent"])
    assert taken == ([TASK, NOTICE], SessionEnd("failed", 0, "500 upstream failed"))


def test_a_session_whose_pi_reads_its_input_no_more_does_not_hold_play_past_the_ceiling(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    flood = message_from_agent_main(config, tmp_path)
    flood["record"] = "\n".join([flood["record"]] * 2000)  # more than a pipe holds
    write_home(tmp_path / "agent-main", **flood)
    write_home(tmp_path / "eval-agent", sleep="30")

    ended = play(config, tmp_path, seconds=2)

    noticed = [json.loads(line) for line in (tmp_path / "notices.jsonl").read_text().splitlines()]
    unsent = sum(not notice["sent"] for notice in noticed)
    assert (ended["eval-agent"].reason, len(noticed), unsent > 0) == ("ceiling", 2000, True)


def test_a_session_whose_models_answer_was_cut_at_its_output_cap_is_idle_like_any_other(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    write_home(tmp_path / "eval-agent", stop="length")

    ended = play(config, tmp_path)

    assert ended["eval-agent"] == SessionEnd("finished", 0)


def test_a_session_whose_pi_exits_by_itself_failed(config: RunConfig, tmp_path: Path) -> None:
    write_home(tmp_path / "eval-agent", exit="3")

    ended = play(config, tmp_path)

    assert (ended["eval-agent"].reason, ended["eval-agent"].exit_code) == ("failed", 3)


def test_a_session_still_working_at_the_ceiling_is_ended_there(config: RunConfig, tmp_path: Path) -> None:
    write_home(tmp_path / "agent-main", sleep="30")

    ended = play(config, tmp_path, seconds=1)

    assert (ended["agent-main"], ended["eval-agent"].reason) == (SessionEnd("ceiling"), "finished")


def test_a_loop_asks_its_model_as_the_episode_naming_its_agent(config: RunConfig, tmp_path: Path) -> None:
    play(config, tmp_path)

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
    play(config, tmp_path)

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
    play(config, tmp_path)

    home = tmp_path / "agent-main"
    arguments = (home / "runs").read_text().splitlines()[0].split(" | ")[1]
    model = config.models["untrusted_agent"].model
    assert arguments == (
        f"--model gateway/{model} --mode rpc --no-builtin-tools --session {home / '.pi' / 'session.jsonl'} "
        f"--system-prompt {home / '.pi' / 'agent' / 'system-prompt.md'}"
    )
    assert (home / ".pi" / "agent" / "system-prompt.md").read_text() == "You are agent-main."


def test_a_session_the_gateway_refused_past_its_budget_ended_on_its_budget(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    write_home(tmp_path / "eval-agent", stop="error", error="429 rate limit exceeded")

    ended = play(config, tmp_path)

    assert ended["eval-agent"] == SessionEnd("budget", 0, "429 rate limit exceeded")


def test_a_session_the_provider_rate_limited_past_pis_retries_failed(
    config: RunConfig,
    tmp_path: Path,
) -> None:
    write_home(tmp_path / "eval-agent", stop="error", error='429 {"error": "provider rate limit"}')

    ended = play(config, tmp_path)

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
    assert (loops.calls["agent-main"], loops.calls["eval-agent"]) == (80, 60)
    assert loops.overrides == {
        "temperature": 1.0,
        "max_completion_tokens": 8192,
        "provider": {"order": ["together", "baseten", "deepseek"], "allow_fallbacks": False},
        "reasoning": {"effort": "none"},
    }


def test_a_loop_on_the_agents_own_upstream_is_sent_without_openrouters_provider_routing() -> None:
    fast = load_run_config("configs/aurora-efficiency.fast.yaml")
    second = AgentsUpstream(
        url=HttpUrl("https://api.second.example"),
        chat_path="v1/chat",
        key_variable="SECOND_API_KEY",
    )
    gateway = fast.settings.gateway.model_copy(update={"agents_upstream": second})
    config = dataclasses.replace(fast, settings=fast.settings.model_copy(update={"gateway": gateway}))

    loops = describe_loops(config)

    assert loops is not None
    assert "provider" not in loops.overrides


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
