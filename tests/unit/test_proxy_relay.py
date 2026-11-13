"""The relay from the gateway to a machine's own proxy: a tunnel to a run's upstream passes, nothing else."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time

import pytest
from loc_arena.gateway.proxy_relay import (
    find_machine_proxy,
    list_upstream_hosts,
    relay_while_running,
    serve_relay,
)
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


@pytest.fixture
def address() -> str:
    """An address of this machine that nothing listens on."""
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{free.getsockname()[1]}"


def ask(address: str, request: bytes) -> bytes | None:
    """What whatever listens on ``address`` answers ``request``; ``None`` when nothing listens there."""
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=5) as caller:
            caller.sendall(request)
            return caller.recv(100)
    except ConnectionRefusedError:
        return None


def proxied(address: str) -> GatewaySettings:
    """A gateway whose outbound proxy is ``address``, with a short wait so a failing test fails soon."""
    return GatewaySettings(outbound_proxy=address, timeout_seconds=1)


def test_a_command_on_a_machine_with_its_own_proxy_has_a_relay_while_it_runs(
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")

    with relay_while_running(proxied(address)):
        answer = ask(address, b"GET / HTTP/1.1\r\n\r\n")

    assert answer == b"HTTP/1.1 403 Forbidden\r\n\r\n"


def test_the_relay_of_a_command_is_gone_when_the_command_ends(
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    with relay_while_running(proxied(address)):
        pass

    answer = ask(address, b"GET / HTTP/1.1\r\n\r\n")

    assert answer is None


def test_a_command_ends_at_once_though_the_relay_still_holds_a_tunnel(
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, _, port = address.rpartition(":")
    with socket.create_server(("127.0.0.1", 0)) as silent_proxy:  # it takes a tunnel and never answers
        monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{silent_proxy.getsockname()[1]}")
        started = time.monotonic()

        with relay_while_running(GatewaySettings(outbound_proxy=address)):
            with socket.create_connection((host, int(port))) as caller:
                caller.sendall(b"CONNECT openrouter.ai:443 HTTP/1.1\r\n\r\n")
                held = silent_proxy.accept()[0]  # the relay has dialed: it now waits for an answer
        held.close()

    assert time.monotonic() - started < 5  # far under the 120 seconds the gateway waits


@pytest.mark.parametrize(
    ("named", "outbound"),
    [
        ("", "{address}"),  # the machine names no proxy
        ("http://{address}", "{address}"),  # the machine's proxy is the outbound proxy itself
        ("http://127.0.0.1:9", "192.0.2.1:3128"),  # the outbound proxy is on another machine
    ],
)
def test_no_relay_runs_where_the_gateway_reaches_its_outbound_proxy_itself(
    named: str,
    outbound: str,
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", named.format(address=address))

    with relay_while_running(proxied(outbound.format(address=address))) as relayed:
        pass

    assert relayed is None


def test_a_relay_that_cannot_listen_stops_the_command_naming_the_setting(
    address: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    host, _, port = address.rpartition(":")

    with socket.create_server((host, int(port))), pytest.raises(RuntimeError, match="gateway.outbound_proxy"):  # noqa: PT012
        with relay_while_running(proxied(address)):
            pass
