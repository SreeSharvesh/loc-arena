"""The model gateway: agentgateway holding the provider key, and the Collector that writes the sealed record.

agentgateway forwards a request on an allowed path to the one upstream in ``settings.gateway`` under the key,
and returns the reply unchanged. It admits callers by the SHA-256 of their token, so it never holds one: the
episode, the host's monitors, each agent's sandbox, each runner and each node of the job service's fleet.
Runners call on their own route, which holds their model allowlist and their calls per play, and so do the
jobs on a node, as that node: the gateway admits no job's own credential yet. Every request leaves an
access-log record (caller, request and reply, refusals and failures included), tagged with its play and
exported over OTLP to an OpenTelemetry Collector on a network only the two share; the Collector appends it to
the sealed record, in a volume only it mounts.

agentgateway exports in batches, and a batch still pending when it stops can be lost. So a stack run drains
the record before the gateway stops (waits until the Collector holds a record of every request the gateway
logged on stdout), and copies it out as ``GatewayCall`` lines.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import certifi
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from loc_arena.compose_document import ComposeService
from loc_arena.gateway.core import (
    API_KEY_VARIABLE,
    GATEWAY_TOKEN_FILE_VARIABLE,
    GATEWAY_URL_VARIABLE,
    OPENROUTER_URL,
)
from loc_arena.settings import LocArenaSettings
from loc_arena.yaml_documents import dump_document

MODEL_GATEWAY_SERVICE = "gateway"  # its compose service and host name
COLLECTOR_SERVICE = "collector"  # the OpenTelemetry Collector's compose service and host name
RECORD_NETWORK = "record-net"  # the gateway and the Collector alone: no agent can send the Collector a record
COLLECTOR_PORT = 4317  # the Collector's OTLP gRPC receiver
KEY_SECRET_NAME = "openrouter_api_key"  # the provider key: a file in the gateway's secrets directory
# The roots the gateway verifies the provider with: certifi's, kept current by the lock, since the image's own
# store lags behind distrusts such as the compromised emSign CAs.
PROVIDER_ROOTS_SECRET_NAME = "provider_roots"
MODEL_GATEWAY_CONFIG_SECRET_NAME = "model_gateway_config"
MODEL_GATEWAY_CONFIG_VARIABLE = "LOC_ARENA_MODEL_GATEWAY_CONFIG"
COLLECTOR_CONFIG_SECRET_NAME = "collector_config"
COLLECTOR_CONFIG_VARIABLE = "LOC_ARENA_COLLECTOR_CONFIG"
GATEWAY_TOKEN_SECRET_NAME = (
    "gateway_token"  # the episode's key to the gateway, a file in its secrets directory
)
EPISODE_TOKEN_VARIABLE = "LOC_ARENA_GATEWAY_TOKEN"
HOST_TOKEN_VARIABLE = "LOC_ARENA_HOST_GATEWAY_TOKEN"  # the host's monitors' key: never a compose secret
EPISODE_CALLER = "episode"
HOST_CALLER = "host"
RUNNER_ROUTE_PREFIX = "runner"  # runners call /runner/<the chat path>
CHAT_PATH = "api/v1/chat/completions"  # the one path runners may call
ACCESS_LOG_SCOPE = "agentgateway.access"  # the OTLP scope of agentgateway's access-log records
PROJECT_PREFIX = "locarena-"  # each stack run's compose project is locarena-<instance id>
UNKNOWN_CALLER = "unknown"
CALL_RECORD_FILE = "gateway.calls.jsonl"  # the run directory's copy of the call record, which grading reads
NEVER_REFILLS = "87600h"  # a rate-limit bucket that refills once in ten years counts calls per play
POLL_SECONDS = 0.5

Role = Literal["caller", "runner"]
Play = Literal["attack", "twin"]
RunCompose = Callable[..., subprocess.CompletedProcess[str]]
PLAY_TAG: TypeAdapter[Play | None] = TypeAdapter(Play | None)  # a record's play tag, parsed


@dataclass(frozen=True)
class Caller:
    """One caller of the gateway: the name its records carry, its token, and the route it may use."""

    name: str
    token: str
    role: Role


class GatewayCall(BaseModel):
    """One line of the call log: who called, what they sent, and what came back."""

    model_config = ConfigDict(frozen=True)

    started_at: float = Field(description="Unix time the gateway received the request.")
    seconds: float = Field(description="Time until the reply ended or the call failed.")
    caller: str = Field(description="The caller's compose service, or its address when it sent no token.")
    method: str = Field(description="The HTTP method.")
    path: str = Field(description="The path the caller asked for.")
    request: JsonValue = Field(description="The request body: parsed JSON, else text.")
    status: int = Field(description="The status returned to the caller.")
    response: JsonValue = Field(description="The reply as received: parsed JSON, else text (an SSE stream).")
    complete: bool = Field(description="False when the call was refused or failed, or its reply broke off.")
    play: Play | None = Field(default=None, description="The play the call was made in; none if untagged.")


class OtlpValue(BaseModel):
    """An OTLP attribute value, in the Collector's JSON: one of its typed fields is set."""

    string: str | None = Field(default=None, alias="stringValue")
    integer: int | None = Field(default=None, alias="intValue")


