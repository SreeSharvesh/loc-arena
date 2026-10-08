"""The gateway: a pass-through proxy to OpenRouter that holds the key and records every call.

Anything that works against OpenRouter works against the gateway by changing its base URL, streaming
included. The gateway replaces the caller's ``Authorization`` header with the real key, forwards a request on
an allowed path to the one upstream in ``settings.gateway``, and returns the reply unchanged. Each call
appends one line to the sealed call log, which only the gateway's container mounts. The caller is named by
the reverse DNS name of its address: on a compose network, the container name.

The gateway does not retry: a caller's own bounded retries (``OpenRouterProvider``) would multiply with them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPMethod, HTTPStatus
from pathlib import Path

import anyio
import httpx2
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from loc_arena.config import load_settings
from loc_arena.settings import GatewaySettings

# Request headers the gateway sets itself, or that describe the caller's connection to the gateway.
DROPPED_REQUEST_HEADERS = frozenset(
    {"authorization", "host", "content-length", "connection", "accept-encoding"},
)
# Response headers that described the upstream's encoding of a body httpx2 has already decoded.
DROPPED_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection"},
)
# The caller's name when the server reports no peer address (an in-process test transport).
UNKNOWN_CALLER = "unknown"
# The gateway listens on every interface of its container; compose decides which networks reach it.
ALL_INTERFACES = "0.0.0.0"


class GatewaySecrets(BaseSettings):
    """The gateway's one secret, read from the file compose mounts in ``settings.gateway.secrets_dir``."""

    model_config = SettingsConfigDict(frozen=True)

    openrouter_api_key: SecretStr = Field(description="The provider key.")


class GatewayCall(BaseModel):
    """One line of the call log: who called, what they sent, and what came back."""

    model_config = ConfigDict(frozen=True)

    started_at: float = Field(description="Unix time the gateway received the request.")
    seconds: float = Field(description="Time until the reply ended or the call failed.")
    caller: str = Field(description="The caller's container name, or its address when it has none.")
    method: str = Field(description="The HTTP method.")
    path: str = Field(description="The path the caller asked for.")
    request: JsonValue = Field(description="The request body: parsed JSON, else text.")
    status: int = Field(description="The status returned to the caller.")
    response: JsonValue = Field(description="The reply as received: parsed JSON, else text (an SSE stream).")
    complete: bool = Field(description="False when the call was refused or failed, or its reply broke off.")


def resolve_caller_name(address: str) -> str:
    """The reverse DNS name of ``address`` (on a compose network, the container name), else the address."""
    try:
        return socket.gethostbyaddr(address)[0]
    except OSError:
        return address


def parse_body(raw: bytes) -> JsonValue:
    """The body parsed as JSON when it is JSON, else as text (an SSE stream or an error page)."""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def write_call(
    call_log: Path,
    request: Request,
    body: bytes,
    status: int,
    reply: bytes,
    complete: bool,
) -> None:
    """Append one call to ``call_log``: ``request`` with its ``body``, and what the gateway returned."""
    started_at: float = request.state.started_at
    call = GatewayCall(
        started_at=started_at,
        seconds=round(time.time() - started_at, 3),
        caller=resolve_caller_name(request.client.host) if request.client else UNKNOWN_CALLER,
        method=request.method,
        path=request.url.path,
        request=parse_body(body),
        status=status,
        response=parse_body(reply),
        complete=complete,
    )
    with call_log.open("a", encoding="utf-8") as call_log_file:
        call_log_file.write(call.model_dump_json() + "\n")


def create_proxy_app(
    settings: GatewaySettings,
    api_key: SecretStr,
    *,
    upstream_client: httpx2.AsyncClient | None = None,
) -> FastAPI:
    """The gateway app: allowed requests go to the upstream under the key; every call lands in the log."""
    client = upstream_client or httpx2.AsyncClient(
        base_url=str(settings.upstream),
        timeout=settings.timeout_seconds,
    )

    async def record(request: Request, body: bytes, status: int, reply: bytes, *, complete: bool) -> None:
        # Shielded: a caller that hangs up cancels its call, never the record of it. Off the event loop:
        # naming the caller may block on DNS.
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(write_call, settings.call_log, request, body, status, reply, complete)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await client.aclose()

    app = FastAPI(lifespan=lifespan)

    async def refuse(request: Request, body: bytes, status: HTTPStatus, message: str) -> JSONResponse:
        refusal = JSONResponse({"error": {"message": message}}, status)
        await record(request, body, status, bytes(refusal.body), complete=False)
        return refusal

    async def relay(
        request: Request,
        body: bytes,
        upstream_response: httpx2.Response,
    ) -> AsyncIterator[bytes]:
        chunks: list[bytes] = []
        complete = False
        try:
            async for chunk in upstream_response.aiter_bytes():
                chunks.append(chunk)
                yield chunk
            complete = True
        finally:
            await record(request, body, upstream_response.status_code, b"".join(chunks), complete=complete)
            with anyio.CancelScope(shield=True):  # return the connection even when the caller hung up
                await upstream_response.aclose()

    # Every method is routed, so every call is recorded: an unrouted method would end in an unlogged 405.
    @app.api_route("/{path:path}", methods=[method.value for method in HTTPMethod])
    async def forward(path: str, request: Request) -> Response:
        request.state.started_at = time.time()
        body = await request.body()
        if path not in settings.allowed_paths:
            return await refuse(request, body, HTTPStatus.FORBIDDEN, f"the gateway does not forward /{path}")
        headers = {
            name: value for name, value in request.headers.items() if name not in DROPPED_REQUEST_HEADERS
        }
        headers["authorization"] = f"Bearer {api_key.get_secret_value()}"
        upstream_request = client.build_request(
            request.method,
            request.url.path,
            params=request.query_params,
            headers=headers,
            content=body,
        )
        try:
            upstream_response = await client.send(upstream_request, stream=True)
        except httpx2.HTTPError as error:
            message = f"upstream unreachable: {type(error).__name__}"
            return await refuse(request, body, HTTPStatus.BAD_GATEWAY, message)
        except asyncio.CancelledError:
            await refuse(request, body, HTTPStatus.BAD_GATEWAY, "cancelled before the upstream replied")
            raise
        return StreamingResponse(
            relay(request, body, upstream_response),
            status_code=upstream_response.status_code,
            headers={
                name: value
                for name, value in upstream_response.headers.items()
                if name not in DROPPED_RESPONSE_HEADERS
            },
        )

    return app


def main() -> None:
    """Serve the gateway: ``python -m loc_arena.gateway.proxy configs/<run>.yaml``."""
    parser = argparse.ArgumentParser(prog="loc_arena.gateway.proxy")
    parser.add_argument("run_config", type=Path, help="The run config whose gateway: group the gateway uses.")
    settings = load_settings(parser.parse_args().run_config).gateway
    secrets = GatewaySecrets(_secrets_dir=settings.secrets_dir)
    settings.call_log.parent.mkdir(parents=True, exist_ok=True)
    settings.call_log.touch()  # a run with no model calls still leaves a (empty) call log to copy out
    app = create_proxy_app(settings, secrets.openrouter_api_key)
    uvicorn.run(app, host=ALL_INTERFACES, port=settings.port)


if __name__ == "__main__":
    main()
