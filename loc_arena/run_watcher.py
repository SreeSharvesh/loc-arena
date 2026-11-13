"""The run watcher behind ``make watch``: every agent's session of a run, step by step, while it plays.

A run writes its transcript and report only once both plays are graded, so until then nothing shows whether
the agents took up their tasks. Each agent's pi session file is written as the session goes, in the episode
container's output directory; the watcher follows those files there and prints one line per step: what the
agent thought, said, called and got back, with its play and its name. Given a finished run's directory it
prints the same from the files in it, once.

It only reads, through ``docker exec`` in the episode container, where no agent's code runs. What it prints is
the experimenter's view of the run, as the sealed log is: never give it to an agent or a monitor.
"""

from __future__ import annotations

import functools
import json
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from pydantic import BaseModel, JsonValue

from loc_arena.episode_stack import OUTPUT_DIRECTORY
from loc_arena.gateway.model_gateway import PHASE_PLAYS, PROJECT_PREFIX
from loc_arena.own_sessions import LOOPS_DIRECTORY, SESSION_FILE
from loc_arena.stack_play import EPISODE_SERVICE

SESSION_GLOB = (
    f"*/{LOOPS_DIRECTORY}/*/{SESSION_FILE}"  # under a run directory: <phase>/loops/<agent>/<session>
)
# What a text block is, by the role of its message; a block of pi's own kind keeps that kind's name.
TEXT_KINDS = {"assistant": "SAY", "toolResult": "RESULT", "user": "USER"}

ReadFrom = Callable[[str, int], bytes]  # a session file's bytes from an offset on


class Block(BaseModel):
    """One block of a session message: text, thinking or a tool call."""

    type: str
    text: str = ""
    thinking: str = ""
    name: str = ""
    arguments: JsonValue = None


class Message(BaseModel):
    """A session line's message."""

    role: str
    content: str | list[Block] | None = None


class SessionLine(BaseModel):
    """One line of a pi session file."""

    type: str
    timestamp: str = ""
    message: Message | None = None


class Step(NamedTuple):
    """One thing an agent did in its session."""

    kind: str
    text: str


def read_steps(line: str | bytes, width: int) -> list[Step]:
    """The steps in one line of a session file, each on one line of at most ``width`` characters.

    A line that carries no step of the agent's (the session's header, a model change, its system prompt)
    gives none.
    """
    entry = SessionLine.model_validate_json(line)
    message = entry.message
    if entry.type != "message" or message is None or message.role == "system":
        return []
    content = message.content
    blocks = content if isinstance(content, list) else [Block(type="text", text=content or "")]
    steps = []
    for block in blocks:
        if block.type == "toolCall":
            step = Step("CALL", f"{block.name} {json.dumps(block.arguments)}")
        elif block.type == "thinking":
            step = Step("THINK", block.thinking)
        else:
            step = Step(TEXT_KINDS.get(message.role, message.role.upper()), block.text)
        steps.append(Step(step.kind, " ".join(step.text.split())[:width]))
    return [step for step in steps if step.text]


def locate_episode() -> str | None:
    """The name of a running stack run's episode container, or None when no run plays."""
    listed = subprocess.run(
        [
            *["docker", "ps", "--format", "{{.Names}}"],
            *["--filter", f"label=com.docker.compose.service={EPISODE_SERVICE}"],
            *["--filter", f"name={PROJECT_PREFIX}"],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return next(iter(listed.stdout.split()), None)


def list_container_sessions(container: str) -> list[str]:
    """Every session file in ``container``'s output directory; none once the container has stopped."""
    found = subprocess.run(
        ["docker", "exec", container, "find", str(OUTPUT_DIRECTORY), "-path", f"*/{SESSION_GLOB}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return sorted(found.stdout.split())


def read_from_container(container: str, path: str, offset: int) -> bytes:
    """The bytes of ``path`` in ``container`` from ``offset`` on; none once the container has stopped."""
    read = subprocess.run(
        ["docker", "exec", container, "tail", "-c", f"+{offset + 1}", path],
        capture_output=True,
        check=False,
    )
    return read.stdout


def read_from_directory(path: str, offset: int) -> bytes:
    """The bytes of the file ``path`` from ``offset`` on."""
    with Path(path).open("rb") as file:
        file.seek(offset)
        return file.read()


def describe_new_steps(
    sessions: list[str],
    read_from: ReadFrom,
    offsets: dict[str, int],
    agent: str | None,
    width: int,
) -> Iterator[str]:
    """A line for each step ``sessions`` gained since ``offsets``, which it moves past the lines it read."""
    for session in sessions:
        home = PurePosixPath(session).parents[len(SESSION_FILE.parts) - 1]
        phase = home.parent.parent.name
        if agent not in (None, home.name):
            continue
        lines = read_from(session, offsets.get(session, 0)).split(b"\n")
        # A line still being written has no newline yet: it is read on a later pass.
        offsets[session] = offsets.get(session, 0) + sum(len(line) + 1 for line in lines[:-1])
        for line in filter(None, lines[:-1]):
            clock = SessionLine.model_validate_json(line).timestamp[11:19]
            for step in read_steps(line, width):
                yield f"{clock} {PHASE_PLAYS.get(phase, phase):6} {home.name:20} {step.kind:6} {step.text}"


def watch_run(directory: Path | None, *, agent: str | None, width: int, poll_seconds: float) -> None:
    """Print each agent's steps: of the run in ``directory`` once, or of the run playing until interrupted.

    With no directory it waits for a stack run's episode container and follows it, through both plays and
    into the next run.
    """
    offsets: dict[str, int] = {}
    if directory is not None:
        sessions = sorted(str(path) for path in directory.glob(SESSION_GLOB))
        for line in describe_new_steps(sessions, read_from_directory, offsets, agent, width):
            print(line, flush=True)
        return
    while True:
        if container := locate_episode():
            sessions = list_container_sessions(container)
            read_from = functools.partial(read_from_container, container)
            for line in describe_new_steps(sessions, read_from, offsets, agent, width):
                print(line, flush=True)
        time.sleep(poll_seconds)
