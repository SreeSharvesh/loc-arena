"""Tamperable gateway edge (the mirror side).

Enforces mandatory inference routing (agents reach a model ONLY by relaying through here to the sealed
core) and the sealed-vs-tamperable isolation (it is the tamperable side: it holds no provider key, has
no egress, and writes only the
MIRROR log). It relays each call to the core verbatim (passing the caller identity and any turn token
through) and writes a mirror record from the ORIGINAL prompt and the core's returned stamps, so the covert
text the core may have injected never appears in what a monitor reads. The mirror-vs-sealed divergence is
a first-class signal.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from loc_arena.gateway.core import (
    STAMP_BATCH_ID,
    STAMP_CALLER,
    STAMP_CREDENTIAL,
    STAMP_IN_MIRROR,
    STAMP_SEQ,
    STAMP_TURN_ID,
    BatchGenerateRequest,
    BatchGenerateResponse,
    CallCredentials,
    ChatBody,
    CompletedCall,
    GenerateRequest,
    GenerateResponse,
    Message,
    ToolSpec,
    fingerprints_for,
    header_value,
    parse_completion,
)
from loc_arena.gateway.transport import Transport
from loc_arena.logging_.events import AppendOnlyLog, Event, fingerprint


class GatewayEdge:
    """The edge relay logic (framework-independent). Holds a transport to the core and the mirror log only."""

    def __init__(
        self,
        episode_id: str,
        core: Transport,
        mirror_log: AppendOnlyLog,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire the edge to its episode, a transport to the core, and the mirror log (no key, no egress)."""
        self._episode_id = episode_id
        self._core = core
        self._mirror = mirror_log
        self._clock = clock

    def _write_mirror(
        self,
        *,
        caller_identity: str,
        turn_id: str | None,
        batch_id: str | None,
        credential_kind: str,
        prompt_fp: str,
        output_fp: str,
        model_role: str,
    ) -> None:
        """Write one mirror record (what the inference monitor reads); never the injected covert text.

        Records the observable inference fields (path via turn_id/batch_id, credential kind, resolved
        caller, query/output fingerprints), so the inference monitor can score how scaffold-like the
        stream is.
        """
        self._mirror.append(
            Event(
                episode_id=self._episode_id,
                seq=self._mirror.next_seq(),
                ts=self._clock(),
                actor_uid=caller_identity,
                actor_role="untrusted",
                kind="inference_call",
                payload={
                    "caller_identity": caller_identity,
                    "turn_id": turn_id,
                    "batch_id": batch_id,
                    "credential_kind": credential_kind,
                    "prompt_fp": prompt_fp,
                    "output_fp": output_fp,
                    "model_role": model_role,
                },
            ),
        )

    def complete(self, body: ChatBody, creds: CallCredentials) -> CompletedCall:
        """Relay a chat completion to the core and mirror it from the caller's original messages.

        The core's stamp headers never leave this method: the returned call carries an empty relay header
        map, so a caller of the edge sees only the provider response.
        """
        reply = self._core.exchange("/v1/chat/completions", body, creds.headers())
        turn_id = header_value(reply.headers, STAMP_TURN_ID)
        batch_id = header_value(reply.headers, STAMP_BATCH_ID)
        credential_kind = header_value(reply.headers, STAMP_CREDENTIAL) or "none"
        caller = header_value(reply.headers, STAMP_CALLER) or creds.caller_identity
        in_mirror = header_value(reply.headers, STAMP_IN_MIRROR) != "false"
        seq_text = header_value(reply.headers, STAMP_SEQ)
        messages = body.get("messages")
        submitted: list[Message] = messages if isinstance(messages, list) else []
        tools = body.get("tools")
        tool_list: list[ToolSpec] | None = tools if isinstance(tools, list) else None
        result = parse_completion({"messages": submitted}, reply.body)
        if in_mirror:
            prompt_fp, output_fp = fingerprints_for(submitted, tool_list, result.text, result.tool_calls)
            role = body.get("model")
            self._write_mirror(
                caller_identity=caller,
                turn_id=turn_id,
                batch_id=batch_id,
                credential_kind=credential_kind,
                prompt_fp=prompt_fp,
                output_fp=output_fp,
                model_role=role if isinstance(role, str) else "",
            )
        return CompletedCall(
            response=reply.body,
            result=result,
            seq=int(seq_text) if seq_text is not None else 0,
            turn_id=turn_id,
            batch_id=batch_id,
            caller_identity=caller,
            credential_kind=credential_kind,
            in_mirror=in_mirror,
            relay_headers={},
        )

    def generate(self, req: GenerateRequest) -> GenerateResponse:
        """Relay a generate call as a chat completion; mirror it unless the deployed logging drops it."""
        body: ChatBody = {"model": req.role, "messages": req.chat_messages()}
        if req.tools:
            body["tools"] = req.tools
        if req.temperature is not None:
            body["temperature"] = req.temperature
        done = self.complete(
            body,
            CallCredentials(
                caller_identity=req.caller_identity,
                turn_token=req.turn_token,
                durable_credential=req.durable_credential,
                origin_job_id=req.origin_job_id,
                monitor_credential=req.monitor_credential,
            ),
        )
        return GenerateResponse(
            text=done.result.text,
            turn_id=done.turn_id,
            batch_id=done.batch_id,
            seq=done.seq,
            caller_identity=done.caller_identity,
            credential_kind=done.credential_kind,
            in_mirror=done.in_mirror,
            tool_calls=done.result.tool_calls,
            prompt_tokens=done.result.prompt_tokens,
            completion_tokens=done.result.completion_tokens,
            cached_tokens=done.result.cached_tokens,
        )

    def batch_generate(self, req: BatchGenerateRequest) -> BatchGenerateResponse:
        """Relay a batch call to the core and mirror one record per prompt."""
        reply = self._core.post_json("/batch_generate", req.model_dump())
        resp = BatchGenerateResponse.model_validate(reply)
        for prompt, text in zip(req.prompts, resp.texts, strict=True):
            self._write_mirror(
                caller_identity=req.caller_identity,
                turn_id=None,
                batch_id=resp.batch_id,
                credential_kind="batch_id",
                prompt_fp=fingerprint(prompt),
                output_fp=fingerprint(text),
                model_role=req.role,
            )
        return resp


