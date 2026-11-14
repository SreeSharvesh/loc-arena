"""The company's telemetry in a stack run: its Collector, its store, and what each collected service is told.

The one live service with ``collects:`` reads telemetry to agents (``loc_arena.telemetry_service``). Each
service it names runs under OpenTelemetry's zero-code instrumentation and exports its traces and logs over
OTLP to the company's Collector; the tools gateway exports a span of every tool call there too, and passes
its trace context on, so a tool call and the service's work on it are one trace. The Collector, on agent-net,
is the store's one writer; the store sits with the reader on a network of their own, so nothing an agent runs
reaches it. Export is best effort, within the bounds of the run config's ``telemetry:`` settings: a signal
that cannot be sent is dropped, in the service and in the Collector, and nothing waits for the store.

This Collector is the company's. The one that writes the sealed record of the model calls is another
container, on a network no service joins, and takes nothing from this one.
"""

from __future__ import annotations

import base64
import secrets
from typing import Any

from scenarios.loader import LiveService

from loc_arena.compose_document import ComposeDocument
from loc_arena.config import RunConfig
from loc_arena.yaml_documents import dump_document

COLLECTOR_SERVICE = "telemetry-collector"  # the company's Collector: its compose service and host name
STORE_SERVICE = "telemetry-store"  # the store the Collector writes and the reader reads
TELEMETRY_NETWORK = "telemetry-net"  # the Collector, the store and the reader alone
GATEWAY_SERVICE_NAME = "tools-gateway"  # the service.name of the tools gateway's spans
WRITER, READER = "collector", "reader"  # the store's users: one can only write, the other only read
USERS_SECRET, USERS_VARIABLE = "telemetry_store_users", "LOC_ARENA_TELEMETRY_STORE_USERS"
COLLECTOR_CONFIG_SECRET = "telemetry_collector_config"
COLLECTOR_CONFIG_VARIABLE = "LOC_ARENA_TELEMETRY_COLLECTOR_CONFIG"
READER_PASSWORD_SECRET, READER_PASSWORD_VARIABLE = "telemetry_reader", "LOC_ARENA_TELEMETRY_READER"
READER_PASSWORD_FILE = "telemetry_reader"  # noqa: S105 - where the reader finds its password, in its secrets
INSTRUMENT = "opentelemetry-instrument"  # runs a command with the SDK and each library's instrumentation on
PACKAGE = "meridian-telemetry"  # the reader's platform package: on every engine service's path
NAMESPACE = "meridian"  # the service.namespace of every service of the company


def find_reader(config: RunConfig) -> LiveService | None:
    """The live service that reads telemetry to agents, if the scenario declares one."""
    return next((service for service in config.live_services if service.collects), None)


def list_telemetry_services(config: RunConfig) -> list[str]:
    """The Collector and the store, which the honest twin's renewal recreates empty; none without a reader."""
    return [COLLECTOR_SERVICE, STORE_SERVICE] if find_reader(config) else []


def list_collected_services(config: RunConfig) -> list[str]:
    """Every service whose telemetry the reader holds, in the order it lists them; none without a reader."""
    reader = find_reader(config)
    return list(reader.collects) if reader else []


