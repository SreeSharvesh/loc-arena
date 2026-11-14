"""The company's telemetry in a stack run: its own Collector, and the store that Collector alone writes.

The one live service with ``collects:`` is the telemetry store (GreptimeDB). It sits on a network of its own,
so nothing an agent runs reaches it, with two users: the Collector's, which can only write, and a reader's,
which can only read. The company's Collector, on agent-net and on the store's network, takes OTLP over HTTP
and sends each batch on to the store as the writer. Export is best effort, within the bounds of the run
config's ``telemetry:`` settings: a batch the store does not take in time is dropped, and nothing waits for
the store. The honest twin's renewal recreates both empty, with new passwords.

This Collector is the company's. The one that writes the sealed record of the model calls is another
container, on a network no service joins, and takes nothing from this one. Nothing that measures a run reads
the store.
"""

from __future__ import annotations

import base64
import secrets
from typing import TypedDict

from scenarios.loader import LiveService

from loc_arena.compose_document import ComposeDocument
from loc_arena.config import RunConfig
from loc_arena.yaml_documents import dump_document

COLLECTOR_SERVICE = "telemetry-collector"  # the company's Collector: its compose service and host name
TELEMETRY_NETWORK = "telemetry-net"  # the store and the Collector alone
WRITER, READER = "collector", "reader"  # the store's users: one can only write, the other only read
# Two compose secrets and the host variable of each: the store's users, the Collector's config.
STORE_USERS, USERS_VARIABLE = "telemetry_store_users", "LOC_ARENA_TELEMETRY_STORE_USERS"
COLLECTOR_CONFIG, COLLECTOR_CONFIG_VARIABLE = (
    "telemetry_collector_config",
    "LOC_ARENA_TELEMETRY_COLLECTOR_CONFIG",
)


class ExportBounds(TypedDict):
    """How long the Collector tries to send a batch to the store, and how many batches it holds meanwhile."""

    timeout: str
    retry_on_failure: dict[str, str]
    sending_queue: dict[str, int]


def find_store(config: RunConfig) -> LiveService | None:
    """The live service that holds the company's telemetry, if the scenario declares one."""
    return next((service for service in config.live_services if service.collects), None)


def list_telemetry_services(config: RunConfig) -> list[str]:
    """The company's Collector, which the honest twin's renewal recreates with its store; none without one."""
    return [COLLECTOR_SERVICE] if find_store(config) else []


def render_collector_config(config: RunConfig, store: LiveService, writer_password: str) -> str:
    """The company's Collector's config: OTLP in over HTTP, each batch on to ``store``, or dropped.

    A batch the store does not take is retried for ``retry_seconds`` and then dropped; a queue that is full
    drops the batch.
    """
    telemetry = config.settings.telemetry
    endpoint = f"http://{store.name}:{store.port}/v1/otlp/v1"
    basic = base64.b64encode(f"{WRITER}:{writer_password}".encode()).decode()
    headers = {"Authorization": f"Basic {basic}"}

    def bound() -> ExportBounds:  # a mapping of its own for each exporter: the document repeats none
        seconds = (
            f"{telemetry.retry_seconds}s"  # no wait between two tries is longer than all of them may take
        )
        return {
            "timeout": f"{telemetry.export_seconds}s",
            "retry_on_failure": {"max_interval": seconds, "max_elapsed_time": seconds},
            "sending_queue": {"queue_size": telemetry.queue_size},
        }

    document = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": f"0.0.0.0:{telemetry.collector_port}"}}}},
        "processors": {"batch": {}},
        "exporters": {
            "otlp_http/logs": {"logs_endpoint": f"{endpoint}/logs", "headers": headers, **bound()},
            # The store keeps spans by its trace pipeline, which the header names.
            "otlp_http/traces": {
                "traces_endpoint": f"{endpoint}/traces",
                "headers": {**headers, "x-greptime-pipeline-name": "greptime_trace_v1"},
                **bound(),
            },
        },
        "service": {
            "pipelines": {
                signal: {"receivers": ["otlp"], "processors": ["batch"], "exporters": [f"otlp_http/{signal}"]}
                for signal in ("logs", "traces")
            },
            "telemetry": {"logs": {"level": "warn"}},
        },
    }
    return dump_document(document)


def issue_telemetry_secrets(config: RunConfig, token_bytes: int) -> dict[str, str]:
    """One play's telemetry secrets by host variable: the store's two users, and the Collector's config."""
    store = find_store(config)
    if store is None:
        return {}
    writer, reader = secrets.token_urlsafe(token_bytes), secrets.token_urlsafe(token_bytes)
    return {
        USERS_VARIABLE: f"{WRITER}:wo={writer}\n{READER}:ro={reader}\n",
        COLLECTOR_CONFIG_VARIABLE: render_collector_config(config, store, writer),
    }


def add_telemetry(
    config: RunConfig,
    compose: ComposeDocument,
    agent_network: str,
    episode: str,
) -> ComposeDocument:
    """``compose`` with the company's telemetry, if the scenario has a store for it; else as it is.

    Moves the store from ``agent_network`` to a network of its own, started with its two users; adds the
    company's Collector, bounded as a live service, on both networks, started before ``episode`` plays.
    """
    store = find_store(config)
    if store is None:
        return compose
    stack, secrets_dir = config.settings.stack, config.settings.gateway.secrets_dir
    services = compose["services"]
    rendered = services[store.name]
    rendered["networks"] = [TELEMETRY_NETWORK]
    rendered["secrets"] = [*rendered.get("secrets", []), STORE_USERS]
    services[episode]["depends_on"][COLLECTOR_SERVICE] = {"condition": "service_started"}
    rendered["command"] = [
        *["standalone", "start", "--http-addr", f"0.0.0.0:{store.port}"],
        *["--user-provider", f"static_user_provider:file:{secrets_dir / STORE_USERS}"],
    ]
    services[COLLECTOR_SERVICE] = {
        "image": stack.collector_image,
        "command": ["--config", str(secrets_dir / COLLECTOR_CONFIG)],
        "secrets": [COLLECTOR_CONFIG],
        "cap_drop": ["ALL"],
        "mem_limit": stack.service_memory_limit,
        "cpus": stack.service_cpus,
        "pids_limit": stack.service_pids_limit,
        "networks": [agent_network, TELEMETRY_NETWORK],
    }
    compose["networks"][TELEMETRY_NETWORK] = {"internal": True}
    compose["secrets"] = {
        **compose["secrets"],
        STORE_USERS: {"environment": USERS_VARIABLE},
        COLLECTOR_CONFIG: {"environment": COLLECTOR_CONFIG_VARIABLE},
    }
    return compose
