"""Subagents: a task an agent hands to a pi session like its own, started on its ``delegate`` call.

An agent whose scope allows ``spawn_subagent`` calls ``delegate`` on the workspace service, behind the
tools gateway, when the run config defines a kind of subagent (``loc_arena.subagent_config``). The episode
starts the subagent as it starts an agent (``loc_arena.own_sessions``): one pi process in RPC mode with a
home of its own, which runs nothing itself. The call waits for its answer, reporting progress so pi's MCP
timeout does not end it, and play waits while a subagent works.

A subagent of an agent, or of the agent's subagent, takes the next of the agent's ids (``<agent>.<n>``). It
calls its model on its own lane of the agent's route, counted in the agent's turn budget, and its tools with a
key of its own, keyed by the episode's key, which no agent holds. The tools gateway admits that key as the
agent, with the agent's identities, and as the subagent, with its tools, the caller its log names. Its
session's header names its caller's session as its parent, and ``SUBAGENTS_FILE`` holds each one's task,
tools, answer and end.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Annotated

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import Field, JsonValue

from loc_arena.config import RunConfig
from loc_arena.mcp_service import caller_from_token
from loc_arena.own_sessions import (
    LOOPS_DIRECTORY,
    PI_ENVIRONMENT,
    SESSION_FILE,
    STOP_SECONDS,
    Child,
    PiSession,
    SessionEnd,
    loop_working_directory,
    write_pi_home,
)
from loc_arena.subagent_config import DELEGATE_TOOL, GENERAL_PURPOSE, describe_kinds, name_lanes
from loc_arena.tool_list_notifier import NOTIFIER_KEY_SECRET_NAME
from loc_arena.tool_offer import list_subagent_tools
from loc_arena.tool_records import append_to, record_ended
from loc_arena.workspace_service import subagent_token

SUBAGENTS_FILE = "subagents.jsonl"  # in a play's directory: each subagent's start and end


class Subagents:
    """The subagents of one play, of the agents ``briefs`` holds a brief of each kind for."""

    def __init__(self, config: RunConfig, briefs: Mapping[str, Mapping[str, str]], directory: Path) -> None:
        """Subagents of ``briefs``' agents, by kind, kept in the play's ``directory``; none without briefs."""
        self._config, self._briefs = config, briefs
        self._homes, self._record = directory / LOOPS_DIRECTORY, append_to(directory / SUBAGENTS_FILE)
        self._ids = [child for child, agent in name_lanes(config).items() if agent in briefs]
        self._parents: dict[str, str] = {}  # each subagent's caller
        self._tools: dict[str, tuple[str, ...]] = {}  # each subagent's tools
        self._models: dict[str, str | None] = {}  # each subagent's model, none: its agent's
        self._working: dict[str, PiSession] = {}
        self._ending: list[PiSession] = []  # what play's end stopped, still to exit
        self._lock = threading.Lock()
        self._ended = False

    def list_tokens(self) -> dict[str, str]:
        """Each id a subagent may take, with its key at the tools gateway."""
        if not self._ids:
            return {}
        key = (self._config.settings.gateway.secrets_dir / NOTIFIER_KEY_SECRET_NAME).read_text().strip()
        return {child: subagent_token(key, child) for child in self._ids}

    def start(
        self,
        caller: str,
        kind: str | None,
        task: str,
        model: str | None = None,
    ) -> tuple[str, PiSession] | str:
        """Start a ``kind`` subagent of ``caller`` on ``task`` and ``model``: its id and session, or why not.

        No ``kind`` is general-purpose; no ``model`` is the agent's own.
        """
        agent, kind = caller.partition(".")[0], kind or GENERAL_PURPOSE
        if model is None:  # a subagent's own subagent runs on its caller's model
            model = self._models.get(caller)
        if model == self._config.loop_model(agent).model:  # its agent's own: the default
            model = None
        with self._lock:
            if refusal := self._refuse(caller, agent, kind, task, model):
                return refusal
            child = next(child for child in self._free(agent))
            self._parents[child] = caller
            delegates = self._depth(child) < self._config.subagents.max_depth
            tools = list_subagent_tools(self._config, agent, [kind], delegates=delegates)
            if caller in self._tools:  # a subagent's own subagent holds no tool its caller lacks
                tools = tuple(tool for tool in tools if tool in self._tools[caller])
            self._tools[child], self._models[child] = tools, model
            started: dict[str, JsonValue] = {
                "subagent": child,
                "agent": agent,
                "parent": caller,
                "kind": kind,
                "model": model or self._config.loop_model(agent).model,
            }
            self._write(started | {"task": task, "tools": list(tools)})
            try:
                session = self._launch(Child(child, self.list_tokens()[child], tools, model), caller, kind)
            except (OSError, KeyError, ValueError) as error:  # its home or its pi could not be set up
                self._write(
                    {"subagent": child, "end": {"reason": "failed", "stderr": str(error)}, "answer": ""},
                )
                return f"{child} could not start: {error}"
            self._working[child] = session
            if not session.prompt(task):
                self._working.pop(child)
                self._write_end(child, session, session.stop())
                return f"{child} could not start: its pi took no task"
            return child, session

    def finish(self, child: str) -> SessionEnd:
        """End ``child``'s session once it answered, or its call ended, and record how it ended."""
        with self._lock:
            session = self._working.pop(child, None)
        if session is None:  # ended with play
            return SessionEnd("ceiling")
        end = session.stop()
        session.wait(STOP_SECONDS)
        self._write_end(child, session, end)
        return end

    def prompt(self, child: str, message: str) -> bool:
        """Give ``message`` to ``child``'s session while it works; whether it took it."""
        with self._lock:
            session = self._working.get(child)
        return session is not None and session.prompt(message)

    def is_working(self) -> bool:
        """Whether a subagent still works."""
        with self._lock:
            return any(session.is_working() for session in self._working.values())

    def end(self) -> None:
        """Start no more subagents, and stop each still working, recording its end; ``wait`` for them."""
        with self._lock:
            self._ended = True
            ending, self._working = self._working, {}
        for child, session in ending.items():
            self._write_end(child, session, session.stop())
        self._ending = list(ending.values())

    def wait(self, seconds: float) -> None:
        """Wait for what ``end`` stopped to exit, killing what is still there after ``seconds``."""
        deadline = monotonic() + seconds
        for session in self._ending:
            session.wait(deadline - monotonic())

    def serve(self, server: MCPServer) -> None:
        """Serve ``delegate`` on ``server``, when an agent has subagents to start."""
        if self._briefs:
            add_delegate_tool(server, self, self._config)

    def _free(self, agent: str) -> list[str]:
        return [child for child in self._ids if child.startswith(f"{agent}.") and child not in self._parents]

    def _refuse(self, caller: str, agent: str, kind: str, task: str, model: str | None) -> str | None:
        """Why ``caller`` may not start a ``kind`` subagent on ``model`` now, the first bound it passes."""
        bounds, kinds = self._config.subagents, self._briefs.get(agent, {})
        working = sum(child.partition(".")[0] == agent for child in self._working)
        refusals = (
            (self._ended, "play has ended"),
            (not kinds or self._depth(caller) >= bounds.max_depth, f"{caller} may not start subagents"),
            (
                len(task) > bounds.max_task_characters,
                f"a task holds at most {bounds.max_task_characters} characters",
            ),
            (
                model is not None and model not in bounds.models,
                f"model {model!r} is not one this run allows: {', '.join(bounds.models) or 'none'}",
            ),
            (kind not in kinds, f"no subagent kind {kind!r}: {', '.join(kinds)}"),
            (
                working >= bounds.max_concurrent,
                f"{bounds.max_concurrent} subagents of {agent}'s already work: wait for one to answer",
            ),
            (not self._free(agent), f"{agent} has started its {bounds.max_children} subagents of this play"),
        )
        return next((message for refused, message in refusals if refused), None)

    def _launch(self, child: Child, caller: str, kind: str) -> PiSession:
        """``child``'s pi, its home written, its session headed by its caller's as its parent."""
        home, agent = self._homes / child.id, caller.partition(".")[0]
        pi = write_pi_home(home, self._config, agent, self._briefs[agent][kind], child)
        working = loop_working_directory(child.id)
        header = {
            "type": "session",
            "version": 3,
            "id": child.id,
            "timestamp": datetime.now(UTC).isoformat(),
            "cwd": str(working),
            "parentSession": str(self._homes / caller / SESSION_FILE),
        }
        (home / SESSION_FILE).write_text(json.dumps(header) + "\n")  # pi's own link to a parent session
        return PiSession(pi, home, working, {"PATH": os.environ["PATH"], "HOME": str(home), **PI_ENVIRONMENT})

    def _write_end(self, child: str, session: PiSession, end: SessionEnd) -> None:
        self._write(
            {
                "subagent": child,
                "end": {"reason": end.reason, "stderr": end.stderr},
                "answer": session.answer(),
            },
        )

    def _write(self, fields: Mapping[str, JsonValue]) -> None:
        self._record(json.dumps({"time": datetime.now(UTC).isoformat(), **fields}))

    def _depth(self, caller: str) -> int:
        depth = 0
        while caller in self._parents:
            caller, depth = self._parents[caller], depth + 1
        return depth


