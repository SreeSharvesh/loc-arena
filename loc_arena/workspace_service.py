"""The workspace service: each agent's tools on its own checkout, over MCP, behind the tools gateway.

An agent's loop runs nothing itself: its bash and its file tools are tools of this service, as its tickets
and chat are tools of theirs. The service runs in the episode's container, on the code services
(``loc_arena.scaffold.code_services``), so a file tool reads and writes the agent's checkout and a command
runs in the agent's sandbox. It records every call (``loc_arena.tool_records``), and runs one call of an
agent at a time: the end of a command in a sandbox ends every other process there.

The tools gateway is its caller, and names the agent with that agent's workspace key, derived from the
agent's sandbox token: the gateway holds the keys, never the tokens. An agent can derive its own key, so it
can call the service as itself and nobody else: such a call is its own tool call, recorded like any other.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import threading
from collections.abc import AsyncIterator, Iterable, Iterator, Mapping
from typing import Annotated, cast

import uvicorn
from mcp.server import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import Field, JsonValue
from starlette.applications import Starlette
from starlette.routing import Mount

from loc_arena.mcp_service import ALL_ADDRESSES, build_app, caller_from_token
from loc_arena.scaffold.code_services import CodeServices
from loc_arena.subagent_config import DELEGATE_TOOL as DELEGATE_TOOL  # served beside them to a spawner
from loc_arena.tool_records import Write, tool_call_recorder

WORKSPACE_SERVICE = "workspace"  # its target at the tools gateway, and the name its records are kept under
WORKSPACE_TOOLS = ("bash", "read_file", "write_file", "edit_file", "list_dir", "search_code")
_STOP_SECONDS = 5
_PATH = Annotated[str, Field(description="path relative to the checkout")]


def workspace_key(sandbox_token: str) -> str:
    """The key that names the agent holding ``sandbox_token`` to the workspace service."""
    return hashlib.sha256(f"{WORKSPACE_SERVICE}:{sandbox_token}".encode()).hexdigest()


def subagent_token(episode_key: str, child: str, kind: str | None = None) -> str:
    """The subagent ``child``'s key, keyed by ``episode_key``, which no agent holds.

    With ``kind``, its key at the tools gateway as that kind; without, its key at the workspace service.
    """
    held = f"subagent:{child}" + (f":{kind}" if kind else "")
    return hmac.new(episode_key.encode(), held.encode(), hashlib.sha256).hexdigest()


class KeyVerifier(TokenVerifier):
    """Admits the holder of each key as the caller it names."""

    def __init__(self, agents: Mapping[str, str]) -> None:
        """A verifier that admits each key of ``agents`` as the caller it maps to."""
        self._agents = dict(agents)  # each caller, by its key

    async def verify_token(self, token: str) -> AccessToken | None:
        """The caller ``token`` names, or none for a key it does not hold."""
        for key, agent in self._agents.items():
            if hmac.compare_digest(key.encode(), token.encode()):
                return AccessToken(token=token, client_id=agent, scopes=[])
        return None


class Turns:
    """One call of an agent at a time, and none after the end of play."""

    def __init__(self, agents: Iterable[str]) -> None:
        """Turns for ``agents``, with play going on."""
        self._locks = {agent: threading.Lock() for agent in agents}
        self._ended = False

    def end(self) -> None:
        """Refuse every later call, and wait for each call still running: none acts after play."""
        self._ended = True
        for lock in self._locks.values():
            with lock:
                pass

    @contextlib.contextmanager
    def take(self, agent: str) -> Iterator[bool]:
        """Hold ``agent``'s turn for the block; false when play has ended, and the call must not run."""
        with self._locks[agent]:
            yield not self._ended


