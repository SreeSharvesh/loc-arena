"""What the event builder's tests share: an attack run played in process, and the services' logs in it."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.gateway.core import Move, ScriptedProvider
from loc_arena.gateway.model_gateway import CALL_RECORD_FILE
from loc_arena.harness import play_run
from loc_arena.logging_.events import Event, read_events
from loc_arena.recorded_events import UNATTRIBUTED_FILE
from loc_arena.task import read_snapshot_file

LIVE = dataclasses.replace(
    load_run_config("configs/aurora-efficiency.deterministic.yaml"),
    policy="model",
    agent_transcript=True,
)
LOOK_AROUND = Move(tool="list_dir", args={"path": "."})
SEALED, MIRROR = "events.sealed.jsonl", "events.mirror.jsonl"


def play(run_directory: Path, *, episode: list[Move] | None = None, twin: list[Move] | None = None) -> Path:
    """Play an attack run where serving-agent replies ``episode`` in the episode and ``twin`` in its twin."""
    play_run(
        LIVE,
        run_directory,
        mode="attack",
        provider=ScriptedProvider({"serving-agent": episode or []}),
        calibration_provider=ScriptedProvider({"serving-agent": twin or []}),
    )
    return run_directory


def write_log(run_directory: Path, service: str, *lines: str) -> None:
    """Write ``lines`` as the run's log for ``service``, as a stack run copies it out before grading.

    A stack run copies the gateway's call record out beside the logs: none of these plays called a model.
    """
    (run_directory / "services").mkdir(exist_ok=True)
    (run_directory / CALL_RECORD_FILE).touch()
    (run_directory / "services" / f"{service}.log").write_text("".join(f"{line}\n" for line in lines))


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
