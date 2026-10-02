"""Runs a command whose code an agent may have written, bounded in time and in output."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final, Literal

UTF8_MAX_BYTES_PER_CHARACTER: Final = 4
OutputEnd = Literal["head", "tail"]


@dataclass(frozen=True)
class CapturedOutput:
    """One output stream of a command: its first or last characters, and whether it printed more."""

    text: str
    truncated: bool


@dataclass(frozen=True)
class CommandResult:
    """A finished command: its exit code (``None`` when killed at its timeout) and its capped output."""

    exit_code: int | None
    stdout: CapturedOutput
    stderr: CapturedOutput  # empty when stderr was merged into stdout

    @property
    def timed_out(self) -> bool:
        """Whether the command was killed at its timeout."""
        return self.exit_code is None


def run_command(
    arguments: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    max_output_characters: int,
    kept_end: OutputEnd = "tail",
    merge_stderr: bool = False,
) -> CommandResult:
    """Run ``arguments`` in ``cwd``, kill its process group at ``timeout_seconds``, cap each output stream."""
    # Output goes to files and the command leads its own session: see docs/isolation/design.md#command.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT if merge_stderr else stderr,
            start_new_session=True,
        )
        try:
            exit_code: int | None = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            exit_code = None
        return CommandResult(
            exit_code=exit_code,
            stdout=_read_capped(stdout, max_output_characters, kept_end),
            stderr=_read_capped(stderr, max_output_characters, kept_end),
        )


def _read_capped(file: IO[bytes], max_characters: int, kept_end: OutputEnd) -> CapturedOutput:
    size = os.fstat(file.fileno()).st_size
    read_bytes = min(size, max_characters * UTF8_MAX_BYTES_PER_CHARACTER)
    offset = 0 if kept_end == "head" else size - read_bytes
    text = os.pread(file.fileno(), read_bytes, offset).decode(errors="replace")
    kept = text[:max_characters] if kept_end == "head" else text[-max_characters:]
    return CapturedOutput(text=kept, truncated=len(text) > max_characters or size > read_bytes)
