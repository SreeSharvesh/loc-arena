"""The events monitors read, built after play from each service's record of the tool calls made to it.

Each call to a live service with tools is one record (``loc_arena.tool_records``): in process, in
``records/<service>.jsonl`` of the episode's directory; in a stack run, among the lines of the service's log
that start with ``{``, copied to ``services/<service>.log`` in the run directory. A record belongs to the
episode, or the honest twin, whose play window holds its time. Its event takes the episode clock's reading at
that time and the native turn of its agent running then, else turn 0, and is written after every event of
play to both the sealed and the mirror log, as the harness wrote it live. A record placed nowhere, or a native
agent's outside all its turns, is noted in ``unattributed_records.jsonl`` in the run directory. Grading never
builds, so grading a run again adds nothing.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sandbox_server.confinement import resolve_inside

from loc_arena.config import RunConfig
from loc_arena.forge.service import SERVICE as FORGE_SERVICE
from loc_arena.logging_.agent_trace import EpisodeTrace
from loc_arena.logging_.events import AppendOnlyLog, EventKind, read_events
from loc_arena.scaffold.bus import Recorder
from loc_arena.task import SNAPSHOT_FILE, SnapshotFile, read_snapshot_file
from loc_arena.tool_records import ToolRecord

_LOGGER = logging.getLogger(__name__)
SERVICE_LOGS = "services"  # a stack run's copy of each live service's log, in the run directory
RECORDS = "records"  # where an in-process run's services record, in each episode's directory
UNATTRIBUTED_FILE = "unattributed_records.jsonl"


@dataclass(frozen=True)
class RecordedEvent:
    """The event a record makes, as monitors read it; its time, turn and actor's role come from the run."""

    kind: EventKind
    actor_uid: str
    target_id: str | None
    payload: dict[str, Any]


class UnattributedRecord(BaseModel):
    """One line of ``unattributed_records.jsonl``: a service's line, and why no event was placed by it."""

    model_config = ConfigDict(frozen=True)

    reason: str = Field(
        description=(
            "Why: not a valid record, outside every phase window, not an agent of the run, or no covering "
            "turn (its event is built in turn 0)."
        ),
    )
    line: str = Field(description="The line as the service wrote it.")


def build_pull_request(record: ToolRecord) -> RecordedEvent | None:
    """The ``pr`` event of a pull request the forge opened, with the fields the PR review monitor reads."""
    result = record.result or {}
    if not result.get("opened"):
        return None
    return RecordedEvent(
        "pr",
        record.agent,
        str(result["repo"]),
        {
            "number": result["number"],
            "target_repo": result["repo"],
            "pr_title": result["title"],
            "pr_body": str(record.arguments.get("body", "")),
            "diff": str(record.arguments.get("diff", "")),
            "ci": result["ci"],
        },
    )


# What each tool's successful call makes, by (service, tool); a call of any other tool makes no event.
EVENT_BUILDERS: Mapping[tuple[str, str], Callable[[ToolRecord], RecordedEvent | None]] = {
    (FORGE_SERVICE, "open_pr"): build_pull_request,
}


@dataclass(frozen=True)
class _Phase:
    """One played episode: its directory, its snapshot, and the native turns of its trace."""

    directory: Path
    snapshot: SnapshotFile
    trace: EpisodeTrace | None

    def holds(self, wall: float) -> bool:
        """Whether ``wall`` falls inside this episode's play window."""
        clock, finished = self.snapshot.clock, self.snapshot.finished_at
        return bool(clock) and finished is not None and clock[0].wall <= wall <= finished

    def read_clock(self, wall: float) -> float:
        """The episode time at ``wall``: the clock's last reading at or before it."""
        walls = [reading.wall for reading in self.snapshot.clock]
        return self.snapshot.clock[bisect.bisect_right(walls, wall) - 1].ts

    def find_turn(self, agent: str, wall: float) -> int | None:
        """The turn of ``agent`` running at ``wall``: 0 if it has no native turn, none if none ran."""
        turns = [turn for turn in self.trace.turns if turn.ref.agent_uid == agent] if self.trace else []
        covering = (turn.ref.turn for turn in turns if turn.wall_start <= wall <= turn.wall_end)
        return next(covering, None if turns else 0)


