"""The company's telemetry in a stack run: its own Collector, and the store that Collector alone writes.

The one live service with ``collects:`` is the telemetry store (GreptimeDB). Each service it names runs under
OpenTelemetry's zero-code instrumentation and exports its traces and logs over OTLP, and the tools gateway
exports a span of every tool call, whose context it passes on to the service. The store sits on a
network of its own, so nothing an agent runs reaches it, with two users: the Collector's, which can only
write, and a reader's, which can only read. The company's Collector, on agent-net and on the store's network,
takes OTLP over HTTP and sends each batch on to the store as the writer. Export is best effort, within the
bounds of the run config's ``telemetry:`` settings: a signal that cannot be sent is dropped, in the service
and in the Collector, and nothing waits for the store. The honest twin's renewal recreates the store and the
Collector empty, with new passwords.

This Collector is the company's. The one that writes the sealed record of the model calls is another
container, on a network no service joins, and takes nothing from this one. Nothing that measures a run reads
the store.
"""

from __future__ import annotations

import base64
import secrets
from typing import TypedDict

from scenarios.loader import LiveService

from loc_arena.compose_document import ComposeDocument, render_healthcheck
from loc_arena.config import RunConfig
from loc_arena.yaml_documents import dump_document

COLLECTOR_SERVICE = "telemetry-collector"  # the company's Collector: its compose service and host name
READER_SERVICE = "telemetry-reader"  # the store's own MCP server, which reads it to the tools gateway
TELEMETRY_NETWORK = "telemetry-net"  # the store, the Collector, the store's reader and the tools gateway
WRITER, READER = "collector", "reader"  # the store's users: one can only write, the other only read
# Two compose secrets and the host variable of each: the store's users, the Collector's config.
STORE_USERS, USERS_VARIABLE = "telemetry_store_users", "LOC_ARENA_TELEMETRY_STORE_USERS"
COLLECTOR_CONFIG, COLLECTOR_CONFIG_VARIABLE = (
    "telemetry_collector_config",
    "LOC_ARENA_TELEMETRY_COLLECTOR_CONFIG",
)
READER_PASSWORD, READER_PASSWORD_VARIABLE = "telemetry_reader", "LOC_ARENA_TELEMETRY_READER"
# The reader's command: the store's own MCP server, given the reader's password from its mounted secret.
READ_STORE = 'GREPTIMEDB_PASSWORD="$(cat "$1")" exec greptimedb-mcp-server'
STORE_DATA = "/var/lib/greptimedb"  # where the store keeps its data: a memory filesystem of bounded size
INSTRUMENT = "opentelemetry-instrument"  # runs a command with the SDK and each library's instrumentation on
SDK_BATCH_SIZE = 512  # the SDK's default: the most spans, or log records, in one export
GATEWAY_NAME = "tools-gateway"  # the service.name of the tools gateway's spans
NAMESPACE = "meridian"  # the service.namespace of every service of the company
# What the Collector stamps on each record: the address it came from. A name of its own, since a span's
# `client.address` is that span's caller, as on the tools gateway's.
SENDER_ATTRIBUTE = f"{NAMESPACE}.sender.address"


class ExportBounds(TypedDict):
    """How long the Collector tries to send a batch to the store, and how many batches it holds meanwhile."""

    timeout: str
    retry_on_failure: dict[str, str]
    sending_queue: dict[str, int]


class GatewayTracing(TypedDict):
    """agentgateway's ``frontendPolicies.tracing``: where it exports spans, and what each one names."""

    host: str
    protocol: str
    randomSampling: str  # each of the three a CEL expression
    clientSampling: str
    parentNotSampled: str
    resources: dict[str, str]  # each a CEL expression
    attributes: dict[str, str]  # each a CEL expression


def find_store(config: RunConfig) -> LiveService | None:
    """The live service that holds the company's telemetry, if the scenario declares one."""
    return next((service for service in config.live_services if service.collects), None)


def list_telemetry_services(config: RunConfig) -> list[str]:
    """What the honest twin's renewal recreates with the store: its Collector, and its reader if any."""
    store = find_store(config)
    return [] if store is None else [COLLECTOR_SERVICE, *([READER_SERVICE] if store.tools else [])]


def find_reader(config: RunConfig) -> str | None:
    """The store's own MCP server's compose service, which the tools gateway joins the store's network for."""
    store = find_store(config)
    return READER_SERVICE if store and store.tools else None


def locate_reader(config: RunConfig, service: LiveService) -> str | None:
    """Where the tools gateway finds ``service``'s tools if it is the telemetry store: its own MCP server."""
    if not service.collects:
        return None
    return f"http://{READER_SERVICE}:{config.settings.telemetry.reader_port}/mcp"


