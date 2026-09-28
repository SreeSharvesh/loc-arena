"""Processes that agent code detaches on purpose, for the tests that check they never hold a call.

Each detached process appends its pid to a file, one per line, and the test kills them all afterwards.
"""

from __future__ import annotations

import os
import signal
from contextlib import suppress
from pathlib import Path


def write_detaching_test(repository: Path, pid_file: Path) -> None:
    """Add a test to ``repository`` that detaches ``sleep 600`` holding the suite's own stdout and stderr."""
    (repository / "tests" / "test_detach.py").write_text(
        "import subprocess\n\n\n"
        "def test_detach(capfd):\n"
        "    with capfd.disabled():  # the process inherits the suite's stdout and stderr, not pytest's\n"
        "        process = subprocess.Popen(['sleep', '600'])\n"
        f"    with open({str(pid_file)!r}, 'a') as pids:\n"
        "        pids.write(f'{process.pid}\\n')\n",
    )


def build_detaching_source(pid_file: Path) -> str:
    """Python source that detaches ``sleep 600`` holding this process's stdout and stderr (no capture)."""
    return (
        "import subprocess\n"
        "_process = subprocess.Popen(['sleep', '600'])\n"
        f"with open({str(pid_file)!r}, 'a') as _pids:\n"
        "    _pids.write(f'{_process.pid}\\n')\n"
    )


def kill_recorded_processes(pid_file: Path) -> None:
    """Kill every process whose pid ``pid_file`` records (none if it does not exist)."""
    for pid in pid_file.read_text().split() if pid_file.exists() else ():
        with suppress(ProcessLookupError):
            os.kill(int(pid), signal.SIGKILL)