@dataclass(frozen=True)
class _Placed:
    """A record's event, placed: its episode, its wall time and its agent's turn."""

    phase: _Phase
    wall: float
    event: RecordedEvent
    turn: int


def build_recorded_events(
    run_directory: Path,
    config: RunConfig,
    traces: Mapping[str, EpisodeTrace | None],
) -> None:
    """Write the events of every record of the run's services into the episode it was made in.

    ``traces`` holds each episode's trace by the name of its directory in ``run_directory``; an episode that
    did not play is skipped.
    """
    phases = [
        _Phase(run_directory / name, read_snapshot_file(run_directory / name), trace)
        for name, trace in traces.items()
        if (run_directory / name / SNAPSHOT_FILE).exists()
    ]
    roles = {agent.id: agent.kind for agent in config.agents}
    services = [service.name for service in config.live_services if service.tools]
    unattributed: list[UnattributedRecord] = []
    built: dict[Path, list[_Placed]] = {phase.directory: [] for phase in phases}
    for service, line in _read_record_lines(run_directory, services, phases):
        placed, note = _place(service, line, phases, roles)
        unattributed += [note] if note else []
        if placed:
            built[placed.phase.directory].append(placed)
    for phase in phases:
        _write_events(phase, sorted(built[phase.directory], key=lambda placed: placed.wall), roles)
    if unattributed:
        _LOGGER.warning("%d service records were not placed: see %s", len(unattributed), UNATTRIBUTED_FILE)
        lines = "".join(f"{record.model_dump_json()}\n" for record in unattributed)
        (run_directory / UNATTRIBUTED_FILE).write_text(lines, encoding="utf-8")


def _read_record_lines(
    run_directory: Path,
    services: list[str],
    phases: list[_Phase],
) -> Iterator[tuple[str, str]]:
    """Each service's record lines, by service: those of its log in a stack run, else of each episode."""
    for service in services:
        stack_log = run_directory / SERVICE_LOGS / f"{service}.log"
        in_process = [phase.directory / RECORDS / f"{service}.jsonl" for phase in phases]
        for path in (path for path in [stack_log, *in_process] if path.exists()):
            # A line not starting with "{" is one the service logged around its records, such as uvicorn's.
            lines = path.read_text(encoding="utf-8").splitlines()
            yield from ((service, line) for line in lines if line.startswith("{"))


def _place(
    service: str,
    line: str,
    phases: list[_Phase],
    roles: Mapping[str, str],
) -> tuple[_Placed | None, UnattributedRecord | None]:
    """The event of ``service``'s record ``line``, placed, if it makes one; a note if it is not placed."""
    try:
        record = ToolRecord.model_validate_json(line)
    except ValidationError:
        return None, UnattributedRecord(reason="not a valid record", line=line)
    wall = record.time.timestamp()
    phase = next((phase for phase in phases if phase.holds(wall)), None)
    if phase is None or record.agent not in roles:
        reason = "outside every phase window" if phase is None else "not an agent of the run"
        return None, UnattributedRecord(reason=reason, line=line)
    build = EVENT_BUILDERS.get((service, record.tool))
    event = build(record) if build is not None and record.error is None else None
    if event is None:
        return None, None
    turn = phase.find_turn(record.agent, wall)
    note = UnattributedRecord(reason="no covering turn", line=line) if turn is None else None
    return _Placed(phase, wall, event, turn or 0), note


def _write_events(phase: _Phase, built: list[_Placed], roles: Mapping[str, str]) -> None:
    """Append ``built`` to both logs of ``phase``, after every event its play wrote."""
    if not built:
        return
    sealed_path = resolve_inside(phase.directory, phase.snapshot.sealed_path)
    episode_id = next(read_events(sealed_path)).episode_id
    sealed = AppendOnlyLog(sealed_path, episode_id)
    mirror = AppendOnlyLog(resolve_inside(phase.directory, phase.snapshot.mirror_path), episode_id)
    recorder = Recorder(episode_id, sealed, mirror)
    for placed in built:
        recorder.dual(
            actor_uid=placed.event.actor_uid,
            actor_role=roles[placed.event.actor_uid],
            kind=placed.event.kind,
            payload=placed.event.payload,
            target_id=placed.event.target_id,
            turn=placed.turn,
            ts=phase.read_clock(placed.wall),
        )
