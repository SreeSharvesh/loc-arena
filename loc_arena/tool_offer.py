"""What the tools gateway offers each agent: every tool its run config names, before play and after.

The gateway serves a session with the targets that answer when it opens (``failureMode: failOpen``), and pi
keeps a session's list until it is told the list changed. A play therefore starts only once a session opened
as each agent lists every tool that agent's ``sandbox: tools:`` names, beside the workspace service's. What
each agent was offered, what it lacked and why a listing failed are written to the play's directory either
way; a play refused fails its run. Once a deployment ends, the same check runs again, and only once it passes
are the sessions told to list again (``loc_arena.tool_list_notifier``). After play, each session's listing
that lacked a named tool is written to the run directory.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import anyio
import httpx2
import tenacity
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from loc_arena.config import RunConfig
from loc_arena.notices import NOTICE_TOOLS
from loc_arena.recorded_events import SERVICE_LOGS
from loc_arena.tool_authorization import TOOLS_GATEWAY_SERVICE
from loc_arena.workspace_service import WORKSPACE_TOOLS

OFFERED_TOOLS_FILE = "tools_offered.json"  # in the play's directory: each agent's offered and missing tools
LACKED_TOOLS_FILE = "tools_lacked.jsonl"  # in the run directory: each tool a session's listing lacked
_LOGGER = logging.getLogger(__name__)


class MissingToolsError(RuntimeError):
    """The tools gateway did not offer an agent every tool its run config names, so play does not start."""


def name_tools(config: RunConfig) -> dict[str, set[str]]:
    """Each agent's tools its run config names, beside the workspace service's and its subscriptions'."""
    return {
        agent.id: {
            *WORKSPACE_TOOLS,
            *NOTICE_TOOLS,
            *(tool for tools in agent.sandbox.tools.values() for tool in tools),
        }
        for agent in config.agents
    }


def await_offered_tools(config: RunConfig, url: str, tokens: Mapping[str, str], directory: Path) -> None:
    """Wait until the gateway at ``url`` offers each agent of ``tokens`` every tool its run config names.

    Writes ``OFFERED_TOOLS_FILE`` to ``directory``, then raises ``MissingToolsError`` naming each tool still
    missing.
    """
    missing, errors = _wait_until_offered(config, url, tokens)
    offered = {
        agent: {"named": sorted(tools), "missing": missing[agent], "error": errors[agent]}
        for agent, tools in name_tools(config).items()
        if agent in tokens
    }
    (directory / OFFERED_TOOLS_FILE).write_text(json.dumps(offered, indent=2))
    if lacking := {agent: tools for agent, tools in missing.items() if tools}:
        raise MissingToolsError(f"the tools gateway did not offer these tools in time: {lacking}")


def notify_once_offered(
    config: RunConfig,
    url: str,
    tokens: Mapping[str, str],
    notify: Callable[[], None],
) -> None:
    """``notify`` once the gateway at ``url`` offers each agent of ``tokens`` every tool it names again.

    So a session told to list its tools again never lists while a target is down. When one stays missing,
    nothing is sent, and the run's check after play names each session's listing that lacked a tool.
    """
    missing, errors = _wait_until_offered(config, url, tokens)
    if lacking := {agent: tools for agent, tools in missing.items() if tools}:
        _LOGGER.warning("no tool-list notice: the gateway still does not offer %s (%s)", lacking, errors)
    else:
        notify()


def _wait_until_offered(
    config: RunConfig,
    url: str,
    tokens: Mapping[str, str],
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """What a new session as each agent of ``tokens`` is not offered, once it is offered all or time is up.

    Each agent's listing is a new session, opened with its sandbox token, read again every
    ``stack.gateway_health_interval_seconds`` for at most ``stack.tools_offer_seconds``, which also bounds
    each request of a listing. With each missing tool, by agent, why each agent's last listing failed.
    """
    stack = config.settings.stack
    named = {agent: tools for agent, tools in name_tools(config).items() if agent in tokens}
    errors: dict[str, str] = {}

    def list_missing() -> dict[str, list[str]]:
        missing = {}
        for agent, tools in named.items():
            listed, errors[agent] = _list_tools(url, tokens[agent], stack.tools_offer_seconds)
            missing[agent] = sorted(tools - listed)
        return missing

    missing = tenacity.Retrying(
        stop=tenacity.stop_after_delay(stack.tools_offer_seconds),
        wait=tenacity.wait_fixed(stack.gateway_health_interval_seconds),
        retry=tenacity.retry_if_result(lambda missing: any(missing.values())),
        retry_error_callback=lambda state: state.outcome.result() if state.outcome else None,
    )(list_missing)
    return missing, errors


class _Listing(BaseModel):
    """The gateway's line of a request: who made it, when, its session, and the tools a listing offered."""

    model_config = ConfigDict(extra="ignore")

    time: AwareDatetime | None = None
    agent: str | None = None
    session: str | None = Field(default=None, validation_alias="mcp.session.id")
    tools: list[str] | None = Field(default=None, validation_alias="tools_listed")


def report_lacked_tools(config: RunConfig, run_directory: Path) -> None:
    """Write each tool a session's listing lacked of those its agent's run config names, and warn of any.

    Read from the tools gateway's log in ``run_directory``, one line of ``LACKED_TOOLS_FILE`` per agent, tool
    and time. A listing outside a session is a check's, before play or before a notice: no agent held it.
    """
    named = name_tools(config)
    log = run_directory / SERVICE_LOGS / f"{TOOLS_GATEWAY_SERVICE}.log"
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    lacked = [
        {"time": listing.time.isoformat(), "agent": listing.agent, "session": listing.session, "tool": tool}
        for listing in _read_listings(lines)
        if listing.time and listing.agent and listing.session and listing.tools is not None
        for tool in sorted(named.get(listing.agent, set()) - set(listing.tools))
    ]
    (run_directory / LACKED_TOOLS_FILE).write_text("".join(f"{json.dumps(line)}\n" for line in lacked))
    if lacked:
        _LOGGER.warning("%d tools were missing from agents' listings: see %s", len(lacked), LACKED_TOOLS_FILE)


def _read_listings(lines: list[str]) -> Iterator[_Listing]:
    """Each of ``lines`` read by its own fields alone, as the tool arguments it records may name any key.

    A line that is no JSON object is skipped: a long tool call's line is split, and no listing's is that long.
    """
    for line in lines:
        with contextlib.suppress(ValidationError):
            yield _Listing.model_validate_json(line)


def _list_tools(url: str, token: str, seconds: float) -> tuple[set[str], str]:
    """The names of the tools a new session at ``url`` with ``token`` lists, every page; and why it could not.

    Each request may take ``seconds``: the gateway answers a session only once it has given up on a target
    that does not answer. A session that cannot open lists nothing, and the error says why, by its type and
    message alone.
    """

    async def list_tools() -> set[str]:
        async with (
            httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=seconds) as http,
            Client(streamable_http_client(url, http_client=http)) as client,
        ):
            names, cursor = set(), None
            while True:
                page = await client.list_tools(cursor=cursor)
                names |= {tool.name for tool in page.tools}
                if not (cursor := page.next_cursor):
                    return names

    try:
        return anyio.run(list_tools), ""
    except Exception as error:  # noqa: BLE001 - a gateway not yet serving lists nothing, and is asked again
        while isinstance(error, ExceptionGroup) and error.exceptions:  # the MCP client's task groups wrap it
            error = error.exceptions[0]
        return set(), f"{type(error).__name__}: {error}"
