"""The model store as a stack run's live service: the weights of each model Meridian serves, as company code.

A stack run starts it as the scenario's ``modelstore`` entry: ``python -m loc_arena.modelstore_service <run
config> <entry name>``. It serves ``meridian_controlplane.modelstore`` under central IAM, which decides every
call, on one port: the MCP tools at ``/mcp``, which agents reach through the tools gateway, and the HTTP API
beside them, which they and their jobs call with their credential on the service. Each HTTP request is one
line of the audit record and each tool call one line of the tool-call record, both on stdout. Each process
writes the shards of every model in ``evalkit.models`` afresh, with new model keys, so the honest twin's
recreated store holds the same models; agents' edits to the package do not go live.
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
from loc_arena.tickets_service import write_record_line
from loc_arena.tool_records import Write, tool_call_recorder
from meridian_controlplane.modelstore import ModelStore, Sizes, build_modelstore_server, create_modelstore_api


def open_model_store(config: RunConfig) -> ModelStore:
    """The weights of every model ``config``'s run serves, sharded as its ``modelstore:`` settings say."""
    sizes = config.settings.modelstore
    return ModelStore(
        config.settings.evalkit.models,
        Sizes(sizes.shard_count, sizes.shard_bytes, sizes.declared_shard_bytes),
    )


def build_modelstore_app(
    config: RunConfig,
    service: str,
    guard: Any,  # noqa: ANN401 - a meridian_iam Guard
    write: Write,
) -> Starlette:
    """The app entry ``service`` of ``config`` serves under ``guard``: tools at ``/mcp``, the API beside them.

    ``write`` takes each tool call's record and each request's audit line.
    """
    store = open_model_store(config)
    api = create_modelstore_api(store, write, load_iam_authority(guard))
    options = iam_options(guard, service)
    recorder = tool_call_recorder(caller_from_token, write, options["annotate"])
    server = build_modelstore_server(
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
        build_modelstore_app(config, entry.name, guard, write_record_line),
        host=ALL_ADDRESSES,
        port=entry.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
