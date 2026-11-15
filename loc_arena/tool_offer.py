"""What the tools gateway offers each agent before its session starts: every tool its run config names.

The gateway serves a session with the targets that answer when it opens (``failureMode: failOpen``), so a
session opened while a target is down or not yet serving holds none of that target's tools, for good, and its
agent is not told. A play therefore starts only once a session opened as each agent lists every tool that
agent's ``sandbox: tools:`` names, beside the workspace service's. What each agent was offered, and what it
lacked, is written to the play's directory either way.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import anyio
import httpx2
import tenacity
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from loc_arena.config import RunConfig
from loc_arena.notices import NOTICE_TOOLS
from loc_arena.workspace_service import WORKSPACE_TOOLS

OFFERED_TOOLS_FILE = "tools_offered.json"  # in the play's directory: each agent's offered and missing tools


class MissingToolsError(RuntimeError):
    """The tools gateway did not offer an agent every tool its run config names, so play does not start."""


def await_offered_tools(config: RunConfig, url: str, tokens: Mapping[str, str], directory: Path) -> None:
    """Wait until the gateway at ``url`` offers each agent of ``tokens`` every tool its run config names.

    Each agent's listing is a new session, opened with its sandbox token, read again every
    ``stack.gateway_health_interval_seconds`` for at most ``stack.tools_offer_seconds``. Writes
    ``OFFERED_TOOLS_FILE`` to ``directory``, then raises ``MissingToolsError`` naming each tool still missing.
    """
    stack = config.settings.stack
    named = {
        agent.id: {
            *WORKSPACE_TOOLS,
            *NOTICE_TOOLS,
            *(tool for tools in agent.sandbox.tools.values() for tool in tools),
        }
        for agent in config.agents
        if agent.id in tokens
    }

    def list_missing() -> dict[str, list[str]]:
        return {agent: sorted(tools - _list_tools(url, tokens[agent])) for agent, tools in named.items()}

    missing = tenacity.Retrying(
        stop=tenacity.stop_after_delay(stack.tools_offer_seconds),
        wait=tenacity.wait_fixed(stack.gateway_health_interval_seconds),
        retry=tenacity.retry_if_result(lambda missing: any(missing.values())),
        retry_error_callback=lambda state: state.outcome.result() if state.outcome else None,
    )(list_missing)
    offered = {agent: {"named": sorted(tools), "missing": missing[agent]} for agent, tools in named.items()}
    (directory / OFFERED_TOOLS_FILE).write_text(json.dumps(offered, indent=2))
    if lacking := {agent: tools for agent, tools in missing.items() if tools}:
        raise MissingToolsError(f"the tools gateway did not offer these tools in time: {lacking}")


def _list_tools(url: str, token: str) -> set[str]:
    """The names of the tools a new session at ``url`` with ``token`` lists; none if it cannot open one."""

    async def list_tools() -> set[str]:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http,
            Client(streamable_http_client(url, http_client=http)) as client,
        ):
            return {tool.name for tool in (await client.list_tools()).tools}

    try:
        return anyio.run(list_tools)
    except Exception:  # noqa: BLE001 - a gateway not yet serving lists nothing, and is asked again
        return set()
