"""The command server's end of leftover processes in a real sandbox container, under the host's rules."""

from __future__ import annotations

import secrets
import subprocess
import time

from loc_arena.settings import StackSettings

from tests.integration._docker_support import needs_stack

IMAGE = StackSettings().sandbox_image
SECONDS = 5
# What the command server does after each command and before each reset, over and over: each time a process
# is left behind, and must be dead once the leftovers are ended.
END_LEFTOVERS = """
import subprocess
import sys
import time

from sandbox_server.server import end_leftover_processes

until = time.monotonic() + float(sys.argv[1])
while time.monotonic() < until:
    leftover = subprocess.Popen(["sleep", "60"])
    end_leftover_processes()
    leftover.wait(timeout=10)
"""

pytestmark = needs_stack(IMAGE)


def test_the_leftovers_are_ended_while_commands_enter_the_container() -> None:
    # A health probe enters a sandbox every few seconds. Under AppArmor the sandbox may not signal a process
    # that is still entering it, so the kill of every process is refused for that one.
    name = f"locarena-test-leftovers-{secrets.token_hex(3)}"
    started = ["docker", "run", "--detach", "--init", "--name", name, IMAGE]
    subprocess.run([*started, "python", "-c", END_LEFTOVERS, str(SECONDS)], check=True, capture_output=True)
    try:
        until = time.monotonic() + SECONDS
        while time.monotonic() < until:
            subprocess.run(["docker", "exec", name, "true"], check=False, capture_output=True)
        ended = subprocess.run(["docker", "wait", name], check=True, capture_output=True, text=True)
        printed = subprocess.run(
            ["docker", "logs", "--tail", "5", name],
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=False, capture_output=True)

    assert ended.stdout.strip() == "0", printed.stderr