class OtlpAttribute(BaseModel):
    """One attribute of an OTLP record."""

    key: str
    value: OtlpValue


class OtlpLogRecord(BaseModel):
    """One OTLP log record: here, one request's access-log record."""

    observed_nanoseconds: int = Field(alias="observedTimeUnixNano")
    attributes: list[OtlpAttribute] = Field(default_factory=list)

    def text(self, key: str) -> str | None:
        """The string attribute ``key``, if the record has it."""
        return next((attribute.value.string for attribute in self.attributes if attribute.key == key), None)

    def integer(self, key: str) -> int | None:
        """The integer attribute ``key``, if the record has it."""
        return next((attribute.value.integer for attribute in self.attributes if attribute.key == key), None)


class OtlpScope(BaseModel):
    """The instrumentation scope of a batch of records."""

    name: str = ""


class OtlpScopeLogs(BaseModel):
    """The records of one scope."""

    scope: OtlpScope = Field(default_factory=OtlpScope)
    records: list[OtlpLogRecord] = Field(default_factory=list, alias="logRecords")


class OtlpResourceLogs(BaseModel):
    """The records of one exporting process."""

    scope_logs: list[OtlpScopeLogs] = Field(default_factory=list, alias="scopeLogs")


class OtlpExport(BaseModel):
    """One line the Collector's file exporter writes: one batch it received."""

    resource_logs: list[OtlpResourceLogs] = Field(default_factory=list, alias="resourceLogs")


def locate_call_log(logs: Path, instance_id: str) -> Path:
    """Where a stack run copies its gateway's call log: in ``logs``, named after the run's compose project."""
    return logs / "gateway" / f"{PROJECT_PREFIX}{instance_id}.calls.jsonl"


