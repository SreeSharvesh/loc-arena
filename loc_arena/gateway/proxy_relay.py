"""A relay from the gateway to this machine's own proxy, for a machine whose only way out is that proxy.

Such a machine (a cloud development container) names its proxy in ``HTTPS_PROXY``, on its loopback address,
which no container on a bridge network reaches. The relay listens on ``gateway.outbound_proxy``, an address
of the machine the gateway does reach, and passes each tunnel to that proxy. It is a way out for whatever
reaches its address, so it passes a ``CONNECT`` to one of the run's upstreams alone and refuses the rest.
``loc-arena run`` and ``loc-arena read`` keep one up for as long as they run; by hand:

    uv run python -m loc_arena.gateway.proxy_relay <run>
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import sys
import threading
from collections.abc import Iterator
from urllib.parse import urlsplit

from loc_arena.config import load_run_config
from loc_arena.settings import GatewaySettings

PROXY_VARIABLE = "HTTPS_PROXY"  # where a machine names its proxy for HTTPS
REFUSED = b"HTTP/1.1 403 Forbidden\r\n\r\n"
NO_HEADER = (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError)


def find_machine_proxy() -> str | None:
    """The ``host:port`` of this machine's HTTP proxy, or ``None`` when ``HTTPS_PROXY`` names none."""
    named = urlsplit(os.environ.get(PROXY_VARIABLE, ""))
    with contextlib.suppress(ValueError):  # a port that is no number
        if named.scheme == "http" and named.hostname and named.port:
            return f"{named.hostname}:{named.port}"
    return None


def list_upstream_hosts(gateway: GatewaySettings) -> frozenset[str]:
    """The ``host:port`` of each upstream the gateway may reach."""
    upstreams = [gateway.upstream, *([gateway.agents_upstream.url] if gateway.agents_upstream else [])]
    return frozenset(f"{upstream.host}:{upstream.port}" for upstream in upstreams)


def _split(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    return host, int(port)


async def _pass(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Every byte from ``reader`` to ``writer``, which is closed when ``reader`` ends."""
    with contextlib.suppress(OSError):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    writer.close()


async def serve_relay(listen: str, proxy: str, allowed: frozenset[str], timeout: float) -> asyncio.Server:
    """A server on ``listen`` that passes a ``CONNECT`` to a host in ``allowed`` on to ``proxy``.

    The proxy is sent a request the relay writes from the target it checked, never the caller's own header.
    ``timeout`` bounds, in seconds, the wait for a caller's request, for the proxy and for its answer.
    """

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        proxy_writer = None
        try:
            try:
                request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
            except NO_HEADER:
                request = b""
            method, _, target = request.partition(b"\r\n")[0].rpartition(b" ")[0].partition(b" ")
            if method != b"CONNECT" or target.decode(errors="replace") not in allowed:
                writer.write(REFUSED)
                return
            dial = asyncio.open_connection(*_split(proxy))
            try:
                proxy_reader, proxy_writer = await asyncio.wait_for(dial, timeout)
                proxy_writer.write(b"CONNECT %b HTTP/1.1\r\nHost: %b\r\n\r\n" % (target, target))
                answer = await asyncio.wait_for(proxy_reader.readuntil(b"\r\n\r\n"), timeout)
            except NO_HEADER:
                return  # the proxy is not there or gave no answer: the caller sees its connection end
            writer.write(answer)
            if answer.partition(b" ")[2][:1] == b"2":  # the proxy opened the tunnel: only then do bytes pass
                await asyncio.gather(_pass(reader, proxy_writer), _pass(proxy_reader, writer))
        except asyncio.CancelledError:  # the relay stops: bytes a peer has not read go with the tunnel
            for held in filter(None, (writer, proxy_writer)):
                held.transport.abort()
            raise
        finally:
            writer.close()
            if proxy_writer:
                proxy_writer.close()

    return await asyncio.start_server(relay, *_split(listen))


def _is_of_this_machine(address: str) -> bool:
    """Whether this machine can listen on ``address``: a proxy on another machine needs no relay."""
    with socket.socket() as probe, contextlib.suppress(OSError):
        probe.bind((_split(address)[0], 0))
        return True
    return False


@contextlib.contextmanager
def relay_while_running(gateway: GatewaySettings) -> Iterator[str | None]:
    """A relay for as long as a command runs: the address it listens on, or ``None`` where none is needed.

    None is needed with no ``gateway.outbound_proxy``, with no proxy named by this machine, when the two are
    one address, or when the outbound proxy is on another machine: the gateway then reaches it itself. A
    relay that cannot listen stops the command. As the command ends, so does every tunnel still open.
    """
    proxy, address = find_machine_proxy(), gateway.outbound_proxy
    if not address or proxy in (None, address):
        yield None
        return
    if not _is_of_this_machine(address):
        print(f"no relay: gateway.outbound_proxy {address} is no address of this machine", file=sys.stderr)
        yield None
        return
    loop = asyncio.new_event_loop()
    relay = serve_relay(address, proxy, list_upstream_hosts(gateway), gateway.timeout_seconds)
    try:
        server = loop.run_until_complete(relay)
    except OSError as error:
        loop.close()
        raise RuntimeError(f"no relay can listen on gateway.outbound_proxy {address}: {error}") from error
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield address
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        server.close()
        tunnels = asyncio.all_tasks(loop)
        for tunnel in tunnels:
            tunnel.cancel()  # each drops both of its connections as it ends
        if tunnels:
            loop.run_until_complete(asyncio.gather(*tunnels, return_exceptions=True))
        with contextlib.suppress(TimeoutError):
            loop.run_until_complete(asyncio.wait_for(server.wait_closed(), gateway.timeout_seconds))
        loop.close()


async def _relay_run(run: str) -> None:
    gateway = load_run_config(f"configs/{run}.yaml").settings.gateway
    proxy = find_machine_proxy()
    if not gateway.outbound_proxy or not proxy:
        sys.exit(f"a relay needs gateway.outbound_proxy in {run} and {PROXY_VARIABLE}=http://host:port here")
    allowed = list_upstream_hosts(gateway)
    async with await serve_relay(gateway.outbound_proxy, proxy, allowed, gateway.timeout_seconds) as server:
        print(f"relaying {gateway.outbound_proxy} to {proxy} for {', '.join(sorted(allowed))}", flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m loc_arena.gateway.proxy_relay <run>")
    asyncio.run(_relay_run(sys.argv[1]))
