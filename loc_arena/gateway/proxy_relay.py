"""A relay from the gateway to this machine's own proxy, for a machine whose only way out is that proxy.

Such a machine (a cloud development container) names its proxy in ``HTTPS_PROXY``, on its loopback address,
which no container on a bridge network reaches. The relay listens on ``gateway.outbound_proxy``, an address
of the machine the gateway does reach, and passes each tunnel to that proxy. It is a way out for whatever
reaches its address, so it passes a ``CONNECT`` to one of the run's upstreams alone and refuses the rest.

    uv run python -m loc_arena.gateway.proxy_relay <run>
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from urllib.parse import urlsplit

from loc_arena.config import load_run_config
from loc_arena.settings import GatewaySettings

PROXY_VARIABLE = "HTTPS_PROXY"  # where a machine names its proxy for HTTPS, as curl and Python read it
REFUSED = b"HTTP/1.1 403 Forbidden\r\n\r\n"


def list_upstream_hosts(gateway: GatewaySettings) -> frozenset[str]:
    """The ``host:port`` of each upstream the gateway may reach."""
    upstreams = [gateway.upstream, *([gateway.agents_upstream.url] if gateway.agents_upstream else [])]
    return frozenset(f"{upstream.host}:{upstream.port}" for upstream in upstreams)


def _split(address: str) -> tuple[str, int]:
    host, _, port = address.rpartition(":")
    return host, int(port)


async def _pass(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Every byte from ``reader`` to ``writer``, which is closed when ``reader`` ends."""
    with contextlib.suppress(ConnectionError):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    writer.close()


async def serve_relay(listen: str, proxy: str, allowed: frozenset[str]) -> asyncio.Server:
    """A server on ``listen`` that passes a ``CONNECT`` to a host in ``allowed`` on to ``proxy``."""

    async def relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            request = b""
        method, _, target = request.partition(b"\r\n")[0].rpartition(b" ")[0].partition(b" ")
        if method != b"CONNECT" or target.decode(errors="replace") not in allowed:
            writer.write(REFUSED)
            writer.close()
            return
        try:
            proxy_reader, proxy_writer = await asyncio.open_connection(*_split(proxy))
        except OSError:
            writer.close()  # the proxy is not there: the caller sees its connection end
            return
        proxy_writer.write(request)
        await asyncio.gather(_pass(reader, proxy_writer), _pass(proxy_reader, writer))

    return await asyncio.start_server(relay, *_split(listen))


async def _relay_run(run: str) -> None:
    gateway = load_run_config(f"configs/{run}.yaml").settings.gateway
    proxy = urlsplit(os.environ.get(PROXY_VARIABLE, "")).netloc
    if not gateway.outbound_proxy or not proxy:
        sys.exit(f"a relay needs gateway.outbound_proxy in {run} and {PROXY_VARIABLE} on this machine")
    allowed = list_upstream_hosts(gateway)
    async with await serve_relay(gateway.outbound_proxy, proxy, allowed) as server:
        print(f"relaying {gateway.outbound_proxy} to {proxy} for {', '.join(sorted(allowed))}", flush=True)
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_relay_run(sys.argv[1]))
