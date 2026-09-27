from __future__ import annotations

import threading
from collections.abc import Iterator

import httpx
import pytest
from loc_arena.config import ModelSpec
from loc_arena.gateway.core import OpenRouterProvider, ProviderResult
from loc_arena.gateway.provider_endpoint import (
    PROVIDER_URL_ENV,
    ModelAllowlist,
    RemoteProvider,
    build_model_allowlist,
    live_provider_from_env,
    make_server,
)

SECRET_DETAIL = "upstream said sk-or-v1-should-never-be-echoed"


class RecordingProvider:
    def __init__(self, *, fail: bool = False) -> None:
        """Record every call; with ``fail`` raise from each one."""
        self.calls: list[tuple[str, str, float, int]] = []
        self._fail = fail

    def generate(self, model: str, prompt: str, temperature: float, max_tokens: int) -> ProviderResult:
        self.calls.append((model, prompt, temperature, max_tokens))
        if self._fail:
            raise RuntimeError(SECRET_DETAIL)
        return ProviderResult(text=f"echo:{prompt}", prompt_tokens=3, completion_tokens=5)


def _serve_endpoint(provider: RecordingProvider | None, model_allowlist: ModelAllowlist) -> Iterator[str]:
    server = make_server(provider, model_allowlist, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def provider() -> RecordingProvider:
    return RecordingProvider()


@pytest.fixture
def url(provider: RecordingProvider) -> Iterator[str]:
    yield from _serve_endpoint(provider, {"cheap/model": 100})


def test_remote_provider_round_trips_through_the_endpoint(url: str, provider: RecordingProvider) -> None:
    result = RemoteProvider(url).generate("cheap/model", "hello", 0.5, 50)
    assert result == ProviderResult(text="echo:hello", prompt_tokens=3, completion_tokens=5)
    assert provider.calls == [("cheap/model", "hello", 0.5, 50)]


def test_max_tokens_is_capped_at_the_configured_value(url: str, provider: RecordingProvider) -> None:
    RemoteProvider(url).generate("cheap/model", "p", 0.0, 10_000)
    assert provider.calls[-1][3] == 100


def test_a_model_outside_the_allowlist_is_refused(url: str, provider: RecordingProvider) -> None:
    with pytest.raises(RuntimeError, match="403"):
        RemoteProvider(url).generate("expensive/model", "p", 0.0, 10)
    assert provider.calls == []


def test_without_a_key_generate_is_unavailable_but_health_is_up() -> None:
    for url in _serve_endpoint(None, {"cheap/model": 100}):
        health = httpx.get(f"{url}/health").json()
        assert health == {"ok": True, "provider_configured": False}
        with pytest.raises(RuntimeError, match="503"):
            RemoteProvider(url).generate("cheap/model", "p", 0.0, 10)


def test_provider_failures_are_reported_without_their_details() -> None:
    for url in _serve_endpoint(RecordingProvider(fail=True), {"cheap/model": 100}):
        resp = httpx.post(
            f"{url}/generate",
            json={"model": "cheap/model", "prompt": "p", "temperature": 0.0, "max_tokens": 5},
        )
        assert resp.status_code == 502
        assert resp.json() == {"error": "provider call failed"}
        assert "sk-or" not in resp.text


def test_malformed_requests_are_rejected(url: str) -> None:
    assert httpx.post(f"{url}/generate", content=b"not json").status_code == 400
    assert httpx.post(f"{url}/generate", json={"model": "cheap/model"}).status_code == 400
    assert httpx.post(f"{url}/other", json={}).status_code == 404


def test_the_allowlist_keeps_the_largest_cap_per_model() -> None:
    models = {
        "untrusted_agent": ModelSpec("a/model", 1.0, 8192),
        "teacher": ModelSpec("a/model", 0.7, 4096),
        "trusted_monitor": ModelSpec("b/model", 0.0, 4096),
    }
    assert build_model_allowlist(models) == {"a/model": 8192, "b/model": 4096}


def test_the_endpoint_url_takes_precedence_over_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROVIDER_URL_ENV, "http://gateway-core:4000")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-canary")
    assert isinstance(live_provider_from_env(), RemoteProvider)
    monkeypatch.delenv(PROVIDER_URL_ENV)
    assert isinstance(live_provider_from_env(), OpenRouterProvider)
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert live_provider_from_env() is None
