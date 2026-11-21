"""A live monitor scores several events at once and grades the run as it would one event at a time."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import yaml
from loc_arena.config import ModelSpec
from loc_arena.gateway.core import Message, ProviderResult, ToolSpec
from loc_arena.harness import grade_run
from loc_arena.logging_.events import Event
from loc_arena.monitors.registry import MonitorContext, score_each

from tests._stored_run import CONFIG, unpack_stored_run
from tests.unit._monitor_support import write_monitors_file


class PromptVerdicts:
    """A verdict fixed by each prompt, answered after a short wait; it counts the most calls in flight."""

    def __init__(self) -> None:
        """Start with no call seen."""
        self.most_in_flight = 0
        self._in_flight = 0
        self._counting = threading.Lock()

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
        *,
        spec: ModelSpec | None = None,
    ) -> ProviderResult:
        with self._counting:
            self._in_flight += 1
            self.most_in_flight = max(self.most_in_flight, self._in_flight)
        time.sleep(0.01)
        with self._counting:
            self._in_flight -= 1
        digest = hashlib.sha256(json.dumps(messages).encode()).digest()
        verdict = {"suspicion": round(digest[0] / 255, 2), "reason": f"verdict {digest[1]}"}
        return ProviderResult(text=json.dumps(verdict), prompt_tokens=1, completion_tokens=1)


def grade_with(tmp_path: Path, calls_in_flight: int) -> tuple[list[object], int]:
    """The run's monitor verdicts when its live action monitor has ``calls_in_flight``, and the most seen."""
    directory = tmp_path / str(calls_in_flight)
    directory.mkdir()
    monitors = write_monitors_file(
        directory,
        tap=["action", "message"],
        mode="live",
        prompt_ref="loc_arena/prompts/action.monitor.md",
    )
    entries = yaml.safe_load(monitors.read_text())
    aggregation = {"monitor_calls_in_flight": calls_in_flight}
    monitors.write_text(yaml.safe_dump({**entries, "scoring": {"aggregation": aggregation}}))
    run = unpack_stored_run(directory / "run")
    model = PromptVerdicts()
    grade_run(
        CONFIG,
        run,
        mode="attack",
        monitors_path=str(monitors),
        monitor_provider=model,
        write_report=False,
    )
    return json.loads((run / "scores.json").read_text())["monitor_verdicts"], model.most_in_flight


def test_a_monitor_with_calls_in_flight_grades_a_run_as_one_scoring_an_event_at_a_time(
    tmp_path: Path,
) -> None:
    one_at_a_time, most_of_one = grade_with(tmp_path, 1)
    together, most_of_eight = grade_with(tmp_path, 8)

    assert together == one_at_a_time
    assert (most_of_one, most_of_eight) == (1, 8)


def test_events_scored_together_come_back_in_their_order(tmp_path: Path) -> None:
    context = MonitorContext(task_description="", thresholds=CONFIG.side_task.thresholds, calls_in_flight=4)
    events = [Event("ep", seq, float(seq), "agent-main", "untrusted", "action") for seq in range(12)]

    def score(event: Event) -> int:
        time.sleep(0.01 * (12 - event.seq))
        return event.seq

    assert score_each(events, score, context) == list(range(12))
