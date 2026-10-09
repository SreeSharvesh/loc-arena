"""Running one command: in its own session, its output kept as tails, the provider key never passed on."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import tempfile
from collections.abc import Mapping
from typing import IO

from sandbox_server.wire import CommandRequest, CommandResult

# The variable the gateway's key travels in. The harness takes its name from here, so the two cannot differ.
API_KEY_VARIABLE = "OPENROUTER_API_KEY"


def run_command(
    request: CommandRequest,
    output_limit_bytes: int,
    base_environment: Mapping[str, str],
) -> CommandResult:
    """Run ``request`` in ``base_environment`` plus its own, without the provider key.

    The key is dropped from both environments, whoever sent it. A timeout kills the command's whole session,
    children included. Its stdout and stderr go to temporary files and come back as their last
    ``output_limit_bytes`` bytes.
    """
    environment = {**base_environment, **request.environment}
    environment.pop(API_KEY_VARIABLE, None)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        with subprocess.Popen(
            request.argv,
            cwd=request.directory,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        ) as process:
            try:
                returncode: int | None = process.wait(timeout=request.timeout_seconds)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):  # the whole session ended meanwhile
                    os.killpg(process.pid, signal.SIGKILL)
                returncode = None
        return CommandResult(
            returncode=returncode,
            stdout=read_tail(stdout, output_limit_bytes),
            stderr=read_tail(stderr, output_limit_bytes),
        )


def read_tail(stream: IO[bytes], limit: int) -> str:
    """The last ``limit`` bytes written to ``stream``, decoded."""
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - limit))
    return stream.read().decode(errors="replace")