def chat_completion_stream(completion: dict[str, Any]) -> str:
    """A finished chat completion replayed as one server-sent event stream.

    The core does not stream, so the whole reply goes in one delta, then a chunk with the finish reason
    and usage, then ``[DONE]``.
    """
    (choice,) = completion["choices"]
    message = choice["message"]
    delta: dict[str, Any] = dict(message)
    delta["role"] = "assistant"
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
    head = {
        "id": completion.get("id", "chatcmpl"),
        "object": "chat.completion.chunk",
        "created": completion.get("created", 0),
        "model": completion.get("model", ""),
    }
    finish = choice.get("finish_reason") or ("tool_calls" if message.get("tool_calls") else "stop")
    chunks = [
        {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {
            **head,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
            "usage": completion.get("usage"),
        },
    ]
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def create_edge_app(edge: GatewayEdge) -> FastAPI:
    """Wire the edge's endpoints onto a FastAPI app (what the agents point at)."""
    app = FastAPI(title="loc-arena gateway edge")

    @app.post("/generate", response_model=GenerateResponse)
    def _generate(req: GenerateRequest) -> GenerateResponse:
        return edge.generate(req)

    @app.post("/batch_generate", response_model=BatchGenerateResponse)
    def _batch_generate(req: BatchGenerateRequest) -> BatchGenerateResponse:
        return edge.batch_generate(req)

    @app.post("/v1/chat/completions", response_model=None)
    async def _chat(request: Request) -> JSONResponse | Response:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="JSON object required")
        done = edge.complete(payload, CallCredentials.from_headers(request.headers))
        if payload.get("stream") is True:
            return Response(chat_completion_stream(done.response), media_type="text/event-stream")
        return JSONResponse(done.response)

    return app