def parse_body(raw: bytes) -> JsonValue:
    """The body parsed as JSON when it is JSON, else as text (an SSE stream or an error page)."""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def render_model_gateway_config(settings: LocArenaSettings, callers: Sequence[Caller], play: Play) -> str:
    """The gateway's config (agentgateway v1.6 schema) for ``callers`` in ``play``, as a compose secret.

    Callers are admitted by the SHA-256 of their token, runners only on their route; any other path is refused
    (403). A request's body is
    buffered (the authorization rule reads it), so a call that never reaches the upstream keeps its request
    in the record. The upstream is asked for an uncompressed reply, so the record holds its text. It listens
    on IPv4 alone; admin, stats and readiness listeners are off, so no caller can read this config back.
    """
    gateway = settings.gateway
    if CHAT_PATH not in gateway.allowed_paths:
        raise ValueError(f"the runners' path {CHAT_PATH} is not in gateway.allowed_paths")
    keys = [
        {
            "keyHash": f"sha256:{hashlib.sha256(caller.token.encode()).hexdigest()}",
            "metadata": {"caller": caller.name, "role": caller.role},
        }
        for caller in callers
    ]
    upstream, roots = gateway.upstream, str(gateway.secrets_dir / PROVIDER_ROOTS_SECRET_NAME)
    backends = [
        {
            "ai": {
                "name": "upstream",
                "hostOverride": f"{upstream.host}:{upstream.port}",
                "provider": {"custom": {"formats": [{"type": "completions", "path": f"/{CHAT_PATH}"}]}},
                **({"policies": {"backendTLS": {"root": roots}}} if upstream.scheme == "https" else {}),
            },
        },
    ]
    shared = {
        "apiKey": {
            "mode": "strict",
            "location": {"header": {"name": "authorization", "prefix": "Bearer "}},
            "keys": keys,
        },
        "backendAuth": {"key": {"file": str(gateway.secrets_dir / KEY_SECRET_NAME)}},
        "transformations": {"request": {"remove": ["accept-encoding"]}},
        # The first bounds the wait for a reply's headers, the second each wait for more of its body.
        "timeout": {
            "requestTimeout": f"{gateway.timeout_seconds}s",
            "responseIdleTimeout": f"{gateway.timeout_seconds}s",
        },
        "ai": {"routes": {"*": "passthrough"}},  # forwarded as the caller sent it
    }
    runner_models = json.dumps(list(gateway.runner_models))
    routes = [
        {
            "name": "callers",
            "matches": [{"path": {"exact": f"/{path}"}} for path in gateway.allowed_paths],
            "policies": {
                **shared,
                "authorization": {"rules": [{"allow": 'apiKey.role == "caller" && size(request.body) >= 0'}]},
            },
            "backends": backends,
        },
        {
            "name": "runners",
            "matches": [{"path": {"exact": f"/{RUNNER_ROUTE_PREFIX}/{CHAT_PATH}"}}],
            "policies": {
                **shared,
                "urlRewrite": {"path": {"full": f"/{CHAT_PATH}"}},
                "authorization": {
                    "rules": [
                        {"allow": f'apiKey.role == "runner" && json(request.body).model in {runner_models}'},
                    ],
                },
                "localRateLimit": [
                    {
                        "type": "requests",
                        "maxTokens": gateway.runner_calls_per_play,
                        "tokensPerFill": 1,
                        "fillInterval": NEVER_REFILLS,
                    },
                ],
            },
            "backends": backends,
        },
        {
            # Any other path: refused, its body read first so the record keeps it, its caller named if known.
            "name": "refused",
            "matches": [{"path": {"pathPrefix": "/"}}],
            "policies": {
                "apiKey": {**shared["apiKey"], "mode": "optional"},
                "authorization": {"rules": [{"allow": "size(request.body) < 0"}]},
            },
            "backends": backends,
        },
    ]
    document = {
        "config": {
            "enableIpv6": False,  # its default listener, [::], cannot bind where the kernel has no IPv6
            "adminAddr": "off",
            "statsAddr": "off",
            "readinessAddr": "off",
            "logging": {"format": "json"},
        },
        "frontendPolicies": {
            "http": {"maxBufferSize": gateway.body_limit_bytes},
            "accessLog": {
                # Raw bytes, base64 in the record: a body that is not UTF-8 is still recorded.
                "add": {
                    "play": json.dumps(play),
                    "caller": "apiKey.caller",
                    "request_body": "request.body",
                    "response_body": "response.body",
                },
                "otlp": {"host": f"{COLLECTOR_SERVICE}:{COLLECTOR_PORT}", "protocol": "grpc"},
            },
        },
        "binds": [{"port": gateway.port, "listeners": [{"routes": routes}]}],
    }
    return dump_document(document)


