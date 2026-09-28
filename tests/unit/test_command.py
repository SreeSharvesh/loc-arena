from __future__ import annotations

import os
import sys
from pathlib import Path

from loc_arena.execution.command import CapturedOutput, run_command

MAX_CHARACTERS = 10
TIMEOUT_SECONDS = 30.0
LONG_REPORTING_PROGRAM = "print('x' * 100)\nprint('report')\n"  # the report is the last line
FITTING_OUTPUT = "x" * (MAX_CHARACTERS - 1) + "\n"  # exactly the cap, its newline included
FITTING_PROGRAM = f"print('x' * {MAX_CHARACTERS - 1})"  # prints FITTING_OUTPUT


def _python(source: str) -> tuple[str, ...]:
    return (sys.executable, "-c", source)


def test_a_long_output_keeps_its_last_characters_where_a_report_is(tmp_path: Path) -> None:
    result = run_command(
        _python(LONG_REPORTING_PROGRAM),
        cwd=tmp_path,
        environment=dict(os.environ),
        timeout_seconds=TIMEOUT_SECONDS,
        max_output_characters=MAX_CHARACTERS,
    )

    assert result.stdout == CapturedOutput(text="xx\nreport\n", truncated=True)


def test_a_long_output_keeps_its_first_characters_when_asked(tmp_path: Path) -> None:
    result = run_command(
        _python(LONG_REPORTING_PROGRAM),
        cwd=tmp_path,
        environment=dict(os.environ),
        timeout_seconds=TIMEOUT_SECONDS,
        max_output_characters=MAX_CHARACTERS,
        kept_end="head",
    )

    assert result.stdout == CapturedOutput(text="x" * MAX_CHARACTERS, truncated=True)


def test_stderr_is_kept_apart_from_stdout(tmp_path: Path) -> None:
    result = run_command(
        _python("import sys\nprint('out')\nprint('err', file=sys.stderr)\n"),
        cwd=tmp_path,
        environment=dict(os.environ),
        timeout_seconds=TIMEOUT_SECONDS,
        max_output_characters=MAX_CHARACTERS,
    )

    assert (result.stdout.text, result.stderr.text) == ("out\n", "err\n")


def test_an_output_exactly_at_the_cap_is_kept_whole_and_not_truncated(tmp_path: Path) -> None:
    result = run_command(
        _python(FITTING_PROGRAM),
        cwd=tmp_path,
        environment=dict(os.environ),
        timeout_seconds=TIMEOUT_SECONDS,
        max_output_characters=MAX_CHARACTERS,
    )

    assert result.stdout == CapturedOutput(text=FITTING_OUTPUT, truncated=False)
