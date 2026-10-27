"""The notes service as a stack run's live service: the team's shared notes, which are company code.

A stack run starts it as the scenario's ``notes`` entry: ``python -m loc_arena.notes_service <run config>
<entry name>``. It serves ``meridian_controlplane.notes`` under central IAM, which decides every call, on one
port: the MCP tools at ``/mcp``, which agents reach through the tools gateway, and the HTTP API beside them,
which they call from their sandbox with their credential on the service. Each HTTP request is one line of the
audit record and each tool call one line of the tool-call record, both on stdout. The notes live in memory and
each process starts from the scenario's note seed, so the honest twin's recreated service starts where the
episode's did; agents' edits to the package do not go live.
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
from loc_arena.tickets_service import write_record_line
from loc_arena.tool_records import Write, tool_call_recorder
from meridian_controlplane.notes import (
    NoteStore,
    build_notes_server,
    create_notes_api,
    load_note_seed,
    open_seeded_store,
)

SEED_KEY = "notes_seed"  # the key of scenario.yaml naming the seed file, relative to the pack


def open_notes_store(config: RunConfig) -> NoteStore:
    """The notes of ``config``'s run, as the scenario's seed starts them."""
    scenario = resolve_scenario(config)
    return open_seeded_store(load_note_seed(scenario.directory / scenario.meta[SEED_KEY]))


def build_notes_app(
    config: RunConfig,
    service: str,
    guard: Any,  # noqa: ANN401 - a meridian_iam Guard
    write: Write,
) -> Starlette:
    """The app entry ``service`` of ``config`` serves under ``guard``: tools at ``/mcp``, the API beside them.

    ``write`` takes each tool call's record and each request's audit line.
    """
    store = open_notes_store(config)
    api = create_notes_api(store, write, load_iam_authority(guard))
    options = iam_options(guard, service)
    recorder = tool_call_recorder(caller_from_token, write, options["annotate"])
    server = build_notes_server(
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
    uvicorn.run(
        build_notes_app(config, entry.name, guard, write_record_line),
        host=ALL_ADDRESSES,
        port=entry.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
