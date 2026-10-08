"""Agent-written code: the one place it is run, here, in the sandbox, or in a throwaway container."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat

from loc_arena.gateway.core import environment_without_key
from loc_arena.settings import StackSettings

CONTAINER_NAME_PREFIX = "locarena-agent-code-"
# How long `docker kill`, then `docker rm --force`, may take to stop a container whose run timed out.
CLEANUP_TIMEOUT_SECONDS = 10


def run_agent_code(
    arguments: Sequence[str],
    *,
    working_directory: Path,
    mount: Path,
    pythonpath: str,
    timeout_seconds: float,
    stack: StackSettings,
) -> subprocess.CompletedProcess[str]:
    """Run ``python <arguments>`` over code the agents wrote and return the finished process.

    With ``stack.sandbox_agent_code`` the process runs in a throwaway container of ``stack.image``: no
    network, no capabilities, the episode's memory, CPU and process limits, the host user's uid and gid, and
    only ``mount`` visible, at the same absolute path so ``pythonpath`` stays valid. Otherwise it runs in this
    process's interpreter with the provider key removed from its environment. Its stdout and stderr go to
    temporary files and come back as their last ``stack.agent_code_output_limit_bytes`` bytes. A timeout stops
    the container, so a hung process never outlives its caller, and re-raises ``subprocess.TimeoutExpired``.
    """
    container_name = f"{CONTAINER_NAME_PREFIX}{uuid.uuid4().hex}"
    if stack.sandbox_agent_code:
        command = [
            "docker", "run", "--rm", "--network", "none", "--cap-drop", "ALL",
            "--memory", stack.episode_memory_limit,
            "--cpus", str(stack.episode_cpus),
            "--pids-limit", str(stack.episode_pids_limit),
            "--name", container_name,
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--volume", f"{mount}:{mount}",
            "--workdir", str(working_directory),
            "--env", f"PYTHONPATH={pythonpath}",
            stack.image, "python", *arguments,
        ]  # fmt: skip
        directory, environment = None, None
    else:
        command = [sys.executable, *arguments]
        directory, environment = working_directory, {**environment_without_key(), "PYTHONPATH": pythonpath}
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            finished = subprocess.run(
                command,
                cwd=directory,
                env=environment,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as expired:
            if stack.sandbox_agent_code and not _stop_container(container_name):
                raise RuntimeError(f"container {container_name} still runs after a timeout") from expired
            raise
        limit = stack.agent_code_output_limit_bytes
        output = (read_tail(stdout, limit), read_tail(stderr, limit))
        return subprocess.CompletedProcess(command, finished.returncode, *output)


class CommandRequest(BaseModel):
    """A command an agent's tool runs over the checkout: what, where, for how long."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: list[str] = Field(min_length=1, description="The program and its arguments.")
    directory: Path = Field(description="The working directory.")
    timeout_seconds: PositiveFloat = Field(description="Seconds before its whole session is killed.")
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="Variables set on top of the runner's environment, which never holds the provider key.",
    )


class CommandResult(BaseModel):
    """What a command did: its exit code (none when it was killed at the timeout) and its output's tails."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int | None = Field(description="The exit code; none when the timeout killed the command.")
    stdout: str = Field(description="The end of its stdout.")
    stderr: str = Field(description="The end of its stderr.")


def run_command(request: CommandRequest, output_limit_bytes: int) -> CommandResult:
    """Run ``request`` without the provider key; a timeout kills its whole session, children included.

    Its stdout and stderr go to temporary files and come back as their last ``output_limit_bytes`` bytes.
    """
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(
            request.argv,
            cwd=request.directory,
            env={**environment_without_key(), **request.environment},
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        ) as process:
            try:
                returncode: int | None = process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):  # the whole session ended meanwhile
                    os.killpg(process.pid, signal.SIGKILL)
                returncode = None
        return CommandResult(
            returncode=returncode,
            stdout=read_tail(stdout, output_limit_bytes),
            stderr=read_tail(stderr, output_limit_bytes),
        )


def read_tail(stream: IO[bytes], limit: int) -> str:
    """The last ``limit`` bytes written to ``stream``, decoded."""
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - limit))
    return stream.read().decode(errors="replace")


def _stop_container(container_name: str) -> bool:
    """Kill the container, or else force-remove it; whether it is gone (a container already gone counts)."""
    for command in (["docker", "kill", container_name], ["docker", "rm", "--force", container_name]):
        try:
            stopped = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=CLEANUP_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            continue
        if stopped.returncode == 0 or "No such container" in stopped.stderr:
            return True
    return False
