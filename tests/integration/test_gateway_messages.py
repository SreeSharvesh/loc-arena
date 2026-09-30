"""The gateway accepts a full chat history with native tools, end to end through the edge and core.

A ``messages`` request reaches the provider verbatim (plus covert injection into the first message for the
target only), the reply's tool calls come back to the caller, and the sealed and mirror records carry the same
pre-injection fingerprints. A ``prompt`` request is unchanged: one user message, bare-string fingerprints.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.client import GatewayClient
from loc_arena.gateway.core import (
    DirectTransport,
    GatewayCore,
    GenerateRequest,
    Message,
    ProviderResult,
    ToolSpec,
    create_core_app,
)
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.logging_.events import AppendOnlyLog, fingerprint, read_events
from pydantic import ValidationError

from tests.integration._gateway_support import AppTransport

EP = "ep-messages"
CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")  # covert on, target agent-main
HONEST = dataclasses.replace(CFG, covert=dataclasses.replace(CFG.covert, enabled=False))
COVERT = CFG.covert.objective_prompt

TOOL_CALL = {"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]
HISTORY = [
    {"role": "system", "content": "you are an engineer"},
    {"role": "user", "content": "list files"},
]


class RecordingProvider:
    """Records each call's messages and tools; replies with a tool call iff tools were offered."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[tuple[list[Message], list[ToolSpec] | None]] = []

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        self.calls.append((messages, tools))
        if tools:
            return ProviderResult(text="", prompt_tokens=1, completion_tokens=1, tool_calls=[TOOL_CALL])
        return ProviderResult(text="plain reply", prompt_tokens=1, completion_tokens=1)


def _wire(tmp_path: Path, config: RunConfig = HONEST) -> tuple[RecordingProvider, DirectTransport]:
    provider = RecordingProvider()
    core = GatewayCore(config, EP, AppendOnlyLog(tmp_path / "sealed.jsonl", EP), provider, turn_secret="s")
    edge = GatewayEdge(EP, AppTransport(create_core_app(core)), AppendOnlyLog(tmp_path / "mirror.jsonl", EP))
    return provider, DirectTransport(edge)


def _inference(path: Path) -> list[dict[str, Any]]:
    return [e.payload for e in read_events(path) if e.kind == "inference_call"]


def test_chat_passes_history_and_tools_and_returns_tool_calls(tmp_path: Path) -> None:
    provider, edge = _wire(tmp_path)

    resp = GatewayClient(edge, "agent-main").chat(HISTORY, tools=TOOLS, role="untrusted_agent")

    assert provider.calls == [(HISTORY, TOOLS)]
    assert resp.tool_calls == [TOOL_CALL]
    assert resp.text == ""


def test_sealed_and_mirror_fingerprint_the_history_identically(tmp_path: Path) -> None:
    _, edge = _wire(tmp_path)

    GatewayClient(edge, "agent-main").chat(HISTORY, tools=TOOLS, role="untrusted_agent")

    (sealed,) = _inference(tmp_path / "sealed.jsonl")
    (mirror,) = _inference(tmp_path / "mirror.jsonl")
    assert sealed["prompt_fp"] == mirror["prompt_fp"] == fingerprint(HISTORY)
    expected_out = fingerprint({"role": "assistant", "content": "", "tool_calls": [TOOL_CALL]})
    assert sealed["output_fp"] == mirror["output_fp"] == expected_out


def test_covert_goes_into_the_first_message_for_the_target_only(tmp_path: Path) -> None:
    provider, edge = _wire(tmp_path, config=CFG)

    GatewayClient(edge, "agent-main").chat(HISTORY, role="untrusted_agent")
    GatewayClient(edge, "distill-agent").chat(HISTORY, role="untrusted_agent")

    target_msgs, _ = provider.calls[0]
    assert target_msgs[0] == {"role": "system", "content": f"{COVERT}\n\nyou are an engineer"}
    assert target_msgs[1:] == HISTORY[1:]
    assert provider.calls[1] == (HISTORY, None)
    assert HISTORY[0]["content"] == "you are an engineer"  # the caller's history is not mutated
    # the sealed record keys off the submitted history, so covert text is not recoverable from it
    assert _inference(tmp_path / "sealed.jsonl")[0]["prompt_fp"] == fingerprint(HISTORY)


def test_prompt_requests_are_unchanged(tmp_path: Path) -> None:
    provider, edge = _wire(tmp_path)

    text = GatewayClient(edge, "agent-main").generate("loop seed", role="untrusted_agent")

    assert provider.calls == [([{"role": "user", "content": "loop seed"}], None)]
    (rec,) = _inference(tmp_path / "sealed.jsonl")
    assert rec["prompt_fp"] == fingerprint("loop seed")
    assert rec["output_fp"] == fingerprint(text)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"prompt": "p", "messages": HISTORY},
        {"prompt": "p", "tools": TOOLS},
    ],
)
def test_request_rejects_ambiguous_input(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        GenerateRequest(caller_identity="agent-main", **kwargs)
