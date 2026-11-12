"""What the event builder's tests share: a played attack run with no record yet, and services' logs in it."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.gateway.model_gateway import CALL_RECORD_FILE, GatewayCall
from loc_arena.harness import PLAYED_FILE, PlayedRun
from loc_arena.logging_.events import AppendOnlyLog, Event, read_events
from loc_arena.merged_main import EPISODE_PHASE, TWIN_PHASE
from loc_arena.recorded_events import UNATTRIBUTED_FILE
from loc_arena.task import SNAPSHOT_FILE, ClockReading, SnapshotFile, read_snapshot_file

LIVE = load_run_config("configs/aurora-efficiency.yaml")
SEALED, MIRROR = "events.sealed.jsonl", "events.mirror.jsonl"
PHASE_SECONDS = 60.0  # how long each play of a played run lasted


def record_phase_around(phase: Path, start: float, end: float, mode: str = "attack") -> Path:
    """A played episode's directory whose play window runs from ``start`` to ``end``, with empty logs."""
    phase.mkdir(parents=True)
    for log in (SEALED, MIRROR):
        AppendOnlyLog(phase / log, "episode-1").append(
            Event("episode-1", 0, 100.0, "system", "sealed", "tick"),
        )
    snapshot = SnapshotFile(
        sealed_path=Path(SEALED),
        mirror_path=Path(MIRROR),
        checkout=Path("checkout"),
        deadline_ts=10_000.0,
        mode=mode,  # ty: ignore[invalid-argument-type]
        clock=(ClockReading(wall=start, ts=100.0),),
        finished_at=end,
    )
    (phase / SNAPSHOT_FILE).write_text(snapshot.model_dump_json())
    return phase


def play(run_directory: Path) -> Path:
    """A played attack run in ``run_directory``: its episode, then its honest twin, with no record yet.

    Each phase holds its play window, which ended before now, and logs with one event of play.
    """
    twin_end = time.time()
    episode_end = twin_end - PHASE_SECONDS
    record_phase_around(run_directory / EPISODE_PHASE, episode_end - PHASE_SECONDS, episode_end)
    record_phase_around(run_directory / TWIN_PHASE, episode_end, twin_end, "honest")
    played = PlayedRun(play_seconds=2 * PHASE_SECONDS, episode_trace=None, calibration_trace=None)
    (run_directory / PLAYED_FILE).write_text(played.model_dump_json())
    return run_directory


def write_log(run_directory: Path, service: str, *lines: str) -> None:
    """Write ``lines`` as the run's log for ``service``, as a stack run copies it out before grading.

    A stack run copies the gateway's call record out beside the logs: none of these plays called a model.
    """
    (run_directory / "services").mkdir(exist_ok=True)
    (run_directory / CALL_RECORD_FILE).touch()
    (run_directory / "services" / f"{service}.log").write_text("".join(f"{line}\n" for line in lines))


def write_session_calls(run_directory: Path, agent: str, *walls: float, tools: Sequence[str] = ()) -> None:
    """Write the run's call record: a model call of ``agent``'s loop in the attack at each of ``walls``.

    Each request offers its model ``tools``, by name.
    """
    calls = [
        GatewayCall(
            started_at=wall,
            seconds=0.1,
            caller="episode",
            method="POST",
            path=f"/loop/{agent}/api/v1/chat/completions",
            request={
                "model": "m",
                "messages": [{"role": "user", "content": "go"}],
                "tools": [{"type": "function", "function": {"name": tool}} for tool in tools],
            },
            status=200,
            response={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            complete=True,
            play="attack",
        )
        for wall in walls
    ]
    (run_directory / CALL_RECORD_FILE).write_text("".join(f"{call.model_dump_json()}\n" for call in calls))


def events_of(phase: Path, log: str, kind: str, actor: str | None = None) -> list[Event]:
    return [
        event
        for event in read_events(phase / log)
        if event.kind == kind and (actor is None or event.actor_uid == actor)
    ]


def read_window(phase: Path) -> tuple[datetime, datetime]:
    """The first and the last microsecond of the phase in ``phase``'s play window, from its snapshot.

    A record's time has microseconds, and a snapshot's has more: one microsecond in keeps it inside.
    """
    snapshot = read_snapshot_file(phase)
    started, finished = snapshot.clock[0].wall, snapshot.finished_at or 0.0
    inside = timedelta(microseconds=1)
    return datetime.fromtimestamp(started, UTC) + inside, datetime.fromtimestamp(finished, UTC) - inside


def read_unattributed_reasons(run_directory: Path) -> list[str]:
    path = run_directory / UNATTRIBUTED_FILE
    return [json.loads(line)["reason"] for line in path.read_text().splitlines()] if path.exists() else []
