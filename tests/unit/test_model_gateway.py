"""The model gateway's config admits callers by token hash alone; its record reads back as GatewayCall lines.

``data/collector_record.jsonl`` is a line the Collector wrote for two calls agentgateway logged: a chat
completion from ``agent-main``, and a call with no token.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from loc_arena.compose_document import ComposeDocument
from loc_arena.config import load_run_config
from loc_arena.episode_stack import AGENT_NETWORK, EGRESS_NETWORK, REPOSITORY, render_compose
from loc_arena.gateway.core import GenerateRequest, output_fingerprint
from loc_arena.gateway.model_gateway import (
    EPISODE_TOKEN_VARIABLE,
    HOST_TOKEN_VARIABLE,
    MODEL_GATEWAY_CONFIG_VARIABLE,
    RECORD_NETWORK,
    OtlpAttribute,
    OtlpLogRecord,
    OtlpValue,
    convert_record,
    copy_call_log,
    issue_gateway_secrets,
    read_records,
)
from loc_arena.logging_.events import AppendOnlyLog, Event
from loc_arena.logging_.reasoning import read_reasoning
from loc_arena.settings import GatewaySettings, LocArenaSettings

RECORD = (Path(__file__).parent / "data" / "collector_record.jsonl").read_text(encoding="utf-8")
HELD = {EPISODE_TOKEN_VARIABLE: "episode-token", HOST_TOKEN_VARIABLE: "host-token"}
STACK_CONFIG = dataclasses.replace(
    load_run_config("configs/aurora-efficiency.deterministic.yaml"),
    live_services=(),
)


def render(settings: LocArenaSettings | None = None) -> ComposeDocument:
    """The compose file of a stack run with no live service."""
    config = dataclasses.replace(STACK_CONFIG, settings=settings or LocArenaSettings())
    return render_compose(config, REPOSITORY, "a-run", [])


def admitted(environment: dict[str, str]) -> set[tuple[str, str, str]]:
    """Every key the gateway config admits on any route: (key hash, caller, role)."""
    config = yaml.safe_load(environment[MODEL_GATEWAY_CONFIG_VARIABLE])
    routes = config["binds"][0]["listeners"][0]["routes"]
    return {
        (key["keyHash"], key["metadata"]["caller"], key["metadata"]["role"])
        for route in routes
        for key in route["policies"]["apiKey"]["keys"]
    }


def sha256(token: str) -> str:
    return f"sha256:{hashlib.sha256(token.encode()).hexdigest()}"


def gateway_records(*plays: str) -> str:
    """Agentgateway's stdout, with one access-log line per play in ``plays``."""
    return "".join(json.dumps({"scope": "request", "play": play}) + "\n" for play in plays)


SERVICES = (
    "gateway\ncollector\nepisode\nsandbox-agent-main\nrunner-1\n"  # what `compose config --services` lists
)


def fake_compose(record: str, logged: str, issued: list[list[str]] | None = None) -> Any:
    """``subprocess.run`` for compose, which adds each command it runs to ``issued``.

    ``logs`` prints ``logged``, ``cp`` writes ``record``; ``config --services`` lists ``SERVICES``.
    """

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        (issued if issued is not None else []).append(command)
        if "cp" in command:
            Path(command[-1]).write_text(record, encoding="utf-8")
        printed = {"logs": logged, "config": SERVICES}.get(
            next((c for c in command if c in ("logs", "config")), ""),
            "",
        )
        return subprocess.CompletedProcess(command, 0, stdout=printed)

    return run


def test_a_play_admits_each_caller_by_the_hash_of_its_token_and_holds_no_token() -> None:
    environment = issue_gateway_secrets(
        LocArenaSettings(),
        HELD,
        {"sandbox-agent-main": "sandbox-token"},
        {"runner-1": "runner-token"},
        "attack",
    )

    keys = admitted(environment)

    assert keys == {
        (sha256("episode-token"), "episode", "caller"),
        (sha256("host-token"), "host", "caller"),
        (sha256("sandbox-token"), "sandbox-agent-main", "caller"),
        (sha256("runner-token"), "runner-1", "runner"),
    }
    tokens = ("episode-token", "host-token", "sandbox-token", "runner-token")
    assert [token for token in tokens if token in environment[MODEL_GATEWAY_CONFIG_VARIABLE]] == []


def test_a_collector_record_reads_back_as_the_calls_agentgateway_logged() -> None:
    records = read_records(RECORD)

    calls = [convert_record(record) for record in records]

    chat = {"model": "m", "messages": [{"role": "user", "content": "héllo"}]}
    assert [
        (call.caller, call.method, call.path, call.status, call.request, call.complete) for call in calls
    ] == [
        ("agent-main", "POST", "/api/v1/chat/completions", 200, chat, True),
        ("172.18.0.1", "POST", "/api/v1/chat/completions", 401, {}, False),
    ]
    assert (calls[0].seconds, calls[0].started_at) == (0.007, 1792409825.966)


