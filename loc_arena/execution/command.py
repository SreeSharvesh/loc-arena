"""One way to run a command whose code an agent may have written: bounded in time and in output.

``bash``, a repository's suite and the benchmarks, in the sandboxes, on the host and in the grader, all run
through :func:`run_command`:

- Output goes to unlinked temporary files, never pipes. Reading a pipe to its end waits for every process
  holding its write end, and a process the command detaches (``sleep 600 &``, ``nohup``, ``setsid``) holds
  it until it exits. With a file the call returns when the command itself exits, and a detached process
  keeps running, writing to a file no one reads again.
- The command runs in a new session (``start_new_session``: the child calls ``setsid()``), so it leads its
  own process group. ``Popen.wait`` only raises ``TimeoutExpired`` and kills nothing, so on timeout the
  whole group is killed, the command's background children with it.
- Only a capped number of characters is read back, with ``pread``, which leaves alone the file offset a
  detached process still writing shares.
"""

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

UTF8_MAX_BYTES_PER_CHARACTER: Final = 4  # RFC 3629: one UTF-8 character is at most four bytes
OutputEnd = Literal["head", "tail"]  # which end of a long output is kept


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
    """Run ``arguments`` as this process's user in ``cwd``; kill its process group at ``timeout_seconds``.

    Each output stream keeps its ``kept_end`` (the last line, where a report or summary is, by default) of
    at most ``max_output_characters``. With ``merge_stderr``, stderr is interleaved into stdout in order.
    """
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
    """At most ``max_characters`` from ``kept_end`` of ``file``, read without moving its offset."""
    size = os.fstat(file.fileno()).st_size
    read_bytes = min(size, max_characters * UTF8_MAX_BYTES_PER_CHARACTER)
    offset = 0 if kept_end == "head" else size - read_bytes
    text = os.pread(file.fileno(), read_bytes, offset).decode(errors="replace")
    kept = text[:max_characters] if kept_end == "head" else text[-max_characters:]
    return CapturedOutput(text=kept, truncated=len(text) > max_characters or size > read_bytes)
