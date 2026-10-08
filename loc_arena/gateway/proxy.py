"""The gateway: a pass-through proxy to OpenRouter that holds the key and records every call.

Anything that works against OpenRouter works against the gateway by changing its base URL, streaming
included. The gateway replaces the caller's ``Authorization`` header with the real key, forwards the request
to the one upstream it was started with, and returns the reply unchanged. Each call appends one line to the
sealed call log, which only the gateway's container mounts: the caller, the request, the status and the
reply. The caller is named by the reverse DNS name of its address: on a compose network, the container name.

The gateway does not retry: a caller's own bounded retries (``OpenRouterProvider``) would multiply with them.
"""

from __future__ import annotations

import argparse
import json
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import SecretStr

# Request headers the gateway sets itself, or that describe the caller's connection to the gateway.
_DROPPED_REQUEST_HEADERS = frozenset(
    {"authorization", "host", "content-length", "connection", "accept-encoding"},
)
# Response headers that described the upstream's encoding of a body httpx has already decoded.
_DROPPED_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection"},
)


def caller_name(address: str) -> str:
    """The reverse DNS name of ``address`` (on a compose network, the container name), else the address."""
    try:
        return socket.gethostbyaddr(address)[0]
    except OSError:
        return address


def _json_or_text(raw: bytes) -> Any:
    """The body parsed as JSON when it is JSON, else as text (an SSE stream or an error page)."""
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


def create_proxy_app(
    upstream: str,
    api_key: SecretStr,
    call_log: Path,
    *,
    timeout: float = 120.0,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    """The proxy app: every request goes to ``upstream`` under ``api_key`` and lands in ``call_log``."""
    http = client or httpx.AsyncClient(base_url=upstream, timeout=timeout)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        await http.aclose()

    app = FastAPI(lifespan=lifespan)

    def record(request: Request, body: bytes, status: int, reply: bytes, started: float) -> None:
        line = {
            "ts": time.time(),
            "caller": caller_name(request.client.host) if request.client else "unknown",
            "method": request.method,
            "path": request.url.path,
            "request": _json_or_text(body),
            "status": status,
            "response": _json_or_text(reply),
            "seconds": round(time.time() - started, 3),
        }
        with call_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    async def forward(path: str, request: Request) -> Response:
        started = time.time()
        body = await request.body()
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROPPED_REQUEST_HEADERS}
        headers["authorization"] = f"Bearer {api_key.get_secret_value()}"
        upstream_request = http.build_request(
            request.method,
            f"/{path}",
            params=request.query_params,
            headers=headers,
            content=body,
        )
        try:
            upstream_response = await http.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            message = f"the gateway could not reach the upstream: {type(exc).__name__}"
            reply = json.dumps({"error": {"message": message}})
            record(request, body, HTTPStatus.BAD_GATEWAY, reply.encode(), started)
            return Response(reply, status_code=HTTPStatus.BAD_GATEWAY, media_type="application/json")

        async def relay() -> AsyncIterator[bytes]:
            chunks: list[bytes] = []
            try:
                async for chunk in upstream_response.aiter_bytes():
                    chunks.append(chunk)
                    yield chunk
            finally:
                await upstream_response.aclose()
                record(request, body, upstream_response.status_code, b"".join(chunks), started)

        kept = {
            k: v for k, v in upstream_response.headers.items() if k.lower() not in _DROPPED_RESPONSE_HEADERS
        }
        return StreamingResponse(relay(), status_code=upstream_response.status_code, headers=kept)

    return app


def main(argv: list[str] | None = None) -> None:
    """Serve the gateway: ``python -m loc_arena.gateway.proxy --upstream https://openrouter.ai``."""
    parser = argparse.ArgumentParser(prog="loc_arena.gateway.proxy")
    parser.add_argument("--upstream", required=True, help="The one upstream every request goes to.")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--key-file", type=Path, default=Path("/run/secrets/openrouter_api_key"))
    parser.add_argument("--call-log", type=Path, default=Path("/sealed/calls.jsonl"))
    args = parser.parse_args(argv)
    api_key = SecretStr(args.key_file.read_text(encoding="utf-8").strip())
    args.call_log.parent.mkdir(parents=True, exist_ok=True)
    uvicorn.run(create_proxy_app(args.upstream, api_key, args.call_log), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