def render_collector_config(settings: LocArenaSettings) -> str:
    """The Collector's config: OTLP in, each batch appended as one JSON line to the sealed record.

    ``append`` keeps the record when the Collector restarts; the message limit lets a long reply's record in.
    """
    gateway = settings.gateway
    receiver = {
        "endpoint": f"0.0.0.0:{COLLECTOR_PORT}",
        "max_recv_msg_size_mib": gateway.record_message_limit_mib,
    }
    document = {
        "receivers": {"otlp": {"protocols": {"grpc": receiver}}},
        "exporters": {
            "file": {
                "path": str(gateway.call_log),
                "format": "json",
                "append": True,
                "flush_interval": "100ms",
            },
        },
        "service": {
            "pipelines": {"logs": {"receivers": ["otlp"], "exporters": ["file"]}},
            "telemetry": {"logs": {"level": "warn"}},
        },
    }
    return dump_document(document)


def render_gateway_services(
    settings: LocArenaSettings,
    agent_network: str,
    egress_network: str,
) -> dict[str, ComposeService]:
    """The gateway and the Collector: pinned images with no shell, so no healthcheck; each config a secret.

    The gateway alone joins ``egress_network``; the record network joins it to the Collector alone, which runs
    as root with no capability so it can write its fresh volume. The gateway's port is published on the host's
    loopback for the monitors, and it is given time to export its last records when it stops.
    """
    stack, gateway = settings.stack, settings.gateway
    return {
        MODEL_GATEWAY_SERVICE: {
            "image": stack.tools_gateway_image,
            "command": ["-f", str(gateway.secrets_dir / MODEL_GATEWAY_CONFIG_SECRET_NAME)],
            "secrets": [KEY_SECRET_NAME, MODEL_GATEWAY_CONFIG_SECRET_NAME, PROVIDER_ROOTS_SECRET_NAME],
            "cap_drop": ["ALL"],
            "networks": [agent_network, egress_network, RECORD_NETWORK],
            "ports": [f"127.0.0.1::{gateway.port}"],
            "stop_grace_period": f"{gateway.stop_grace_seconds}s",
            "depends_on": {COLLECTOR_SERVICE: {"condition": "service_started"}},
        },
        COLLECTOR_SERVICE: {
            "image": stack.collector_image,
            "command": ["--config", str(gateway.secrets_dir / COLLECTOR_CONFIG_SECRET_NAME)],
            "secrets": [COLLECTOR_CONFIG_SECRET_NAME],
            "user": "0:0",
            "cap_drop": ["ALL"],
            "volumes": [f"sealed:{gateway.call_log.parent}"],
            "networks": [RECORD_NETWORK],
        },
    }


def issue_held_tokens(token_bytes: int) -> dict[str, str]:
    """The episode's and the host's tokens, by host variable: issued once, since neither is recreated."""
    return {
        variable: secrets.token_urlsafe(token_bytes)
        for variable in (EPISODE_TOKEN_VARIABLE, HOST_TOKEN_VARIABLE)
    }


def issue_gateway_secrets(
    settings: LocArenaSettings,
    held: Mapping[str, str],
    sandboxes: Mapping[str, str],
    compute: Mapping[str, str],
    play: Play,
) -> dict[str, str]:
    """One play's gateway secrets by host variable: the ``held`` tokens and configs admitting its callers.

    The callers are the episode and the host, each sandbox in ``sandboxes``, and on the runners' route each
    runner and each node of the job service's fleet in ``compute`` (compose service to token).
    """
    callers = [
        Caller(EPISODE_CALLER, held[EPISODE_TOKEN_VARIABLE], "caller"),
        Caller(HOST_CALLER, held[HOST_TOKEN_VARIABLE], "caller"),
        *(Caller(name, token, "caller") for name, token in sandboxes.items()),
        *(Caller(name, token, "runner") for name, token in compute.items()),
    ]
    return {
        **held,
        MODEL_GATEWAY_CONFIG_VARIABLE: render_model_gateway_config(settings, callers, play),
        COLLECTOR_CONFIG_VARIABLE: render_collector_config(settings),
    }


def runners_api_url(settings: LocArenaSettings) -> str:
    """The gateway's API on the runners' route, OpenRouter's passed through, as runners and jobs call it."""
    return f"http://{MODEL_GATEWAY_SERVICE}:{settings.gateway.port}/{RUNNER_ROUTE_PREFIX}/api/v1"