def render_export_environment(config: RunConfig, service: str) -> dict[str, str]:
    """The standard variables the SDK in ``service`` reads: where it exports, and the bounds of its export.

    Its resource names the service; its supervisor, if it has one, adds the release it runs.
    """
    telemetry = config.settings.telemetry
    bounds = {
        "SCHEDULE_DELAY": telemetry.schedule_milliseconds,
        "MAX_QUEUE_SIZE": telemetry.queue_size,
        # The SDK refuses to start with a batch larger than its queue.
        "MAX_EXPORT_BATCH_SIZE": min(SDK_BATCH_SIZE, telemetry.queue_size),
    }
    return {
        "OTEL_SERVICE_NAME": service,
        "OTEL_RESOURCE_ATTRIBUTES": f"service.namespace={NAMESPACE}",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://{COLLECTOR_SERVICE}:{telemetry.collector_port}",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_TIMEOUT": str(telemetry.export_seconds),
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_PYTHON_LOG_CODE_ATTRIBUTES": "true",
        # The span and the log record batch processors, each with the same bounds.
        **{f"OTEL_{kind}_{bound}": str(value) for kind in ("BSP", "BLRP") for bound, value in bounds.items()},
    }


def render_gateway_tracing(config: RunConfig) -> GatewayTracing | None:
    """The tools gateway's tracing policy: a span of every request, with the calling agent, to the Collector.

    None without a telemetry store. The gateway passes the span's context on to the service it calls, so
    a tool call and the service's work on it are one trace.
    """
    if find_store(config) is None:
        return None
    return {
        "host": f"{COLLECTOR_SERVICE}:{config.settings.telemetry.collector_port}",
        "protocol": "http",
        # Every request: one with no trace context, one that asks to be traced, one that asks not to be.
        "randomSampling": "true",
        "clientSampling": "true",
        "parentNotSampled": "true",
        "resources": {"service.name": f'"{GATEWAY_NAME}"', "service.namespace": f'"{NAMESPACE}"'},
        "attributes": {"agent": "apiKey.agent"},
    }


