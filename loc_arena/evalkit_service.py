"""The eval service as a stack run's live service: Meridian's evals, runs and leaderboards, as company code.

A stack run starts it as the scenario's ``evalkit`` entry: ``python -m loc_arena.evalkit_service <run config>
<entry name>``. It serves ``meridian_evalkit.service`` under central IAM, which decides every call, on one
port: the MCP tools at ``/mcp``, which agents reach through the tools gateway, and the HTTP API beside them,
which they call from their sandbox with their credential on the service. Each HTTP request is one line of the
audit record and each tool call one line of the tool-call record, both on stdout. The registry starts from
the package's seed, and agents' edits to the package do not go live. A run's job asks the inference API for
the run's model by its Meridian name, so the service does not start when the gateway serves a seeded model
under no name (``gateway.runner_model_aliases``).
"""

from __future__ import annotations

import os
import sys

import uvicorn

from loc_arena.config import load_run_config
from loc_arena.iam_wiring import build_service_app, guard_from_environment, iam_options, load_iam_authority
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token
from loc_arena.tool_records import tool_call_recorder
from meridian_evalkit.service import (
    EvalkitRegistry,
    JobResources,
    build_evalkit_server,
    create_evalkit_api,
    load_seed,
)


def write_record_line(line: str) -> None:
    """One line of the audit record or the tool-call record, on stdout."""
    print(line, flush=True)


def main(arguments: list[str]) -> None:
    """Serve entry ``arguments[1]`` of the run config at ``arguments[0]``, under central IAM."""
    config = load_run_config(arguments[0])
    entry = next(service for service in config.live_services if service.name == arguments[1])
    settings, seed = config.settings, load_seed()
    if unserved := sorted(set(seed.models) - set(settings.gateway.runner_model_aliases)):
        raise ValueError(
            f"gateway.runner_model_aliases has no {', '.join(unserved)}: every eval run on one would fail",
        )
    registry = EvalkitRegistry(
        seed,
        url=f"http://{entry.name}:{entry.port}",
        resources=JobResources(cpu=settings.evalkit.job_cpu, memory_mb=settings.evalkit.job_memory_mb),
    )
    guard = guard_from_environment(os.environ)  # IAM decides every call: no identity or rights table here
    api = create_evalkit_api(registry, write_record_line, load_iam_authority(guard))
    options = iam_options(guard, entry.name)
    recorder = tool_call_recorder(caller_from_token, write_record_line, options["annotate"])
    server = build_evalkit_server(
        registry,
        caller_from_token,
        options["verifier"],
        [recorder, *options["extra_middleware"]],
    )
    uvicorn.run(
        build_service_app(guard, server, entry.name, api),
        host=ALL_ADDRESSES,
        port=entry.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main(sys.argv[1:])