def render_export_environment(config: RunConfig, service: str) -> dict[str, str]:
    """The standard variables the SDK in ``service`` reads: where it exports, and the bounds of its export.

    Its resource names the service; its supervisor, if it has one, adds the release it runs.
    """
    telemetry = config.settings.telemetry
    bounds = {
        "SCHEDULE_DELAY": telemetry.schedule_milliseconds,
        "MAX_QUEUE_SIZE": telemetry.queue_size,
        "EXPORT_TIMEOUT": telemetry.export_seconds * 1000,
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


def render_gateway_tracing(config: RunConfig) -> dict[str, Any]:
    """The tools gateway's tracing policy: a span of every request, with the calling agent, to the Collector.

    Nothing without a reader. The gateway passes the span's context on to the service it calls.
    """
    if find_reader(config) is None:
        return {}
    return {
        "tracing": {
            "host": f"{COLLECTOR_SERVICE}:{config.settings.telemetry.collector_port}",
            "protocol": "http",
            "randomSampling": True,
            "resources": {"service.name": f'"{GATEWAY_SERVICE_NAME}"', "service.namespace": f'"{NAMESPACE}"'},
            "attributes": {"agent": "apiKey.agent"},
        },
    }


def render_collector_config(config: RunConfig, writer_password: str) -> str:
    """The company's Collector's config: OTLP in over HTTP, each batch on to the store, or dropped.

    It stamps each record with the address it came from, which its sender cannot choose. A batch the store
    does not take is retried for ``retry_seconds`` and then dropped; a queue that is full drops the batch.
    """
    telemetry = config.settings.telemetry
    store = f"http://{STORE_SERVICE}:{telemetry.store_port}/v1/otlp/v1"
    basic = base64.b64encode(f"{WRITER}:{writer_password}".encode()).decode()

    def bound() -> dict[str, Any]:  # a mapping of its own for each exporter: the document repeats none
        return {
            "timeout": f"{telemetry.export_seconds}s",
            # No wait between two tries is longer than all of them may take.
            "retry_on_failure": {
                "max_interval": f"{telemetry.retry_seconds}s",
                "max_elapsed_time": f"{telemetry.retry_seconds}s",
            },
            "sending_queue": {"queue_size": telemetry.queue_size},
        }

    headers = {"Authorization": f"Basic {basic}"}
    stamp = {"actions": [{"key": "client.address", "from_context": "client.address", "action": "upsert"}]}
    document = {
        "receivers": {
            "otlp": {
                "protocols": {
                    "http": {"endpoint": f"0.0.0.0:{telemetry.collector_port}", "include_metadata": True},
                },
            },
        },
        "processors": {
            "attributes/sender": stamp,
            # The ASGI instrumentation's span of each message a request receives and sends: several a
            # request, and nothing a reader of a trace asks for.
            "filter/messages": {"traces": {"span": ['IsMatch(name, " http (receive|send)$")']}},
            "batch": {},
        },
        "exporters": {
            "otlp_http/logs": {"logs_endpoint": f"{store}/logs", "headers": headers, **bound()},
            # The store keeps spans by its trace pipeline, which the header names.
            "otlp_http/traces": {
                "traces_endpoint": f"{store}/traces",
                "headers": {**headers, "x-greptime-pipeline-name": "greptime_trace_v1"},
                **bound(),
            },
        },
        "service": {
            "pipelines": {
                signal: {
                    "receivers": ["otlp"],
                    "processors": [
                        "attributes/sender",
                        *(["filter/messages"] * (signal == "traces")),
                        "batch",
                    ],
                    "exporters": [f"otlp_http/{signal}"],
                }
                for signal in ("logs", "traces")
            },
            "telemetry": {"logs": {"level": "warn"}},
        },
    }
    return dump_document(document)


def issue_telemetry_secrets(config: RunConfig, token_bytes: int) -> dict[str, str]:
    """One play's telemetry secrets by host variable: the store's two users, and the Collector's config."""
    if find_reader(config) is None:
        return {}
    writer, reader = secrets.token_urlsafe(token_bytes), secrets.token_urlsafe(token_bytes)
    return {
        USERS_VARIABLE: f"{WRITER}:wo={writer}\n{READER}:ro={reader}\n",
        COLLECTOR_CONFIG_VARIABLE: render_collector_config(config, writer),
        READER_PASSWORD_VARIABLE: reader,
    }


def add_telemetry(config: RunConfig, compose: ComposeDocument) -> ComposeDocument:
    """``compose`` with the company's telemetry, if the scenario has a reader of it; else as it is.

    Adds the Collector and the store, each bounded as a live service, and their network; has each collected
    service run instrumented and export to the Collector; and gives the reader the store's network and its
    password there.
    """
    reader = find_reader(config)
    if reader is None:
        return compose
    stack, telemetry, secrets_dir = (
        config.settings.stack,
        config.settings.telemetry,
        config.settings.gateway.secrets_dir,
    )
    services = compose["services"]
    agent_network = services[reader.name]["networks"][0]
    bounded = {
        "cap_drop": ["ALL"],
        "mem_limit": stack.service_memory_limit,
        "cpus": stack.service_cpus,
        "pids_limit": stack.service_pids_limit,
    }
    for name in reader.collects:
        collected = services[name]
        collected["command"] = [INSTRUMENT, *collected["command"]]
        collected["environment"] = {**collected["environment"], **render_export_environment(config, name)}
    services[reader.name]["networks"] = [agent_network, TELEMETRY_NETWORK]
    services[reader.name]["secrets"] = [
        *services[reader.name]["secrets"],
        {"source": READER_PASSWORD_SECRET, "target": READER_PASSWORD_FILE},
    ]
    services[reader.name]["depends_on"] = {
        **services[reader.name]["depends_on"],
        STORE_SERVICE: {"condition": "service_healthy"},
        COLLECTOR_SERVICE: {"condition": "service_started"},
    }
    services[COLLECTOR_SERVICE] = {
        **bounded,
        "image": stack.collector_image,
        "command": ["--config", str(secrets_dir / COLLECTOR_CONFIG_SECRET)],
        "secrets": [COLLECTOR_CONFIG_SECRET],
        "networks": [agent_network, TELEMETRY_NETWORK],
    }
    services[STORE_SERVICE] = {
        **bounded,
        "image": stack.telemetry_store_image,
        "command": [
            *["standalone", "start", "--http-addr", f"0.0.0.0:{telemetry.store_port}"],
            *["--user-provider", f"static_user_provider:file:{secrets_dir / USERS_SECRET}"],
        ],
        "secrets": [USERS_SECRET],
        "networks": [TELEMETRY_NETWORK],
        "healthcheck": {
            "test": ["CMD", "curl", "--fail", "--silent", f"http://localhost:{telemetry.store_port}/health"],
            "interval": f"{stack.gateway_health_interval_seconds}s",
            "retries": stack.gateway_health_retries,
            "start_period": f"{stack.gateway_health_start_seconds}s",
            "start_interval": f"{stack.gateway_health_interval_seconds}s",
        },
    }
    compose["networks"][TELEMETRY_NETWORK] = {"internal": True}
    compose["secrets"] = {
        **compose["secrets"],
        USERS_SECRET: {"environment": USERS_VARIABLE},
        COLLECTOR_CONFIG_SECRET: {"environment": COLLECTOR_CONFIG_VARIABLE},
        READER_PASSWORD_SECRET: {"environment": READER_PASSWORD_VARIABLE},
    }
    return compose
