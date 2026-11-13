"""The relay from the gateway to a machine's own proxy: a tunnel to a run's upstream passes, nothing else."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from loc_arena.gateway.proxy_relay import list_upstream_hosts, serve_relay
from loc_arena.settings import AgentsUpstream, GatewaySettings
from pydantic import HttpUrl

ALLOWED = frozenset({"provider.example:443"})


async def ask_through_relay(request: bytes) -> tuple[bytes, list[bytes]]:
    """What a caller that sends ``request`` to a relay reads back, and what the machine's proxy was sent."""
    received: list[bytes] = []

    async def stand_in_proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(TimeoutError, asyncio.IncompleteReadError):  # a caller may send nothing
            received.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            writer.write(b"echo:" + await asyncio.wait_for(reader.read(5), timeout=1))
        writer.close()

    async with await asyncio.start_server(stand_in_proxy, "127.0.0.1", 0) as proxy:
        proxy_address = f"127.0.0.1:{proxy.sockets[0].getsockname()[1]}"
        async with await serve_relay("127.0.0.1:0", proxy_address, ALLOWED) as relay:
            reader, writer = await asyncio.open_connection("127.0.0.1", relay.sockets[0].getsockname()[1])
            writer.write(request)
            try:
                reply = await asyncio.wait_for(reader.read(), timeout=3)
            finally:
                writer.close()
    return reply, received


def test_a_tunnel_to_an_upstream_of_the_run_passes_both_ways_through_the_machines_proxy() -> None:
    request = b"CONNECT provider.example:443 HTTP/1.1\r\nHost: provider.example:443\r\n\r\n"

    reply, received = asyncio.run(ask_through_relay(request + b"hello"))

    assert reply == b"HTTP/1.1 200 Connection established\r\n\r\necho:hello"
    assert received == [request]


@pytest.mark.parametrize(
    "request_line",
    [b"CONNECT elsewhere.example:443 HTTP/1.1", b"GET provider.example:443 HTTP/1.1"],
)
def test_any_other_request_is_refused_and_never_reaches_the_machines_proxy(request_line: bytes) -> None:
    reply, received = asyncio.run(ask_through_relay(request_line + b"\r\n\r\n"))

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
