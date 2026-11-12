"""Where a unit test's agent commands run: this process, in place of a sandbox."""

from __future__ import annotations

import os

from sandbox_server.command import run_command
from sandbox_server.wire import CommandRequest, CommandResult


class LocalRunner:
    """Runs a command here, its whole output kept."""

    def run(self, request: CommandRequest) -> CommandResult:
        """Run ``request`` in this process's environment."""
        return run_command(request, None, os.environ)
