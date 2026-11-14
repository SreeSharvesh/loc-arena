"""Temporary, for issue 306: the runner's size, and Phoenix's time to healthy with the stack's limits.

It reports through a warning, so CI's log shows the numbers whatever the result. Removed before merging.
"""

from __future__ import annotations

import os
import subprocess
import time
import warnings
from pathlib import Path

import pytest

IMAGE = (
    "arizephoenix/phoenix:version-20.23.0"
    "@sha256:c542e7de1dc1fdd0f938de337d44ebefa7266e91391eed6e7978523d7f6e1f2f"
)
PROBE = "import urllib.request; urllib.request.urlopen('http://localhost:6006/healthz', timeout=2)"
LIMITS = ["--memory", "512m", "--cpus", "1", "--cap-drop", "ALL", "--init", "--pids-limit", "256"]
GIVE_UP_SECONDS = 300
pytestmark = pytest.mark.stack


def test_phoenix_alone() -> None:
    subprocess.run(["docker", "pull", "--quiet", IMAGE], check=True, capture_output=True)
    times = []
    for trial in range(3):
        name = f"phoenix-measure-{os.getpid()}-{trial}"
        run = ["docker", "run", "-d", "--rm", "--name", name, *LIMITS, IMAGE]
        subprocess.run(run, check=True, capture_output=True)
        started = time.monotonic()
        probe = ["docker", "exec", name, "python3.13", "-c", PROBE]
        while subprocess.run(probe, capture_output=True, check=False).returncode:
            if time.monotonic() - started > GIVE_UP_SECONDS:
                break
            time.sleep(0.5)
        times.append(round(time.monotonic() - started, 1))
        subprocess.run(["docker", "stop", "-t", "1", name], capture_output=True, check=False)
    load = Path("/proc/loadavg").read_text().split()[0]
    warnings.warn(f"MEASURE cores={os.cpu_count()} load={load} phoenix_healthy_seconds={times}", stacklevel=1)
