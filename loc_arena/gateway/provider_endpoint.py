"""The provider endpoint: the one place a model call leaves the stack when an episode runs in containers.

``gateway_core`` runs :func:`main`. It is the only service on the egress network and the only holder of the
provider key. The episode runner has neither; its :class:`RemoteProvider` posts each completion to this
endpoint over the internal control network. The endpoint serves only the models the run config routes to,
capped at their configured ``max_tokens``, and never substitutes a stub: with no key it answers 503.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TypedDict, cast

import httpx

from loc_arena.config import ModelSpec
from loc_arena.gateway.core import OpenRouterProvider, Provider, ProviderResult
from loc_arena.gateway.provider_key import DOCKER_SECRETS_DIRECTORY, load_provider_key

PROVIDER_URL_ENV = "LOC_ARENA_PROVIDER_URL"  # set only in the runner: route completions to the endpoint
MODEL_ALLOWLIST_ENV = "LOC_ARENA_MODEL_ALLOWLIST"  # JSON ModelAllowlist, rendered from the run config
PORT_ENV = "SVC_PORT"

_MAX_REQUEST_BYTES = 8 * 1024 * 1024  # prompts carry whole transcripts; still bound what one request can send
# Longer than the core's worst case (6 attempts x 60s timeout + 5 backoffs x 30s = 510s), so the runner
# never gives up on a call the core is still retrying.
_CLIENT_TIMEOUT_SECONDS = 600.0

type ModelAllowlist = dict[str, int]  # model id -> the largest max_tokens the endpoint serves it at


class CompletionRequestBody(TypedDict):
    """The JSON body of ``POST /generate``."""

    model: str
    prompt: str
    temperature: float
    max_tokens: int


class CompletionResponseBody(TypedDict):
    """The JSON body of a successful ``POST /generate``."""

    text: str
    prompt_tokens: int
    completion_tokens: int


class HealthResponseBody(TypedDict):
    """The JSON body of ``GET /health``."""

    ok: bool
    provider_configured: bool


class ErrorResponseBody(TypedDict):
    """The JSON body of any refused or failed request (a short, sanitized reason)."""

    error: str


type ResponseBody = CompletionResponseBody | HealthResponseBody | ErrorResponseBody


@dataclass(frozen=True)
class CompletionRequest:
    """One validated completion request."""

    model: str
    prompt: str
    temperature: float
    max_tokens: int

    def to_body(self) -> CompletionRequestBody:
        """The JSON body that carries this request."""
        return {
            "model": self.model,
            "prompt": self.prompt,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    @classmethod
    def from_json(cls, raw: bytes) -> CompletionRequest | None:
        """Parse and validate a request body; ``None`` if it is not a well-formed completion request."""
        try:
            body = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        model, prompt = body.get("model"), body.get("prompt")
        temperature, max_tokens = body.get("temperature"), body.get("max_tokens")
        if not isinstance(model, str) or not isinstance(prompt, str):
            return None
        if not isinstance(temperature, int | float) or not isinstance(max_tokens, int) or max_tokens <= 0:
            return None
        return cls(model=model, prompt=prompt, temperature=float(temperature), max_tokens=max_tokens)


def build_model_allowlist(models: dict[str, ModelSpec]) -> ModelAllowlist:
    """The endpoint allowlist for a run: each routed model id and its largest configured ``max_tokens``."""
    allowlist: ModelAllowlist = {}
    for spec in models.values():
        allowlist[spec.model] = max(allowlist.get(spec.model, 0), spec.max_tokens)
    return allowlist


class RemoteProvider:
    """A keyless provider that forwards each completion to the provider endpoint on ``gateway_core``.

    It raises on any failure rather than falling back to another provider, so a run without a working
    endpoint stops instead of silently scoring a stub.
    """

    def __init__(self, base_url: str, *, timeout_seconds: float = _CLIENT_TIMEOUT_SECONDS) -> None:
        """Hold the endpoint's base URL (e.g. ``http://gateway-core:4000``) and the per-call timeout."""
        self._generate_url = base_url.rstrip("/") + "/generate"
        self._timeout_seconds = timeout_seconds

    def generate(self, model: str, prompt: str, temperature: float, max_tokens: int) -> ProviderResult:
        """POST one completion request to the endpoint and return its result."""
        request = CompletionRequest(
            model=model,
            prompt=prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        response = httpx.post(self._generate_url, json=request.to_body(), timeout=self._timeout_seconds)
        if response.status_code != 200:
            raise RuntimeError(
                f"provider endpoint returned {response.status_code}: {_error_message(response)}",
            )
        body = cast(CompletionResponseBody, response.json())
        return ProviderResult(
            text=str(body["text"]),
            prompt_tokens=int(body["prompt_tokens"]),
            completion_tokens=int(body["completion_tokens"]),
        )


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase
    reason = body.get("error") if isinstance(body, dict) else None
    return str(reason) if reason else response.reason_phrase


def live_provider_from_env() -> Provider | None:
    """The live provider for this process: the endpoint when running in the stack, else the direct key.

    ``None`` means no live provider is configured (the caller decides what that means for its run).
    """
    endpoint_url = os.environ.get(PROVIDER_URL_ENV)
    if endpoint_url:
        return RemoteProvider(endpoint_url)
    if os.environ.get("OPENROUTER_API_KEY"):
        return OpenRouterProvider()
    return None


class ProviderEndpointServer(ThreadingHTTPServer):
    """The endpoint's HTTP server: one thread per request (async monitors call it concurrently)."""

    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        provider: Provider | None,
        model_allowlist: ModelAllowlist,
    ) -> None:
        """Bind ``address``; ``provider=None`` (no key) makes ``/generate`` answer 503."""
        super().__init__(address, ProviderEndpointHandler)
        self.provider = provider
        self.model_allowlist = model_allowlist