def add_delegate_tool(server: MCPServer, subagents: Subagents, config: RunConfig) -> None:
    """Serve ``delegate`` on ``server``: it starts a subagent and answers with what the subagent answered.

    The call wakes every ``pi.notice_poll_seconds`` and reports progress every ``subagents.progress_seconds``.
    However it ends, its subagent's session ends with it and is recorded.
    """
    bounds, poll = config.subagents, config.settings.pi.notice_poll_seconds
    kinds = "; ".join(f"{name}: {description}" for name, description in describe_kinds(config).items())
    models = ", ".join(bounds.models) or "none"

    @server.tool(
        name=DELEGATE_TOOL,
        description=f"Hand one self-contained task to a new subagent, which works on your checkout with your "
        f"access and answers when done; you wait for its answer. Kinds: {kinds}; none is {GENERAL_PURPOSE}. "
        f"Models beside your own, the default: {models}. At most {bounds.max_concurrent} at once and "
        f"{bounds.max_children} in all.",
    )
    async def delegate(
        task: Annotated[str, Field(description="everything it needs to know to do the task")],
        context: Context,
        kind: Annotated[str | None, Field(description="the subagent's kind; none: general-purpose")] = None,
        model: Annotated[str | None, Field(description="the subagent's model; none: your own")] = None,
    ) -> dict[str, JsonValue]:
        caller = caller_from_token()
        started = subagents.start(caller, kind, task, model) if caller else "no identity"
        if isinstance(started, str):
            return {"error": started}
        child, session = started
        cut = False
        try:
            reported = monotonic()
            while session.is_working():
                if monotonic() - reported >= bounds.progress_seconds:
                    reported = monotonic()
                    await context.report_progress(reported, None, f"{child} is working")  # keeps pi's call
                await anyio.sleep(poll)
        except anyio.get_cancelled_exc_class():
            cut = True
            raise
        finally:
            with anyio.CancelScope(shield=True):  # a call cut short still ends its subagent, and is recorded
                end = await anyio.to_thread.run_sync(subagents.finish, child)
                if cut:  # the record writes no line of a cancelled call by itself
                    record_ended({"subagent": child, "answer": session.answer(), "end": end.reason})
        return {"subagent": child, "answer": session.answer(), "end": end.reason}
