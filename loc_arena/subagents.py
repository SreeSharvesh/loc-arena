"""Subagents: a task an agent hands to a pi session like its own, started on its ``delegate`` call.

An agent whose scope allows ``spawn_subagent`` calls ``delegate`` on the workspace service, behind the
tools gateway, when the run config defines a kind of subagent (``loc_arena.subagent_config``). The episode
starts the subagent as it starts an agent (``loc_arena.own_sessions``): one pi process in RPC mode with a
home of its own, which runs nothing itself. The call waits for its answer, reporting progress so pi's MCP
timeout does not end it, and play waits while a subagent works.

A subagent of an agent, or of the agent's subagent, takes the next of the agent's ids (``<agent>.<n>``). It
calls its model on its own lane of the agent's route, counted in the agent's turn budget, and its tools with a
key of its own, which the tools gateway admits as the agent, with the agent's tools and identities, naming the
subagent as the caller. Its session's header names its caller's session as its parent, and ``SUBAGENTS_FILE``
holds each one's task, tools, answer and end.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from pydantic import Field, JsonValue

from loc_arena.config import RunConfig
from loc_arena.mcp_service import caller_from_token
from loc_arena.notices import NOTICE_TOOLS
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
from loc_arena.sandbox import token_secret_name
from loc_arena.session_events import SUBAGENTS_FILE
from loc_arena.tool_offer import list_agent_tools
from loc_arena.tool_records import append_to
from loc_arena.workspace_service import DELEGATE_TOOL, name_children, subagent_token


class Subagents:
    """The subagents of one play, of the agents ``briefs`` holds a brief of each kind for."""

    def __init__(self, config: RunConfig, briefs: Mapping[str, Mapping[str, str]], directory: Path) -> None:
        """Subagents of ``briefs``' agents, by kind, kept in the play's ``directory``; none without briefs."""
        self._config, self._briefs = config, briefs
        self._homes, self._record = directory / LOOPS_DIRECTORY, append_to(directory / SUBAGENTS_FILE)
        self._ids = {agent: list(name_children(agent, config.subagents.max_children)) for agent in briefs}
        self._parents: dict[str, str] = {}  # each subagent's caller
        self._working: dict[str, PiSession] = {}
        self._lock = threading.Lock()
        self._ended = False

    def children(self) -> dict[str, tuple[str, ...]]:
        """By agent, the ids its subagents may take."""
        return {agent: name_children(agent, self._config.subagents.max_children) for agent in self._briefs}

    def start(self, caller: str, kind: str, task: str) -> tuple[str, PiSession] | str:
        """Start a ``kind`` subagent of ``caller`` on ``task``: its id and session, or why it did not."""
        bounds, agent = self._config.subagents, caller.partition(".")[0]
        with self._lock:
            depth = self._depth(caller)
            if self._ended:
                return "play has ended"
            if agent not in self._briefs or depth >= bounds.max_depth:
                return f"{caller} may not start subagents"
            if kind not in self._briefs[agent]:
                return f"no subagent kind {kind!r}: {', '.join(self._briefs[agent])}"
            if sum(child.partition(".")[0] == agent for child in self._working) >= bounds.max_concurrent:
                return f"{bounds.max_concurrent} subagents of {agent}'s already work: wait for one to answer"
            if not self._ids[agent]:
                return f"{agent} has started its {bounds.max_children} subagents of this play"
            child = self._ids[agent].pop(0)
            self._parents[child] = caller
            tools = self._list_tools(agent, kind, depth + 1 < bounds.max_depth)
            self._working[child] = self._launch(
                Child(child, self._derive_token(agent, child), tools),
                caller,
                kind,
            )
            self._working[child].prompt(task)
            started: dict[str, JsonValue] = {
                "subagent": child,
                "agent": agent,
                "parent": caller,
                "kind": kind,
                "task": task,
                "tools": list(tools),
            }
            self._write(started)
            return child, self._working[child]

    def finish(self, child: str) -> SessionEnd:
        """End ``child``'s session once it answered, and record how it ended."""
        with self._lock:
            session = self._working.pop(child, None)
        if session is None:  # ended with play
            return SessionEnd("ceiling")
        end = session.stop()
        session.wait(STOP_SECONDS)
        self._write(
            {
                "subagent": child,
                "end": {"reason": end.reason, "stderr": end.stderr},
                "answer": session.answer(),
            },
        )
        return end

    def is_working(self) -> bool:
        """Whether a subagent still works."""
        with self._lock:
            return any(session.is_working() for session in self._working.values())

    def end(self) -> None:
        """Start no more subagents, and end each still working."""
        with self._lock:
            self._ended = True
            working = list(self._working)
        for child in working:
            self.finish(child)

    def serve(self, server: MCPServer) -> None:
        """Serve ``delegate`` on ``server``, when an agent has subagents to start."""
        if self._briefs:
            add_delegate_tool(server, self, self._config)

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
            "parentSession": str(Path(caller) / SESSION_FILE),  # beside it, in the same directory of homes
        }
        (home / SESSION_FILE).write_text(json.dumps(header) + "\n")  # pi's own link to a parent session
        return PiSession(pi, home, working, {"PATH": os.environ["PATH"], "HOME": str(home), **PI_ENVIRONMENT})

    def _derive_token(self, agent: str, child: str) -> str:
        """``child``'s key at the tools gateway, derived from its agent's sandbox token."""
        secrets = self._config.settings.gateway.secrets_dir
        return subagent_token((secrets / token_secret_name(agent)).read_text().strip(), child)

    def _write(self, fields: Mapping[str, JsonValue]) -> None:
        self._record(json.dumps({"time": datetime.now(UTC).isoformat(), **fields}))

    def _depth(self, caller: str) -> int:
        depth = 0
        while caller in self._parents:
            caller, depth = self._parents[caller], depth + 1
        return depth

    def _list_tools(self, agent: str, kind: str, delegates: bool) -> tuple[str, ...]:
        """The tools a ``kind`` subagent of ``agent`` gets: its kind's, or the agent's but subscriptions."""
        held = list_agent_tools(self._config.agent(agent), spawns=delegates)
        named = self._config.subagents.definitions[kind].tools
        return tuple(
            tool
            for tool in held
            if tool not in NOTICE_TOOLS and (named is None or tool in named or tool == DELEGATE_TOOL)
        )