def render_episode_environment(settings: LocArenaSettings) -> dict[str, str]:
    """Where the episode finds the gateway and the file of its token there."""
    return {
        GATEWAY_URL_VARIABLE: f"http://{MODEL_GATEWAY_SERVICE}:{settings.gateway.port}{urlsplit(OPENROUTER_URL).path}",
        GATEWAY_TOKEN_FILE_VARIABLE: str(settings.gateway.secrets_dir / GATEWAY_TOKEN_SECRET_NAME),
    }


def declare_gateway_secrets() -> dict[str, dict[str, str]]:
    """The provider key, the gateway's and the Collector's configs and the episode's token: host variables.

    The provider's roots are certifi's file: too large for a variable.
    """
    return {
        KEY_SECRET_NAME: {"environment": API_KEY_VARIABLE},
        PROVIDER_ROOTS_SECRET_NAME: {"file": certifi.where()},
        MODEL_GATEWAY_CONFIG_SECRET_NAME: {"environment": MODEL_GATEWAY_CONFIG_VARIABLE},
        COLLECTOR_CONFIG_SECRET_NAME: {"environment": COLLECTOR_CONFIG_VARIABLE},
        GATEWAY_TOKEN_SECRET_NAME: {"environment": EPISODE_TOKEN_VARIABLE},
    }


def read_records(otlp: str) -> list[OtlpLogRecord]:
    """The gateway's access-log records in the Collector's file, in the order it wrote them."""
    return [
        record
        for line in otlp.splitlines()
        if line.strip()
        for resource in OtlpExport.model_validate_json(line).resource_logs
        for scope in resource.scope_logs
        if scope.scope.name == ACCESS_LOG_SCOPE
        for record in scope.records
    ]


def convert_record(record: OtlpLogRecord) -> GatewayCall:
    """One access-log record as a ``GatewayCall``.

    A call is complete unless the gateway refused or failed it (its record names an ``error``) or its streamed
    reply ended before ``data: [DONE]``.
    """

    def body(key: str) -> bytes:
        text = record.text(key) or ""
        try:
            return base64.b64decode(text, validate=True)
        except binascii.Error:  # not base64: the text as recorded
            return text.encode()

    response = body("response_body")
    broke_off = response.lstrip().startswith(b"data:") and not response.rstrip().endswith(b"data: [DONE]")
    seconds = parse_duration(record.text("duration") or "")
    return GatewayCall(
        started_at=round(record.observed_nanoseconds / 1e9 - seconds, 3),
        seconds=seconds,
        caller=record.text("caller") or record.text("client.address") or UNKNOWN_CALLER,
        method=record.text("http.request.method") or "",
        path=record.text("url.path") or "",
        request=parse_body(body("request_body")),
        status=record.integer("http.response.status_code") or 0,
        response=parse_body(response),
        complete=record.text("error") is None and not broke_off,
        play=PLAY_TAG.validate_python(record.text("play")),
    )


def parse_duration(duration: str) -> float:
    """Seconds in an agentgateway duration (``7ms``, ``1.5s``, ``250µs``); 0 for anything else."""
    found = re.fullmatch(r"([0-9.]+)(µs|us|ms|s)", duration.strip())
    if not found:
        return 0.0
    return round(float(found.group(1)) * {"µs": 1e-6, "us": 1e-6, "ms": 1e-3, "s": 1.0}[found.group(2)], 3)


def list_logged_plays(gateway_log: str) -> list[str]:
    """The play tag of each request agentgateway logged on stdout: one JSON line of scope ``request`` each."""
    plays = []
    for line in gateway_log.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:  # not a log line of agentgateway's
            continue
        if isinstance(entry, dict) and entry.get("scope") == "request":
            plays.append(str(entry.get("play", "")))
    return plays


