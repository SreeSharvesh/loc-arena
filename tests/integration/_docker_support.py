"""Helpers for the docker isolation tests.

The sealed/egress boundary (the sealed-vs-tamperable isolation) is enforced STRUCTURALLY, at the network
and volume
layer. The deterministic way to assert it is network MEMBERSHIP and volume MOUNTS (from ``docker
inspect``): a container can reach only what shares one of its networks, and can read only what is mounted
into it. Runtime reachability probes additionally test the docker DAEMON's enforcement of that membership;
they are reliable on a Linux host but non-deterministic on Docker Desktop for Mac (its VM networking
intermittently bridges user networks), so the gate asserts the structural facts and the mount-based
runtime probes (which are reliable).
"""

from __future__ import annotations

import json
import subprocess

from loc_arena.harness import EpisodeStack


def service_networks(stack: EpisodeStack, service: str) -> set[str]:
    """The base network names a service is attached to (project prefix stripped), from ``docker inspect``."""
    cid = stack.container_id(service)
    if not cid:
        return set()
    result = subprocess.run(
        ["docker", "inspect", cid, "--format", "{{json .NetworkSettings.Networks}}"],
        capture_output=True,
        text=True,
    )
    nets = json.loads(result.stdout) if result.stdout.strip() else {}
    prefix = f"{stack.project}_"
    return {n[len(prefix) :] if n.startswith(prefix) else n for n in nets}


def share_a_network(stack: EpisodeStack, a: str, b: str) -> bool:
    """True iff services ``a`` and ``b`` are attached to at least one common network."""
    return bool(service_networks(stack, a) & service_networks(stack, b))


def path_exists(stack: EpisodeStack, service: str, path: str) -> bool:
    """True iff ``path`` exists inside ``service`` (mount-based; deterministic)."""
    result = stack.exec(
        service, ["python", "-c", f"import pathlib;print(pathlib.Path({path!r}).exists())"], check=False
    )
    return result.stdout.strip() == "True"


def can_egress(stack: EpisodeStack, service: str, host: str = "1.1.1.1", port: int = 443) -> bool:
    """True iff ``service`` can open a TCP connection to an external ``host:port`` (the egress direction)."""
    code = (
        "import socket\n"
        "s=socket.socket();s.settimeout(4)\n"
        f"try:\n s.connect(({host!r},{port}));print('REACHABLE')\n"
        "except Exception:\n print('UNREACHABLE')\n"
    )
    return "REACHABLE" in stack.exec(service, ["python", "-c", code], check=False).stdout