def add_delegate_tool(server: MCPServer, subagents: Subagents, config: RunConfig) -> None:
    """Serve ``delegate`` on ``server``: it starts a subagent and answers with what the subagent answered."""
    bounds = config.subagents
    kinds = "; ".join(f"{name}: {kind.description}" for name, kind in bounds.definitions.items())

    @server.tool(
        name=DELEGATE_TOOL,
        description=f"Hand one self-contained task to a new subagent, which works on your checkout with your "
        f"access and answers when done; you wait for its answer. Kinds: {kinds}. At most "
        f"{bounds.max_concurrent} at once and {bounds.max_children} in all.",
    )
    async def delegate(
        kind: Annotated[str, Field(description="the subagent's kind")],
        task: Annotated[str, Field(description="everything it needs to know to do the task")],
        context: Context,
    ) -> dict[str, JsonValue]:
        caller = caller_from_token()
        started = subagents.start(caller, kind, task) if caller else "no identity"
        if isinstance(started, str):
            return {"error": started}
        child, session = started
        ticks = 0
        while session.is_working():
            ticks += 1
            await context.report_progress(ticks, None, f"{child} is working")  # keeps the call alive in pi
            await anyio.sleep(bounds.progress_seconds)
        end = await anyio.to_thread.run_sync(subagents.finish, child)
        return {"subagent": child, "answer": session.answer(), "end": end.reason}
