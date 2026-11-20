"""Start one full stack of the stack-test run, time each container to healthy, and sample idle CPU.

Usage: stack_probe.py <settled-interval-seconds> [idle-seconds]
The rendered compose file's steady health interval is set to the first argument; the start interval stays.
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml
from loc_arena.config import load_run_config
from loc_arena.episode_stack import REPOSITORY, issue_secrets, render_compose
from loc_arena.gateway.core import API_KEY_VARIABLE

RUN = "aurora-efficiency.stack-test"
CONFIG = load_run_config(REPOSITORY / "configs" / f"{RUN}.yaml")
interval = sys.argv[1]
idle_seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 40

directory = Path(tempfile.mkdtemp(prefix="stack-probe-"))
document = render_compose(CONFIG, REPOSITORY, RUN, [])
for service in document["services"].values():
    if "healthcheck" in service:
        service["healthcheck"]["interval"] = f"{interval}s"
(directory / "compose.yaml").write_text(yaml.safe_dump(document))
attack, _ = issue_secrets(CONFIG)
project = f"locarena-probe{secrets.token_hex(3)}"
compose = ["docker", "compose", "-p", project, "-f", str(directory / "compose.yaml")]
compose += ["--project-directory", str(directory)]
environment = {**os.environ, API_KEY_VARIABLE: secrets.token_urlsafe(24), **attack}
services = [name for name in document["services"] if name != "episode"]


def read_cpu() -> tuple[int, int]:
    """Busy and total jiffies of the Docker host, read from a container's /proc/stat."""
    line = subprocess.run(
        ["docker", "run", "--rm", "busybox", "head", "-1", "/proc/stat"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[1:]
    values = list(map(int, line))
    return sum(values) - values[3] - values[4], sum(values)


healthy_at: dict[str, float] = {}
stop = threading.Event()
started = time.monotonic()


def watch() -> None:
    while not stop.is_set():
        listed = subprocess.run([*compose, "ps", "--format", "json"], env=environment, capture_output=True, text=True)
        for line in listed.stdout.splitlines():
            container = json.loads(line)
            if container.get("Health") in ("healthy", "") and container.get("State") == "running":
                healthy_at.setdefault(container["Service"], time.monotonic() - started)
        time.sleep(1)


threading.Thread(target=watch, daemon=True).start()
try:
    up = subprocess.run(
        [*compose, "up", "--detach", "--wait", "--no-build", *services],
        env=environment,
        capture_output=True,
        text=True,
    )
    up_seconds = time.monotonic() - started
    stop.set()
    print(f"interval={interval}s  up --wait: exit {up.returncode} in {up_seconds:.1f}s  ({len(services)} services)")
    if up.returncode:
        print(up.stderr[-1500:])
    for name, at in sorted(healthy_at.items(), key=lambda item: item[1]):
        print(f"   {at:6.1f}s  {name}")
    time.sleep(15)  # let start-up work settle
    busy0, total0 = read_cpu()
    time.sleep(idle_seconds)
    busy1, total1 = read_cpu()
    cores = os.cpu_count()
    print(f"host cores: {cores}")
    share = (busy1 - busy0) / (total1 - total0)
    print(f"idle for {idle_seconds}s: host busy {100 * share:.1f}% of all its cores")
    stats = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}"],
        capture_output=True,
        text=True,
    ).stdout
    ours = [line.split("\t") for line in stats.splitlines() if line.startswith(project)]
    total = sum(float(percent.rstrip("%")) for _, percent in ours)
    print(f"docker stats, one sample: {len(ours)} containers, {total:.0f}% of one core in their cgroups")
    for name, percent in sorted(ours, key=lambda row: -float(row[1].rstrip("%")))[:14]:
        print(f"      {percent:>8}  {name.removeprefix(project + '-')}")
finally:
    stop.set()
    subprocess.run([*compose, "down", "--volumes", "--remove-orphans"], env=environment, capture_output=True)