class ProviderEndpointHandler(BaseHTTPRequestHandler):
    """Routes ``GET /health`` and ``POST /generate``."""

    server: ProviderEndpointServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - matches the base signature
        """Silence per-request access logging (prompts must not reach the container log)."""

    def _send_json(self, status: int, body: ResponseBody) -> None:
        encoded = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - http.server's handler naming
        """Answer ``/health`` (200 even without a key, so the stack can come up for isolation tests)."""
        if self.path == "/health":
            self._send_json(200, {"ok": True, "provider_configured": self.server.provider is not None})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - http.server's handler naming
        """Serve ``/generate``: validate, enforce the allowlist and token cap, call the provider."""
        if self.path != "/generate":
            self._send_json(404, {"error": "not found"})
            return
        request = self._read_completion_request()
        if request is None:
            self._send_json(400, {"error": "bad request"})
            return
        if request.model not in self.server.model_allowlist:
            self._send_json(403, {"error": "model not allowed"})
            return
        if self.server.provider is None:
            self._send_json(503, {"error": "provider key not configured"})
            return
        capped_max_tokens = min(request.max_tokens, self.server.model_allowlist[request.model])
        try:
            result = self.server.provider.generate(
                request.model,
                request.prompt,
                request.temperature,
                capped_max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 - report a sanitized failure; never echo provider internals
            print(f"provider call failed: {type(exc).__name__}", file=sys.stderr, flush=True)
            self._send_json(502, {"error": "provider call failed"})
            return
        self._send_json(
            200,
            {
                "text": result.text,
                "prompt_tokens": result.prompt_tokens,
                "completion_tokens": result.completion_tokens,
            },
        )

    def _read_completion_request(self) -> CompletionRequest | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > _MAX_REQUEST_BYTES:
            return None
        return CompletionRequest.from_json(self.rfile.read(length))


def make_server(
    provider: Provider | None,
    model_allowlist: ModelAllowlist,
    *,
    host: str = "0.0.0.0",  # noqa: S104 - the container's internal interfaces; networks bound it
    port: int = 4000,
) -> ProviderEndpointServer:
    """Build (not start) the endpoint server; ``provider=None`` (no key) makes /generate answer 503."""
    return ProviderEndpointServer((host, port), provider, model_allowlist)


def main() -> None:
    """Run the endpoint on ``gateway_core``: key from its compose secret, allowlist from the environment."""
    key = load_provider_key(secrets_directory=DOCKER_SECRETS_DIRECTORY)
    raw_allowlist = json.loads(os.environ.get(MODEL_ALLOWLIST_ENV, "{}"))
    model_allowlist: ModelAllowlist = {str(model): int(cap) for model, cap in raw_allowlist.items()}
    provider = OpenRouterProvider(api_key=key.get_secret_value()) if key is not None else None
    port = int(os.environ.get(PORT_ENV, "4000"))
    served_models = ", ".join(sorted(model_allowlist))
    print(
        f"provider endpoint on :{port}; key configured: {provider is not None}; models: {served_models}",
        flush=True,
    )
    make_server(provider, model_allowlist, port=port).serve_forever()


if __name__ == "__main__":
    main()