def fetch_record(run: RunCompose, compose: list[str], settings: LocArenaSettings) -> str | None:
    """The Collector's file as it stands, up to its last complete line; ``None`` if it cannot be copied.

    The Collector appends to the file in place, so a copy can end inside a line it is still writing: that tail
    is left for the next copy.
    """
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / "record.jsonl"
        source = f"{COLLECTOR_SERVICE}:{settings.gateway.call_log}"
        if run([*compose, "cp", source, str(copy)], capture_output=True, text=True).returncode != 0:
            return None
        record = copy.read_text(encoding="utf-8") if copy.exists() else ""
        return record[: record.rfind("\n") + 1]


def stop_callers(run: RunCompose, compose: list[str], callers: Sequence[str] | None = None) -> bool:
    """Stop the gateway's ``callers``, by default every service but the gateway and the Collector.

    Then no call is in flight: a call cut off ends as its caller stops, and the gateway logs it.
    """
    if callers is None:
        listed = run([*compose, "config", "--services"], capture_output=True, text=True)
        if listed.returncode != 0:
            return False
        callers = [
            name for name in listed.stdout.split() if name not in (MODEL_GATEWAY_SERVICE, COLLECTOR_SERVICE)
        ]
    return not callers or run([*compose, "stop", *callers], capture_output=True, text=True).returncode == 0


def drain_record(
    run: RunCompose,
    compose: list[str],
    settings: LocArenaSettings,
    callers: Sequence[str] | None = None,
) -> bool:
    """Stop the gateway's ``callers``, then wait until the Collector holds a record of each request it logged.

    False if the callers cannot be stopped, if a complete line of the record does not parse, or if the record
    still falls short when ``gateway.drain_timeout_seconds`` runs out.
    """
    if not stop_callers(run, compose, callers):
        return False
    logs = [*compose, "logs", "--no-color", "--no-log-prefix", MODEL_GATEWAY_SERVICE]
    deadline = time.monotonic() + settings.gateway.drain_timeout_seconds
    while True:
        logged = run(logs, capture_output=True, text=True)
        record = fetch_record(run, compose, settings)
        if logged.returncode == 0 and record is not None:
            plays = list_logged_plays(logged.stdout)
            try:
                held = [each.text("play") or "" for each in read_records(record)]
            except ValidationError:
                return False
            if all(held.count(play) >= plays.count(play) for play in set(plays)):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def copy_call_log(
    run: RunCompose,
    compose: list[str],
    settings: LocArenaSettings,
    call_log: Path,
    callers: Sequence[str] | None = None,
) -> bool:
    """Stop ``callers`` (by default every one), drain the record, and copy it out as ``GatewayCall`` lines.

    The lines go to ``call_log``, the Collector's own beside it. False, and nothing written, if the record
    does not drain, cannot be copied, or holds a line that does not parse.
    """
    record = fetch_record(run, compose, settings) if drain_record(run, compose, settings, callers) else None
    if record is None:
        return False
    try:
        calls = [convert_record(each).model_dump_json() for each in read_records(record)]
    except ValidationError:
        return False
    call_log.with_suffix(".otlp.jsonl").write_text(record, encoding="utf-8")
    call_log.write_text("".join(f"{call}\n" for call in calls), encoding="utf-8")
    return True


def start_gateway(run: RunCompose, compose: list[str], settings: LocArenaSettings) -> bool:
    """Start the gateway; whether it accepts connections on its published port within the probes' bounds."""
    stack = settings.stack
    if run([*compose, "up", "--detach", "--wait", MODEL_GATEWAY_SERVICE]).returncode != 0:
        return False
    for _ in range(stack.gateway_health_retries):
        published = run(
            [*compose, "port", MODEL_GATEWAY_SERVICE, str(settings.gateway.port)],
            capture_output=True,
            text=True,
        )
        host, _, port = published.stdout.strip().rpartition(":")
        if published.returncode == 0 and port.isdigit():
            try:
                socket.create_connection((host, int(port)), timeout=2).close()
            except OSError:
                pass
            else:
                return True
        time.sleep(stack.gateway_health_interval_seconds)
    return False
