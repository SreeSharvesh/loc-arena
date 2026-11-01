"""pi-on-gateway writes pi's home before it becomes pi: here a stand-in pi prints what it finds there."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "sandbox_server" / "pi_on_gateway.py"


def test_a_runners_pi_compacts_nothing_so_each_model_call_ends_in_its_agent_loop(tmp_path: Path) -> None:
    bin_directory, home = tmp_path / "bin", tmp_path / "home"
    bin_directory.mkdir()
    home.mkdir()
    fake = bin_directory / "pi"
    fake.write_text('#!/bin/sh\ncat "$HOME/.pi/agent/settings.json"\n')
    fake.chmod(0o755)
    environment = {
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "LOC_ARENA_GATEWAY_API": "http://gateway:8080/runner/api/v1",
    }

    ran = subprocess.run(
        [sys.executable, str(SCRIPT), "aurora-7b-v3", "Summarize.", "the run"],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(ran.stdout) == {"compaction": {"enabled": False}}
