from __future__ import annotations

import os
import sys
from pathlib import Path

from loc_arena.execution.command import CapturedOutput, CommandResult, OutputEnd, run_command

MAX_CHARACTERS = 10
LONG_REPORTING_PROGRAM = "print('x' * 100)\nprint('report')\n"  # the report is the last line
FITTING_OUTPUT = "x" * (MAX_CHARACTERS - 1) + "\n"  # exactly the cap, its newline included
FITTING_PROGRAM = f"print('x' * {MAX_CHARACTERS - 1})"  # prints FITTING_OUTPUT


def _run_python(cwd: Path, source: str, kept_end: OutputEnd = "tail") -> CommandResult:
    return run_command(
        (sys.executable, "-c", source),
        cwd=cwd,
        environment=dict(os.environ),
        timeout_seconds=30.0,
        max_output_characters=MAX_CHARACTERS,
        kept_end=kept_end,
    )


def test_a_long_output_keeps_its_last_characters_where_a_report_is(tmp_path: Path) -> None:
    result = _run_python(tmp_path, LONG_REPORTING_PROGRAM)

    assert result.stdout == CapturedOutput(text="xx\nreport\n", truncated=True)


def test_a_long_output_keeps_its_first_characters_when_asked(tmp_path: Path) -> None:
    result = _run_python(tmp_path, LONG_REPORTING_PROGRAM, kept_end="head")

    assert result.stdout == CapturedOutput(text="x" * MAX_CHARACTERS, truncated=True)


def test_stderr_is_kept_apart_from_stdout(tmp_path: Path) -> None:
    result = _run_python(tmp_path, "import sys\nprint('out')\nprint('err', file=sys.stderr)\n")

    assert (result.stdout.text, result.stderr.text) == ("out\n", "err\n")


def test_an_output_exactly_at_the_cap_is_kept_whole_and_not_truncated(tmp_path: Path) -> None:
    result = _run_python(tmp_path, FITTING_PROGRAM)

    assert result.stdout == CapturedOutput(text=FITTING_OUTPUT, truncated=False)
