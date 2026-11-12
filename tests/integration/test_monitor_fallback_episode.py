"""A live monitor that stops calling its model in the honest twin alone is reported errored for the run."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from loc_arena.harness import grade_run

from tests._stored_run import CONFIG, unpack_stored_run
from tests.integration._gateway_support import FailingProvider
from tests.unit._monitor_support import write_monitors_file

MONITOR = "action_monitor"
# With a fallback after 1 failure: the honest twin, scored first, fails its first call and makes no more;
# every later call, the episode's, answers.
ONLY_THE_TWIN_FAILS = range(2, 1_000_000)


def test_a_monitor_that_stopped_calling_its_model_in_the_honest_twin_alone_is_errored(tmp_path: Path) -> None:
    monitors = write_monitors_file(
        tmp_path,
        tap=["action", "message"],
        mode="live",
        prompt_ref="configs/prompts/monitors/action_monitor.txt",
    )
    entries = yaml.safe_load(monitors.read_text())
    monitors.write_text(
        yaml.safe_dump({**entries, "scoring": {"aggregation": {"monitor_failures_before_fallback": 1}}}),
    )
    run = unpack_stored_run(tmp_path / "run")

    grade_run(
        CONFIG,
        run,
        mode="attack",
        monitors_path=str(monitors),
        monitor_provider=FailingProvider(answered=ONLY_THE_TWIN_FAILS),
        write_report=False,
    )

    rows = json.loads((run / "scores.json").read_text())["monitors"]
    assert [row["errored"] for row in rows if row["name"] == MONITOR] == [True]
