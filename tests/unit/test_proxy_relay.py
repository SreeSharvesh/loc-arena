"""The relay from the gateway to a machine's own proxy: a tunnel to a run's upstream passes, nothing else."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from loc_arena.gateway.proxy_relay import find_machine_proxy, list_upstream_hosts, serve_relay
from loc_arena.settings import AgentsUpstream, GatewaySettings
from pydantic import HttpUrl

ALLOWED = frozenset({"provider.example:443"})
TUNNEL = b"CONNECT provider.example:443 HTTP/1.1\r\nHost: provider.example:443\r\n\r\n"
OPENED = b"HTTP/1.1 200 Connection established\r\n\r\n"


def ask_through_relay(request: bytes, answer: bytes = OPENED) -> tuple[bytes, list[bytes]]:
    """What a caller that sends ``request`` to a relay reads back, and what the machine's proxy was sent.

    The stand-in proxy answers a request's header with ``answer`` and echoes what it is sent after it. A
    relay that keeps a connection open fails the test in ten seconds.
    """
    return asyncio.run(asyncio.wait_for(_ask_through_relay(request, answer), timeout=10))


async def _ask_through_relay(request: bytes, answer: bytes) -> tuple[bytes, list[bytes]]:
    received: list[bytes] = []

    async def stand_in_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(TimeoutError, asyncio.IncompleteReadError):  # a caller may send nothing
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(answer)
            if more := await asyncio.wait_for(reader.read(100), timeout=1):
                received.append(more)
                writer.write(b"echo:" + more)
        writer.close()

    async with await asyncio.start_server(stand_in_proxy, "127.0.0.1", 0) as proxy:
        proxy_address = f"127.0.0.1:{proxy.sockets[0].getsockname()[1]}"
        async with await serve_relay("127.0.0.1:0", proxy_address, ALLOWED, timeout=0.5) as relay:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.sockets[0].getsockname()[1])
            writer.write(request)
            try:
                reply = await asyncio.wait_for(reader.read(), timeout=3)
            finally:
                writer.close()
    return reply, received


def test_a_tunnel_to_an_upstream_of_the_run_passes_both_ways_through_the_machines_proxy() -> None:
    reply, received = ask_through_relay(TUNNEL + b"hello")

    assert reply == OPENED + b"echo:hello"
    assert received == [TUNNEL, b"hello"]


def test_a_tunnel_the_machines_proxy_refuses_passes_its_answer_and_nothing_after_it() -> None:
    refused = b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n"
    second = b"CONNECT elsewhere.example:443 HTTP/1.1\r\n\r\n"

    reply, received = ask_through_relay(TUNNEL + second, answer=refused)

    assert reply == refused
    assert received == [TUNNEL]


def test_the_machines_proxy_is_sent_the_target_the_relay_checked_and_none_of_the_callers_header() -> None:
    request = b"CONNECT provider.example:443 X\rCONNECT\tx:1\r\nHost: elsewhere.example:443\r\n\r\n"

    _, received = ask_through_relay(request)

    assert received == [TUNNEL]


@pytest.mark.parametrize(
    "sent",
    [
        b"CONNECT elsewhere.example:443 HTTP/1.1\r\n\r\n",
        b"GET provider.example:443 HTTP/1.1\r\n\r\n",
        b"",  # a caller that sends no request is answered once the wait for one ends
    ],
)
def test_any_other_request_is_refused_and_never_reaches_the_machines_proxy(sent: bytes) -> None:
    reply, received = ask_through_relay(sent)

    assert reply == b"HTTP/1.1 403 Forbidden\r\n\r\n"
    assert received == []


def test_the_relay_allows_the_upstream_and_the_agents_upstream_of_the_run() -> None:
    second = AgentsUpstream(
        url=HttpUrl("https://api.second.example"),
        chat_path="v1/chat/completions",
        key_variable="SECOND_API_KEY",
    )

    allowed = list_upstream_hosts(GatewaySettings(agents_upstream=second))

    assert allowed == {"openrouter.ai:443", "api.second.example:443"}


@pytest.mark.parametrize(
    ("named", "proxy"),
    [
        ("http://127.0.0.1:45769", "127.0.0.1:45769"),
        ("http://user:secret@proxy.example:3128/", "proxy.example:3128"),
        ("127.0.0.1:45769", None),  # no scheme
        ("socks5://127.0.0.1:1080", None),
        ("http://user:p/w@proxy.example:3128", None),  # its port is no number
        ("", None),
    ],
)
def test_the_machines_proxy_is_the_host_and_port_of_an_http_url_in_its_variable(
    named: str,
    proxy: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", named)

    found = find_machine_proxy()

    assert found == proxy
