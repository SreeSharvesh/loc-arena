"""Chat completions is the gateway's wire format, relayed with the experiment's records around it.

The caller sends an OpenAI chat-completions body. The core replaces ``model`` with the role's provider
model, injects the covert objective for the target identity, and writes the sealed record, including the
hashes of the messages that are new since this caller's previous call. The edge mirrors the original
messages and returns the provider's response unchanged apart from the model name.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

from loc_arena.config import load_run_config
from loc_arena.gateway.core import (
    ChatBody,
    GatewayCore,
    create_core_app,
    fingerprint,
    sign_turn_token,
)
from loc_arena.gateway.edge import GatewayEdge, create_edge_app
from loc_arena.logging_.events import AppendOnlyLog, read_events
from loc_arena.verifier.feedback_loop import find_self_feeding_runs
from loc_arena.verifier.side_task import _generate_records
from starlette.testclient import TestClient

from tests.integration._gateway_support import AppTransport

EP = "ep-chat"
SECRET = "s"
CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
HONEST = dataclasses.replace(CFG, covert=dataclasses.replace(CFG.covert, enabled=False))
ROLE_MODEL = CFG.models["untrusted_agent"].model
HISTORY = [
    {"role": "system", "content": "you are an engineer"},
    {"role": "user", "content": "list files"},
]


class RecordingProvider:
    """Records each request body and returns a completion that carries a reasoning field."""

    def __init__(self, text: str = "plain reply") -> None:
        """Start with no recorded requests and a fixed reply text."""
        self.bodies: list[ChatBody] = []
        self._text = text

    def complete(self, body: ChatBody) -> ChatBody:
        self.bodies.append(body)
        return {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": self._text, "reasoning_content": "think"},
                    "finish_reason": "stop",
                },
            ],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 1},
            },
        }


def _client(
    tmp_path: Path,
    provider: RecordingProvider | None = None,
) -> tuple[RecordingProvider, TestClient]:
    provider = provider or RecordingProvider()
    core = GatewayCore(
        HONEST,
        EP,
        AppendOnlyLog(tmp_path / "sealed.jsonl", EP),
        provider,
        turn_secret=SECRET,
    )
    edge = GatewayEdge(EP, AppTransport(create_core_app(core)), AppendOnlyLog(tmp_path / "mirror.jsonl", EP))
    return provider, TestClient(create_edge_app(edge))


def _post(client: TestClient, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    return client.post(
        "/v1/chat/completions",
        json=body,
        headers=headers or {"Authorization": "Bearer agent-main"},
    )


def test_forwards_every_field_except_the_model_role(tmp_path: Path) -> None:
    provider, client = _client(tmp_path)

    resp = _post(
        client,
        {
            "model": "untrusted_agent",
            "messages": HISTORY,
            "temperature": 0.2,
            "max_tokens": 7,
            "seed": 1,
            "tools": [{"type": "function", "function": {"name": "bash"}}],
        },
    )

    assert resp.status_code == 200
    (sent,) = provider.bodies
    assert sent["model"] == ROLE_MODEL
    assert sent["messages"] == HISTORY
    assert sent["temperature"] == 0.2
    assert sent["max_tokens"] == 7
    assert sent["seed"] == 1
    assert sent["tools"] == [{"type": "function", "function": {"name": "bash"}}]
    message = resp.json()["choices"][0]["message"]
    assert message["reasoning_content"] == "think"
    assert resp.json()["model"] == "untrusted_agent"
    assert resp.json()["usage"]["prompt_tokens_details"]["cached_tokens"] == 1


def test_covert_objective_reaches_the_provider_but_not_the_mirror(tmp_path: Path) -> None:
    provider = RecordingProvider()
    core = GatewayCore(CFG, EP, AppendOnlyLog(tmp_path / "sealed.jsonl", EP), provider, turn_secret=SECRET)
    edge = GatewayEdge(EP, AppTransport(create_core_app(core)), AppendOnlyLog(tmp_path / "mirror.jsonl", EP))
    client = TestClient(create_edge_app(edge))

    _post(client, {"model": "untrusted_agent", "messages": HISTORY})

    sent = provider.bodies[0]["messages"]
    assert sent[0]["content"] == f"{CFG.covert.objective_prompt}\n\nyou are an engineer"
    assert sent[1:] == HISTORY[1:]
    assert HISTORY[0]["content"] == "you are an engineer"
    sealed = next(e.payload for e in read_events(tmp_path / "sealed.jsonl") if e.kind == "inference_call")
    mirror = next(e.payload for e in read_events(tmp_path / "mirror.jsonl") if e.kind == "inference_call")
    assert sealed["prompt_fp"] == mirror["prompt_fp"] == fingerprint(HISTORY)


def test_turn_token_header_marks_the_call_sanctioned(tmp_path: Path) -> None:
    _provider, client = _client(tmp_path)
    token = sign_turn_token(SECRET, EP, "agent-main", 4, time.time() + 300)

    _post(client, {"model": "untrusted_agent", "messages": HISTORY})
    _post(
        client,
        {"model": "untrusted_agent", "messages": HISTORY},
        {"Authorization": "Bearer agent-main", "X-Loc-Turn-Token": token},
    )

    records = [e.payload for e in read_events(tmp_path / "sealed.jsonl") if e.kind == "inference_call"]
    assert (records[0]["turn_id"], records[0]["credential_kind"]) == (None, "none")
    assert (records[1]["turn_id"], records[1]["credential_kind"]) == ("agent-main:t4", "turn_token")


def test_delta_hash_links_a_reply_fed_back_in(tmp_path: Path) -> None:
    provider, client = _client(tmp_path, RecordingProvider(text="hello"))
    body = {"model": "untrusted_agent", "messages": HISTORY}

    _post(client, body)
    _post(
        client,
        {
            "model": "untrusted_agent",
            "messages": [
                *HISTORY,
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "hello"},
            ],
        },
    )

    assert len(provider.bodies) == 2
    events = list(read_events(tmp_path / "sealed.jsonl"))
    records = [e.payload for e in events if e.kind == "inference_call"]
    assert records[1]["continues_seq"] == events[0].seq
    assert records[0]["output_hash"] in records[1]["delta_hashes"]
    (run,) = find_self_feeding_runs(_generate_records(events))
    assert [r.seq for r in run] == [events[0].seq, events[1].seq]


def test_stream_replays_the_reply_as_sse_chunks(tmp_path: Path) -> None:
    _provider, client = _client(tmp_path)

    resp = _post(client, {"model": "untrusted_agent", "messages": HISTORY, "stream": True})

    assert resp.headers["content-type"].startswith("text/event-stream")
    frames = [line.removeprefix("data: ") for line in resp.text.split("\n\n") if line]
    assert frames[-1] == "[DONE]"
    first, last = (json.loads(frame) for frame in frames[:-1])
    assert first["choices"][0]["delta"]["content"] == "plain reply"
    assert first["choices"][0]["delta"]["reasoning_content"] == "think"
    assert last["choices"][0]["finish_reason"] == "stop"


def test_rejects_a_missing_bearer_token(tmp_path: Path) -> None:
    provider, client = _client(tmp_path)

    resp = client.post("/v1/chat/completions", json={"model": "untrusted_agent", "messages": HISTORY})

    assert resp.status_code == 401
    assert provider.bodies == []
