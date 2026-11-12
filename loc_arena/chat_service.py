"""The chat service as a stack run's live service: Meridian's chat, which is company code.

A stack run starts it as the scenario's ``chat`` entry: ``python -m loc_arena.chat_service <run config> <entry
name>``. It serves ``meridian_controlplane.chat`` under central IAM, which decides every call, on one port:
the MCP tools at ``/mcp``, which agents reach through the tools gateway, and the HTTP API beside them, which
they call from their sandbox with their credential on the service. Each HTTP request is one line of the audit
record and each tool call one line of the tool-call record, both on stdout; the ``message`` events monitors
read are built from them after play. A new container replays the scenario's chat seed into a new database, so
the honest twin's recreated service starts where the episode's did; agents' edits to the package go live when
it is deployed (``docs/deploy-service.md``).
"""

from __future__ import annotations

import os
import sys
from typing import Any

import uvicorn
from starlette.applications import Starlette

from loc_arena.config import RunConfig, load_run_config
from loc_arena.iam_wiring import build_service_app, guard_from_environment, iam_options, load_iam_authority
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token
from loc_arena.task import resolve_scenario
from loc_arena.tool_records import Write, record_writer, tool_call_recorder
from meridian_controlplane.chat import (
    ChatSeed,
    ChatStore,
    build_chat_server,
    create_chat_api,
    load_chat_seed,
    open_seeded_store,
)

SEED_KEY = "chat_seed"  # the key of scenario.yaml naming the seed file, relative to the pack


def open_chat_store(config: RunConfig) -> ChatStore:
    """The chat of ``config``'s run: its database, replayed from the scenario's seed if there is none."""
    scenario = resolve_scenario(config)
    seed = load_chat_seed(scenario.directory / scenario.meta[SEED_KEY])
    if (overlay := config.settings.chat.seed_overlay) is not None:
        seed = _lay_over(seed, load_chat_seed(scenario.directory / overlay))
    return open_seeded_store(config.settings.chat.database, seed, [agent.id for agent in config.agents])


def _lay_over(seed: ChatSeed, overlay: ChatSeed) -> ChatSeed:
    """``seed`` with each channel ``overlay`` names in place of its own of that name; its messages follow."""
    channels = {channel.name: channel for channel in overlay.channels}
    return ChatSeed(
        people=list(dict.fromkeys([*seed.people, *overlay.people])),
        channels=[channels.pop(channel.name, channel) for channel in seed.channels] + list(channels.values()),
        direct_messages=[*seed.direct_messages, *overlay.direct_messages],
    )


def build_chat_app(
    config: RunConfig,
    service: str,
    guard: Any,  # noqa: ANN401 - a meridian_iam Guard
    write: Write,
) -> Starlette:
    """The app entry ``service`` of ``config`` serves under ``guard``: tools at ``/mcp``, the API beside them.

    ``write`` takes each tool call's record and each request's audit line.
    """
    store = open_chat_store(config)
    api = create_chat_api(store, write, load_iam_authority(guard))
    options = iam_options(guard, service)
    recorder = tool_call_recorder(caller_from_token, write, options["annotate"])
    server = build_chat_server(
        store,
        caller_from_token,
        options["verifier"],
        [recorder, *options["extra_middleware"]],
    )
    return build_service_app(guard, server, service, api)


def main(arguments: list[str]) -> None:
    """Serve entry ``arguments[1]`` of the run config at ``arguments[0]``, under central IAM."""
    config = load_run_config(arguments[0])
    entry = next(service for service in config.live_services if service.name == arguments[1])
    guard = guard_from_environment(os.environ)  # IAM decides every call: no identity or rights table here
    write = record_writer(config.settings.stack.records_directory, entry.name)
    uvicorn.run(
        build_chat_app(config, entry.name, guard, write),
        host=ALL_ADDRESSES,
        port=entry.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
