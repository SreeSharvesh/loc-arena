"""The telemetry service as a stack run's live service: it reads the company's services' telemetry to agents.

A stack run starts it as the scenario's entry with ``collects:``: ``python -m loc_arena.telemetry_service <run
config> <entry name>``. It serves the platform package ``meridian_telemetry`` under central IAM, which decides
every call: its tools at ``/mcp``, which agents reach through the tools gateway, each call one line of its
record. It keeps nothing: every read is the store's answer (``loc_arena.telemetry_stack``).
"""

from __future__ import annotations

import os
import sys
from typing import Any

import uvicorn
from pydantic import SecretStr
from starlette.applications import Starlette

from loc_arena.config import RunConfig, load_run_config
from loc_arena.iam_wiring import build_service_app, guard_from_environment, iam_options
from loc_arena.mcp_service import ALL_ADDRESSES, caller_from_token
from loc_arena.telemetry_stack import READER, READER_PASSWORD_FILE, STORE_SERVICE
from loc_arena.tool_records import Write, record_writer, tool_call_recorder
from meridian_telemetry.server import build_telemetry_server
from meridian_telemetry.store import TelemetryStore


def build_telemetry_app(
    config: RunConfig,
    service: str,
    guard: Any,  # noqa: ANN401 - a meridian_iam Guard
    write: Write,
) -> Starlette:
    """The app entry ``service`` of ``config`` serves under ``guard``: its tools at ``/mcp``, recorded."""
    settings = config.settings.telemetry
    password = (config.settings.gateway.secrets_dir / READER_PASSWORD_FILE).read_text().strip()
    store = TelemetryStore(
        f"http://{STORE_SERVICE}:{settings.store_port}",
        READER,
        SecretStr(password),
        settings.query_rows,
        settings.query_seconds,
    )
    options = iam_options(guard, service)
    recorder = tool_call_recorder(caller_from_token, write, options["annotate"])
    server = build_telemetry_server(store, options["verifier"], [recorder, *options["extra_middleware"]])
    return build_service_app(guard, server, service)


def main(arguments: list[str]) -> None:
    """Serve entry ``arguments[1]`` of the run config at ``arguments[0]``, under central IAM."""
    config = load_run_config(arguments[0])
    entry = next(service for service in config.live_services if service.name == arguments[1])
    guard = guard_from_environment(os.environ)  # IAM decides every call: no identity or rights table here
    write = record_writer(config.settings.stack.records_directory, entry.name)
    app = build_telemetry_app(config, entry.name, guard, write)
    uvicorn.run(app, host=ALL_ADDRESSES, port=entry.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main(sys.argv[1:])
