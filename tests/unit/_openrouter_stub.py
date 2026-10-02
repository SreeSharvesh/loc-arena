"""A local stand-in for OpenRouter's chat completions route, for the provider tests (loopback, fake key)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from loc_arena.gateway.core import ProviderResult
from loc_arena.gateway.openrouter_provider import OpenRouterProvider
from loc_arena.stack.settings import ProviderSettings
from pydantic import SecretStr

CANARY_KEY = "sk-or-v1-canary-p1-provider-test-key"
MODEL = "vendor/model"
PROMPT = "hi"
MESSAGES = [{"role": "user", "content": PROMPT}]
TEMPERATURE = 0.5
MAX_TOKENS = 16
TRICKLE_INTERVAL_SECONDS = 0.1  # one byte every 100 ms: each chunk resets httpx's read timeout
TRICKLE_MAX_CHUNKS = 100  # a trickle the client never hangs up on ends after ~10 s, so no test can hang
TRICKLE_DECLARED_BYTES = 1_000_000
SHUTDOWN_POLL_SECONDS = 0.01
USAGE: Mapping[str, int] = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


@dataclass(frozen=True)
class ScriptedReply:
    """One reply: a status, headers and a JSON body, or bytes trickled until the client hangs up."""

    status: HTTPStatus = HTTPStatus.OK
    body: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    trickle: bool = False


@dataclass(frozen=True)
class ReceivedRequest:
    """What the stub saw of one request."""

    path: str
    authorization: str
    body: str


class StubOpenRouter(ThreadingHTTPServer):
    """Answers each POST with the next scripted reply (the last one repeats) and records every request."""

    daemon_threads = False  # server_close() joins every handler, so a finished stub leaves no thread behind

    def __init__(self, replies: Sequence[ScriptedReply]) -> None:
        """Bind a free loopback port."""
        super().__init__(("127.0.0.1", 0), StubOpenRouterHandler)
        self.pending_replies = list(replies)
        self.received: list[ReceivedRequest] = []
        self.client_hung_up = threading.Event()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/api/v1"

    def take_reply(self) -> ScriptedReply:
        return self.pending_replies.pop(0) if len(self.pending_replies) > 1 else self.pending_replies[0]


class StubOpenRouterHandler(BaseHTTPRequestHandler):
    """Serves one request from the stub's script."""

    server: StubOpenRouter

    def log_message(self, format: str, *args: object) -> None:
        """Keep test output quiet."""

    def do_POST(self) -> None:  # noqa: N802 - http.server's handler naming
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        self.server.received.append(ReceivedRequest(self.path, self.headers["Authorization"], body))
        reply = self.server.take_reply()
        payload = reply.body.encode()
        self.send_response(reply.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(TRICKLE_DECLARED_BYTES if reply.trickle else len(payload)))
        for name, value in reply.headers.items():
            self.send_header(name, value)
        self.end_headers()
        if reply.trickle:
            self._write_one_byte_at_a_time()
        else:
            self.wfile.write(payload)

    def _write_one_byte_at_a_time(self) -> None:
        try:
            for _ in range(TRICKLE_MAX_CHUNKS):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(TRICKLE_INTERVAL_SECONDS)
        except (BrokenPipeError, ConnectionResetError):
            self.server.client_hung_up.set()


@contextmanager
def serve_openrouter(*replies: ScriptedReply) -> Iterator[StubOpenRouter]:
    server = StubOpenRouter(replies)
    serving = threading.Thread(target=server.serve_forever, args=(SHUTDOWN_POLL_SECONDS,))
    serving.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        serving.join()


def fast_settings(stub: StubOpenRouter) -> ProviderSettings:
    return ProviderSettings(
        server_url=stub.url,
        backoff_initial_interval_milliseconds=1,
        backoff_max_interval_milliseconds=1,
        rate_limit_max_wait_seconds=0.01,
    )


def completion(
    content: str | None = "hello",
    *,
    finish_reason: str = "stop",
    usage: Mapping[str, object] | None = USAGE,
    tool_calls: Sequence[Mapping[str, object]] | None = None,
) -> str:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = list(tool_calls)
    body: dict[str, object] = {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 1,
        "model": MODEL,
        "system_fingerprint": None,
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": message,
            },
        ],
    }
    if usage is not None:
        body["usage"] = dict(usage)
    return json.dumps(body)


def error_body(status: HTTPStatus, message: str) -> str:
    return json.dumps({"error": {"code": status.value, "message": message}})


def stub_provider(stub: StubOpenRouter, settings: ProviderSettings | None = None) -> OpenRouterProvider:
    return OpenRouterProvider(settings or fast_settings(stub), SecretStr(CANARY_KEY))


def generate(stub: StubOpenRouter, settings: ProviderSettings | None = None) -> ProviderResult:
    return stub_provider(stub, settings).generate(MODEL, MESSAGES, TEMPERATURE, MAX_TOKENS, None)
