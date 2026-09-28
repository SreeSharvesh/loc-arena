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

from loc_arena.compose_stack import EpisodeStack


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


# The source of ``is_mount_point(path)``, for the probes that run inside a container. It reads the kernel's
# mount table (proc_pid_mountinfo(5): one line per mount, field 5 its mount point), because whether the path
# exists says nothing: both images create every volume's mount point as an empty directory, mounted or not.
IS_MOUNT_POINT_SOURCE = (
    "def is_mount_point(path):\n"
    "    with open('/proc/self/mountinfo') as mount_table:\n"
    "        return any(line.split()[4] == path for line in mount_table)\n"
)


def build_mount_point_probe(path: str) -> str:
    """Python code printing ``{"mounted": <bool>}``: whether a filesystem is mounted at ``path``."""
    return f"import json\n{IS_MOUNT_POINT_SOURCE}print(json.dumps({{'mounted': is_mount_point({path!r})}}))\n"


def has_mount_at(stack: EpisodeStack, service: str, path: str) -> bool:
    """True iff a filesystem is mounted at ``path`` inside ``service``; raises if the probe does not run."""
    result = stack.exec(service, ["python", "-c", build_mount_point_probe(path)])
    return json.loads(result.stdout)["mounted"]


def can_egress(stack: EpisodeStack, service: str, host: str = "1.1.1.1", port: int = 443) -> bool:
    """True iff ``service`` can open a TCP connection to an external ``host:port`` (the egress direction)."""
    code = (
        "import socket\n"
        "s=socket.socket();s.settimeout(4)\n"
        f"try:\n s.connect(({host!r},{port}));print('REACHABLE')\n"
        "except Exception:\n print('UNREACHABLE')\n"
    )
    return "REACHABLE" in stack.exec(service, ["python", "-c", code], check=False).stdout