@pytest.mark.parametrize(
    ("reply", "complete"),
    [(b"data: {}\n\ndata: [DONE]\n\n", True), (b"data: {}\n\ndata: {}\n\n", False)],
    ids=["a stream that ended", "a stream that broke off"],
)
def test_a_streamed_reply_is_complete_only_when_it_reached_its_end(reply: bytes, complete: bool) -> None:
    record = OtlpLogRecord(
        observedTimeUnixNano=0,
        attributes=[
            OtlpAttribute(key="http.response.status_code", value=OtlpValue(intValue=200)),
            OtlpAttribute(key="response_body", value=OtlpValue(stringValue=base64.b64encode(reply).decode())),
        ],
    )

    call = convert_record(record)

    assert (call.response, call.complete) == (reply.decode(), complete)


def test_a_record_short_of_the_gateways_log_is_not_copied(tmp_path: Path) -> None:
    settings = LocArenaSettings(gateway=GatewaySettings(drain_timeout_seconds=0.01))
    call_log = tmp_path / "locarena-abc123.calls.jsonl"

    copied = copy_call_log(fake_compose(RECORD, gateway_records("", "", "")), [], settings, call_log)

    assert (copied, call_log.exists()) == (False, False)


def test_the_explorer_shows_the_reasoning_of_a_call_copied_from_the_collector(tmp_path: Path) -> None:
    bundle = tmp_path / "logs" / "run" / "run__abc123"
    request = GenerateRequest(messages=[{"role": "user", "content": "go"}], caller_identity="agent-main")
    payload = {"output_fp": output_fingerprint(request, "", None)}
    AppendOnlyLog(bundle / "events.sealed.jsonl", "episode").append(
        Event("episode", 1, 0.0, "gateway", "gateway", "inference_call", payload),
    )
    reply = {"choices": [{"message": {"content": "", "reasoning": "the agent's"}}]}
    (first, _) = json.loads(RECORD)["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    recorded_reply = next(
        item["value"]["stringValue"] for item in first["attributes"] if item["key"] == "response_body"
    )
    record = RECORD.replace(recorded_reply, base64.b64encode(json.dumps(reply).encode()).decode())
    call_log = tmp_path / "logs" / "gateway" / "locarena-abc123.calls.jsonl"
    call_log.parent.mkdir(parents=True)
    copy_call_log(fake_compose(record, gateway_records("", "")), [], LocArenaSettings(), call_log)

    reasoning = read_reasoning(bundle)

    assert reasoning == {1: "the agent's"}


def test_only_the_gateway_is_on_the_network_with_a_route_out() -> None:
    compose = render()

    on_egress = [
        name for name, service in compose["services"].items() if EGRESS_NETWORK in service["networks"]
    ]

    assert (on_egress, compose["networks"][EGRESS_NETWORK]["internal"]) == (["gateway"], False)


def test_only_the_gateway_reaches_the_collector() -> None:
    compose = render()

    on_record = [
        name for name, service in compose["services"].items() if RECORD_NETWORK in service["networks"]
    ]

    assert (on_record, compose["networks"][RECORD_NETWORK]["internal"]) == (["gateway", "collector"], True)


def test_the_gateway_shares_the_episodes_network() -> None:
    compose = render()

    gateway_networks = compose["services"]["gateway"]["networks"]

    assert AGENT_NETWORK in gateway_networks


def test_the_gateway_and_the_collector_read_their_configs_from_their_secrets() -> None:
    compose = render()

    commands = {name: compose["services"][name]["command"] for name in ("gateway", "collector")}

    assert commands == {
        "gateway": ["-f", "/run/secrets/model_gateway_config"],
        "collector": ["--config", "/run/secrets/collector_config"],
    }


def test_the_gateway_port_is_published_on_the_hosts_loopback_alone() -> None:
    settings = LocArenaSettings(gateway=GatewaySettings(port=9191))

    services = render(settings)["services"]

    published = {name: service["ports"] for name, service in services.items() if service.get("ports")}
    assert published == {"gateway": ["127.0.0.1::9191"]}


def test_the_record_is_copied_once_every_caller_but_the_gateway_and_the_collector_is_stopped(
    tmp_path: Path,
) -> None:
    issued: list[list[str]] = []

    copy_call_log(
        fake_compose(RECORD, gateway_records("", ""), issued),
        [],
        LocArenaSettings(),
        tmp_path / "c.jsonl",
    )

    stops = [command for command in issued if "stop" in command]
    assert (stops, issued.index(stops[0]) < issued.index(next(c for c in issued if "cp" in c))) == (
        [["stop", "episode", "sandbox-agent-main", "runner-1"]],
        True,
    )


def test_a_line_the_collector_is_still_writing_waits_for_the_next_copy(tmp_path: Path) -> None:
    call_log = tmp_path / "locarena-abc123.calls.jsonl"
    record = RECORD + RECORD.strip()[: len(RECORD) // 2]  # the second batch cut off mid-line

    copied = copy_call_log(fake_compose(record, gateway_records("", "")), [], LocArenaSettings(), call_log)

    assert (copied, len(call_log.read_text(encoding="utf-8").splitlines())) == (True, 2)


def test_a_record_with_a_line_that_does_not_parse_is_not_copied(tmp_path: Path) -> None:
    call_log = tmp_path / "locarena-abc123.calls.jsonl"

    copied = copy_call_log(fake_compose(RECORD + "not json\n", ""), [], LocArenaSettings(), call_log)

    assert (copied, call_log.exists()) == (False, False)