def build_workspace_server(
    code: CodeServices,
    sandbox_tokens: Mapping[str, str],
    write: Write,
    turns: Turns,
    child_tokens: Mapping[str, str] | None = None,
) -> MCPServer:
    """The MCP server of ``code``'s tools for the holders of ``sandbox_tokens``; ``write`` records calls.

    A subagent ``<agent>.<n>`` of ``child_tokens`` is known by its own key and recorded as itself, and works
    on its agent's checkout and sandbox, in its agent's turn of ``turns``, as every call does.
    """
    agents = {workspace_key(token): agent for agent, token in sandbox_tokens.items()} | {
        workspace_key(token): child for child, token in (child_tokens or {}).items()
    }
    server = MCPServer(
        WORKSPACE_SERVICE,
        token_verifier=KeyVerifier(agents),
        auth=AuthSettings(issuer_url=f"http://{WORKSPACE_SERVICE}", resource_server_url=None),
        middleware=[tool_call_recorder(caller_from_token, write)],
        log_level="WARNING",
    )

    def run(tool: str, **arguments: str) -> dict[str, JsonValue]:
        caller = caller_from_token()
        if caller is None:
            return {"error": "no identity: the workspace serves only authenticated callers", "tool": tool}
        agent = caller.partition(".")[0]  # a subagent's agent
        with turns.take(agent) as playing:
            if not playing:
                return {"error": "play has ended", "tool": tool}
            return cast("dict[str, JsonValue]", code.run(tool, {**arguments, "actor_uid": agent}))

    # Sync, so the SDK runs each call in a worker thread: a command takes as long as its timeout.
    @server.tool(
        description="Run a bash command in the checkout, git included. Returns its exit code and the "
        "end of its stdout and stderr.",
    )
    def bash(
        command: Annotated[str, Field(description="the command, run with bash -c")],
    ) -> dict[str, JsonValue]:
        return run("bash", command=command)

    @server.tool(description="Read a file in the checkout.")
    def read_file(path: _PATH) -> dict[str, JsonValue]:
        return run("read_file", path=path)

    @server.tool(description="Create or overwrite a file in the checkout.")
    def write_file(
        path: _PATH,
        content: Annotated[str, Field(description="the full new contents")] = "",
    ) -> dict[str, JsonValue]:
        return run("write_file", path=path, content=content)

    @server.tool(description="Replace every occurrence of old with new in a file.")
    def edit_file(
        path: _PATH,
        old: Annotated[str, Field(description="the text to replace")],
        new: Annotated[str, Field(description="the text to put in its place")] = "",
    ) -> dict[str, JsonValue]:
        return run("edit_file", path=path, old=old, new=new)

    @server.tool(description="List a directory in the checkout.")
    def list_dir(path: _PATH = "") -> dict[str, JsonValue]:
        return run("list_dir", path=path)

    @server.tool(description="Search Python files under a path for a regular expression.")
    def search_code(
        pattern: Annotated[str, Field(description="Python regular expression")],
        path: _PATH = "",
    ) -> dict[str, JsonValue]:
        return run("search_code", pattern=pattern, path=path)

    return server


@contextlib.contextmanager
def serve_workspace(
    server: MCPServer,
    host: str,
    port: int,
    mounted: Mapping[str, Starlette] | None = None,
) -> Iterator[None]:
    """Serve ``server`` at ``/mcp`` on ``port`` to requests addressed to ``host``, until the block ends.

    Each app of ``mounted`` is served on the same port under its own path. Raises ``RuntimeError`` when it
    cannot start, so no session plays without its tools. At the end, a call still open is given
    ``_STOP_SECONDS`` to finish.
    """
    apps = {**(mounted or {}), "": build_app(server, host)}

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:  # a mounted app's own lifespan runs only here
        async with contextlib.AsyncExitStack() as running:
            for app in apps.values():
                await running.enter_async_context(app.router.lifespan_context(app))
            yield

    routes = [Mount(f"/{path}" if path else "", app=app) for path, app in apps.items()]
    config = uvicorn.Config(
        Starlette(routes=routes, lifespan=lifespan),
        host=ALL_ADDRESSES,
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=_STOP_SECONDS,
    )
    serving = uvicorn.Server(config)
    thread = threading.Thread(target=serving.run, daemon=True)
    thread.start()
    try:
        while not serving.started and thread.is_alive():
            threading.Event().wait(0.05)
        if not serving.started:
            raise RuntimeError(f"the workspace service could not start on port {port}")
        yield
    finally:
        serving.should_exit = True
        thread.join()
