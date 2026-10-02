"""Processes agent code detaches on purpose, each recording its pid in a file the test kills them from."""

from __future__ import annotations

import os
import signal
from contextlib import suppress
from pathlib import Path


def write_detaching_test(repository: Path, pid_file: Path) -> None:
    (repository / "tests" / "test_detach.py").write_text(
        "import subprocess\n\n\n"
        "def test_detach(capfd):\n"
        "    with capfd.disabled():  # the process inherits the suite's stdout and stderr, not pytest's\n"
        "        process = subprocess.Popen(['sleep', '600'])\n"
        f"    with open({str(pid_file)!r}, 'a') as pids:\n"
        "        pids.write(f'{process.pid}\\n')\n",
    )


def build_detaching_source(pid_file: Path) -> str:
    return (
        "import subprocess\n"
        "_process = subprocess.Popen(['sleep', '600'])\n"
        f"with open({str(pid_file)!r}, 'a') as _pids:\n"
        "    _pids.write(f'{_process.pid}\\n')\n"
    )


def kill_recorded_processes(pid_file: Path) -> None:
    for pid in pid_file.read_text().split() if pid_file.exists() else ():
        with suppress(ProcessLookupError):
            os.kill(int(pid), signal.SIGKILL)
