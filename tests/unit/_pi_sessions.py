"""A stand-in for pi in RPC mode, and the run config of sessions that start it: the unit tests' pi."""

from __future__ import annotations

import dataclasses
import sys
import threading
from pathlib import Path
from time import monotonic

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import GATEWAY_TOKEN_FILE_VARIABLE, GATEWAY_URL_VARIABLE
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.sessions import Sessions
from loc_arena.task import TOOLS_URL_VARIABLE

# A stand-in for pi in RPC mode: it records each prompt and its arguments, then does what its home says.
PI = f"""#!{sys.executable}
import json, os, sys, time
from pathlib import Path

home = Path(os.environ["HOME"])
(home / "cwd").write_text(os.getcwd())


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
    reply = [dict(type="text", text="Answered: " + prompt["message"])]
    stop, error = read("stop", "stop"), read("error")
    answer = dict(role="assistant", stopReason=stop, errorMessage=error, content=reply)
    say(type="message_end", message=answer)
    say(type="agent_settled")
    if read("deaf"):  # it stops reading its input, and goes on living for a while
        os.close(0)
        time.sleep(float(read("deaf")))
        sys.exit(0)
"""
BRIEFS = {"agent-main": "You are agent-main.", "eval-agent": "You are eval-agent."}


def install_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RunConfig:
    """The run config of sessions that start the stand-in pi, put first on ``PATH``."""
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


def write_home(home: Path, **files: str) -> None:
    home.mkdir()
    for name, text in files.items():
        (home / name).write_text(text)


def await_runs(home: Path) -> list[str]:
    """What the stand-in pi of ``home`` was started with, once it took its first prompt."""
    deadline = monotonic() + 10
    while not (home / "runs").exists() and monotonic() < deadline:
        threading.Event().wait(0.05)
    return (home / "runs").read_text().splitlines()


def no_sessions(config: RunConfig, directory: Path) -> Sessions:
    """A play's sessions where no agent starts one."""
    return Sessions(
        config,
        {},
        directory,
        CodeServices(checkouts={}, repos=[], stack=config.settings.stack),
        {},
        30,
    )