def render_collector_config(config: RunConfig, store: LiveService, writer_password: str) -> str:
    """The company's Collector's config: OTLP in over HTTP, each batch on to ``store``, or dropped.

    It stamps each record with the address it came from, which its sender cannot choose. A batch the store
    does not take is retried for ``retry_seconds`` and then dropped; a queue that is full drops the batch.
    """
    telemetry = config.settings.telemetry
    endpoint = f"http://{store.name}:{store.port}/v1/otlp/v1"
    basic = base64.b64encode(f"{WRITER}:{writer_password}".encode()).decode()
    # Each table the store creates for OTLP keeps a row for ``retention``.
    headers = {"Authorization": f"Basic {basic}", "x-greptime-hints": f"ttl={telemetry.retention}"}

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
        "receivers": {
            "otlp": {
                "protocols": {
                    "http": {"endpoint": f"0.0.0.0:{telemetry.collector_port}", "include_metadata": True},
                },
            },
        },
        "processors": {
            # First in each pipeline: past its bound the Collector refuses data, so it is never killed for it.
            "memory_limiter": {
                "check_interval": f"{telemetry.collector_check_seconds}s",
                "limit_mib": telemetry.collector_memory_mib,
                "spike_limit_mib": telemetry.collector_spike_mib,
            },
            "attributes/sender": {
                "actions": [{"key": SENDER_ATTRIBUTE, "from_context": "client.address", "action": "upsert"}],
            },
            # The ASGI instrumentation's span of each message a request receives and sends: several a
            # request, and nothing a reader of a trace asks for.
            "filter/messages": {"traces": {"span": ['IsMatch(name, " http (receive|send)$")']}},
            "batch": {},
        },
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
                signal: {
                    "receivers": ["otlp"],
                    "processors": ["memory_limiter", "attributes/sender", *filters, "batch"],
                    "exporters": [f"otlp_http/{signal}"],
                }
                for signal, filters in (("logs", []), ("traces", ["filter/messages"]))
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
        READER_PASSWORD_VARIABLE: reader,
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
    company's Collector, with memory bounds of its own, on both networks, started before ``episode``
    plays; and has each collected service run under OpenTelemetry's zero-code instrumentation, exporting
    to it.
    """
    store = find_store(config)
    if store is None:
        return compose
    stack, secrets_dir = config.settings.stack, config.settings.gateway.secrets_dir
    services = compose["services"]
    rendered = services[store.name]
    rendered["networks"] = [TELEMETRY_NETWORK]
    rendered["secrets"] = [*rendered.get("secrets", []), STORE_USERS]
    # Each collected service, and the episode, start once the Collector has, so a first export finds it.
    for name in store.collects:
        collected = services[name]
        collected["command"] = [INSTRUMENT, *collected["command"]]
        collected["environment"] = {**collected["environment"], **render_export_environment(config, name)}
    for name in (*store.collects, episode):
        services[name].setdefault("depends_on", {})[COLLECTOR_SERVICE] = {"condition": "service_started"}
    rendered["command"] = [
        *["standalone", "start", "--http-addr", f"0.0.0.0:{store.port}"],
        *["--user-provider", f"static_user_provider:file:{secrets_dir / STORE_USERS}"],
        *["--mysql-addr", f"0.0.0.0:{config.settings.telemetry.sql_port}"],  # where its reader asks it
        *["--data-home", STORE_DATA],
    ]
    telemetry = config.settings.telemetry
    rendered["tmpfs"] = [f"{STORE_DATA}:size={telemetry.store_disk_mib}m"]  # its disk, bounded
    rendered["mem_limit"] = f"{telemetry.store_memory_mib}m"  # the disk is in its memory: room beside it
    # Back after an OOM kill, empty: its disk was memory. Its Collector drops what it sends meanwhile.
    rendered["restart"] = "unless-stopped"
    if store.tools:
        _add_reader(config, compose, store)
    services[COLLECTOR_SERVICE] = {
        "image": stack.collector_image,
        "command": ["--config", str(secrets_dir / COLLECTOR_CONFIG)],
        "secrets": [COLLECTOR_CONFIG],
        # Its Go runtime collects from where its memory limiter starts to refuse data.
        "environment": {"GOMEMLIMIT": f"{telemetry.collector_memory_mib - telemetry.collector_spike_mib}MiB"},
        "cap_drop": ["ALL"],
        # Its own limit: its memory limiter's, and what the Collector holds beside its data.
        "mem_limit": f"{telemetry.collector_memory_mib + telemetry.collector_overhead_mib}m",
        "cpus": stack.service_cpus,
        "pids_limit": stack.service_pids_limit,
        "networks": [agent_network, TELEMETRY_NETWORK],
        "restart": "unless-stopped",
    }
    compose["networks"][TELEMETRY_NETWORK] = {"internal": True}
    compose["secrets"] = {
        **compose["secrets"],
        STORE_USERS: {"environment": USERS_VARIABLE},
        COLLECTOR_CONFIG: {"environment": COLLECTOR_CONFIG_VARIABLE},
        READER_PASSWORD: {"environment": READER_PASSWORD_VARIABLE},
    }
    return compose


def _add_reader(config: RunConfig, compose: ComposeDocument, store: LiveService) -> None:
    """Add the store's own MCP server to ``compose``; the tools gateway joins its network (``find_reader``).

    The server holds no credential of an agent and checks none, so it is on the store's network alone: only
    the tools gateway, which asks IAM before each call, reaches it. It reads as the store's read-only user,
    which is what keeps a query from changing anything, and returns at most ``telemetry.result_bytes``.
    """
    stack, telemetry = config.settings.stack, config.settings.telemetry
    services, port = compose["services"], telemetry.reader_port
    engine = next(service for service in services.values() if "build" in service and "environment" in service)
    services[READER_SERVICE] = {
        "image": engine["image"],
        "pull_policy": "never",  # the engine image, which another service builds
        "command": ["sh", "-c", READ_STORE, "sh", str(config.settings.gateway.secrets_dir / READER_PASSWORD)],
        "environment": {
            "GREPTIMEDB_HOST": store.name,
            "GREPTIMEDB_PORT": str(telemetry.sql_port),
            "GREPTIMEDB_HTTP_PORT": str(store.port),
            "GREPTIMEDB_USER": READER,
            "GREPTIMEDB_TRANSPORT": "streamable-http",
            "GREPTIMEDB_LISTEN_PORT": str(telemetry.reader_port),
            "GREPTIMEDB_MAX_RESULT_BYTES": str(telemetry.result_bytes),
        },
        "secrets": [READER_PASSWORD],
        "cap_drop": ["ALL"],
        "mem_limit": stack.service_memory_limit,
        "cpus": stack.service_cpus,
        "pids_limit": stack.service_pids_limit,
        "networks": [TELEMETRY_NETWORK],
        "depends_on": {store.name: {"condition": "service_healthy"}},
        "healthcheck": render_healthcheck(
            ["CMD", "python", "-c", f"import socket; socket.create_connection(('localhost', {port}), 2)"],
            stack,
        ),
    }
