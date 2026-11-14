"""What a run cost: its memory and CPU per container, its storage, its wall time by phase, and its dollars.

``record_cost`` is the engine's profiler. For as long as a run lasts, a thread of the run's host process
samples the Docker daemon for the containers of the run's compose project (and, while the run is graded, the
containers its agents' code is graded in), so nothing an agent reaches holds the Docker socket. Memory at rest
is each container's at the first sample in which the ``rest_at`` service runs, which starts once the services
it waits for are healthy. ``mark_phase`` marks where play and grading start and end. ``CostRecorder.write``
then writes ``cost.json`` in the run directory: no score, event, monitor or verifier reads it. A sample that
fails is counted in the file and never fails or stops the run. ``render_costs`` prints one run's cost, or the
difference of two.
"""

from __future__ import annotations

import contextlib
import functools
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import NamedTuple

import docker
import psutil
from docker.errors import APIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loc_arena.agent_code import CONTAINER_NAME_PREFIX
from loc_arena.gateway.model_gateway import GatewayCall
from loc_arena.session_events import read_agent, read_usage
from loc_arena.settings import CostSettings

COST_FILE = "cost.json"
PLAY, GRADING = "play", "grading"  # the two phases a run marks; the others lie around and within them
PROJECT_LABEL, SERVICE_LABEL = "com.docker.compose.project", "com.docker.compose.service"
GRADING_CONTAINER = "agent-code"  # every container the agents' code is graded in, under one name
NANOSECONDS = 1_000_000_000
_active: list[CostRecorder] = []  # the recorder of the run this process plays, if it records one


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ContainerCost(_Frozen):
    """One container of the run; a service the honest twin's renewal recreated is one container here."""

    memory_rest_bytes: int | None  # once the services were healthy; none for one that was not running then
    memory_peak_bytes: int
    memory_limit_bytes: int | None  # none: it has no limit
    cpu_seconds: float  # until its last sample


class Dollars(_Frozen):
    """What the run's model calls cost, as each answered call's ``usage.cost`` in the gateway's record."""

    total: float
    by_model: dict[str, float]
    by_caller: dict[str, float]  # an agent, else who launched the workload, else the gateway's name for it


class SamplerReport(_Frozen):
    """How the containers were sampled, and the last thing that went wrong."""

    interval_seconds: float
    samples: int
    failed_samples: int
    last_error: str | None


class RunCost(_Frozen):
    """The cost file of one run."""

    wall_seconds: float
    phases: dict[str, float]  # see PHASES; empty for a run that marked no play and no grading
    containers: dict[str, ContainerCost]
    containers_rest_bytes: int | None  # every container's memory once the services were healthy
    containers_peak_bytes: int  # the most every container held at one sample
    host_memory_peak_bytes: int | None  # the most memory in use on the machine the run's CLI is on
    images: dict[str, int]
    volumes: dict[str, int]  # at the end of grading, before the stack is removed
    records: dict[str, int]  # each entry of the run directory
    dollars: Dollars
    sampler: SamplerReport


class _Mount(BaseModel):
    name: str | None = Field(default=None, alias="Name")  # a volume's; none for a bind mount
    destination: str = Field(alias="Destination")


class _Listed(BaseModel):
    """A container as the Docker daemon lists it."""

    id: str = Field(alias="Id")
    names: list[str] = Field(alias="Names")
    image: str = Field(alias="Image")
    labels: dict[str, str] | None = Field(default=None, alias="Labels")
    mounts: list[_Mount] = Field(default=[], alias="Mounts")


class _Info(BaseModel):
    memory_total: int = Field(alias="MemTotal")  # the daemon's machine's memory


class _Image(BaseModel):
    size: int = Field(alias="Size")


class _MemoryStats(BaseModel):
    usage: int | None = None  # none: the container stopped between the listing and this read
    limit: int | None = None
    stats: dict[str, int] = {}


class _CpuUsage(BaseModel):
    total_usage: int = 0  # nanoseconds since the container started


class _CpuStats(BaseModel):
    cpu_usage: _CpuUsage = _CpuUsage()


class _Stats(BaseModel):
    """One reading of a container's ``GET /containers/{id}/stats``."""

    memory_stats: _MemoryStats = _MemoryStats()
    cpu_stats: _CpuStats = _CpuStats()


class _VolumeUsage(BaseModel):
    size: int = Field(default=-1, alias="Size")  # -1: the daemon could not measure it


class _Volume(BaseModel):
    """A volume as the daemon's disk usage lists it."""

    name: str = Field(alias="Name")
    labels: dict[str, str] | None = Field(default=None, alias="Labels")
    usage: _VolumeUsage | None = Field(default=None, alias="UsageData")


class _VolumeItems(BaseModel):
    items: list[_Volume] = Field(default=[], alias="Items")


class _DiskUsage(BaseModel):
    """``GET /system/df``: the volumes under ``VolumeUsage`` since API 1.52, else under ``Volumes``."""

    volume_usage: _VolumeItems | None = Field(default=None, alias="VolumeUsage")
    volumes: list[_Volume] | None = Field(default=None, alias="Volumes")


class _Reading(NamedTuple):
    """One container as one sample read it."""

    id: str
    name: str  # its compose service, or the grading container's shared name
    image: str
    held: int  # memory in use, as `docker stats` shows it: usage less the inactive file cache
    limit: int | None  # none: no limit below the daemon's machine's memory
    cpu: int  # nanoseconds since it started
    volumes: dict[str, str]  # each volume it mounts, as service:path; Compose labels no anonymous one


@dataclass
class _Seen:
    """One container, by its id, as the samples saw it."""

    name: str
    image: str
    rest: int | None = None
    peak: int = 0
    limit: int | None = None
    cpu_before: int = 0  # of its earlier starts: a container started again counts from zero
    cpu: int = 0


@dataclass
class CostRecorder:
    """One run's cost as it is measured; ``write`` makes its file."""

    settings: CostSettings
    project: str
    rest_at: str  # the compose service whose start marks the containers' memory at rest
    clock: Callable[[], float]
    started: float = 0.0
    ended: float = 0.0
    marks: dict[str, tuple[float, float]] = field(default_factory=dict)
    _seen: dict[str, _Seen] = field(default_factory=dict)
    _open: set[str] = field(default_factory=set)  # the phases under way
    _rest: int | None = None
    _rested: float | None = None  # when memory at rest was taken
    _peak: int = 0
    _host_peak: int | None = None
    _samples: int = 0
    _failed: int = 0
    _last_error: str | None = None
    _mounted: dict[str, str] = field(default_factory=dict)  # a volume a container mounts: service:path
    _images: dict[str, int] = field(default_factory=dict)
    _volumes: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stopped: threading.Event = field(default_factory=threading.Event)

    def _connect(self) -> docker.DockerClient:
        return docker.from_env(timeout=self.settings.request_seconds)

    def _fail(self, error: Exception) -> None:
        with self._lock:
            self._failed += 1
            self._last_error = f"{type(error).__name__}: {error}"

    def watch(self) -> None:
        """Sample until ``stop``; a sample that fails is counted and the next one is tried."""
        client: docker.DockerClient | None = None
        memory_total = 0  # the daemon's machine's memory, read once a connection
        while True:
            try:
                if client is None:
                    client = self._connect()
                    memory_total = _Info.model_validate(client.info()).memory_total
                self._sample(client, memory_total)
            except Exception as error:  # noqa: BLE001 - the profiler never fails or stops the run
                self._fail(error)
            if self._stopped.wait(self.settings.sample_seconds):
                break
        if client is not None:
            client.close()

    def stop(self) -> None:
        """End the sampling; ``watch`` returns after the sample it is taking."""
        self._stopped.set()

    def _read(self, client: docker.DockerClient, memory_total: int, *, grading: bool) -> list[_Reading]:
        """Each container of the run that runs now, as this sample reads it."""
        read = []
        for listed in map(_Listed.model_validate, client.api.containers()):
            labels, name = listed.labels or {}, (listed.names or ["/"])[0].lstrip("/")
            if labels.get(PROJECT_LABEL) == self.project:
                name = labels.get(SERVICE_LABEL, name)
            elif grading and name.startswith(CONTAINER_NAME_PREFIX):
                name = GRADING_CONTAINER
            else:
                continue
            try:
                stats = _Stats.model_validate(client.api.stats(listed.id, stream=False, one_shot=True))
            except APIError:  # it is being removed, or was removed, between the listing and this read
                continue
            memory = stats.memory_stats
            if memory.usage is not None:  # by cgroup v1's name of the cache, else v2's
                cache = memory.stats.get("total_inactive_file", memory.stats.get("inactive_file", 0))
                limit = memory.limit if memory.limit and memory.limit < memory_total else None
                cpu = stats.cpu_stats.cpu_usage.total_usage
                volumes = {mount.name: f"{name}:{mount.destination}" for mount in listed.mounts if mount.name}
                read.append(
                    _Reading(listed.id, name, listed.image, memory.usage - cache, limit, cpu, volumes),
                )
        return read

    def _sample(self, client: docker.DockerClient, memory_total: int) -> None:
        machine = psutil.virtual_memory()
        read = self._read(client, memory_total, grading=GRADING in self._open)
        held_now = {reading.id: reading.held for reading in read}
        with self._lock:
            for reading in read:
                seen = self._seen.setdefault(reading.id, _Seen(reading.name, reading.image))
                seen.cpu_before += seen.cpu if reading.cpu < seen.cpu else 0
                seen.cpu, seen.peak, seen.limit = reading.cpu, max(seen.peak, reading.held), reading.limit
                self._mounted = reading.volumes | self._mounted
            if (
                self._rested is None
                and PLAY in self._open
                and self.rest_at in {reading.name for reading in read}
            ):
                self._rested, self._rest = self.clock(), sum(held_now.values())
                for container_id, held in held_now.items():
                    self._seen[container_id].rest = held
            self._samples += 1
            self._peak = max(self._peak, sum(held_now.values()))
            self._host_peak = max(self._host_peak or 0, machine.total - machine.available)

    @property
    def samples(self) -> int:
        """How many samples have been taken so far."""
        with self._lock:
            return self._samples

    def start_phase(self, phase: str) -> float:
        """Mark the start of ``phase``."""
        with self._lock:
            self._open.add(phase)
        return self.clock()

    def end_phase(self, phase: str, started: float) -> None:
        """Mark the end of ``phase``; as grading ends, the stack still stands, so its storage is measured."""
        self.marks[phase] = (started, self.clock())
        with self._lock:
            self._open.discard(phase)
            images, mounted = {seen.image for seen in self._seen.values()}, dict(self._mounted)
        if phase == GRADING:
            try:
                self._measure_storage(images, mounted)
            except Exception as error:  # noqa: BLE001 - the profiler never fails or stops the run
                self._fail(error)

    def _measure_storage(self, images: set[str], mounted: Mapping[str, str]) -> None:
        client = self._connect()
        try:
            for image in images:
                self._images[image] = _Image.model_validate(client.api.inspect_image(image)).size
            usage = _DiskUsage.model_validate(client.df())
            listed = usage.volume_usage.items if usage.volume_usage else usage.volumes or []
            for volume in listed:
                size = volume.usage.size if volume.usage else -1
                if (volume.labels or {}).get(PROJECT_LABEL) == self.project and size >= 0:
                    self._volumes[volume.name.removeprefix(f"{self.project}_")] = size
                elif volume.name in mounted and size >= 0:  # each play's, when the renewal made it anew
                    self._volumes[mounted[volume.name]] = self._volumes.get(mounted[volume.name], 0) + size
        finally:
            client.close()

    def _split_phases(self) -> dict[str, float]:
        """Wall time by phase: the stack's start, its services' start within play when seen, and the rest."""
        if PLAY not in self.marks or GRADING not in self.marks:
            return {}
        (play_start, play_end), (grading_start, grading_end) = self.marks[PLAY], self.marks[GRADING]
        rested = self._rested if self._rested is not None and play_start <= self._rested <= play_end else None
        services = {} if rested is None else {"services_start": rested - play_start}
        return {
            "stack_start": play_start - self.started,
            **services,
            PLAY: play_end - (rested or play_start),
            "copy": grading_start - play_end,
            GRADING: grading_end - grading_start,
            "teardown": self.ended - grading_end,
        }

    def write(self, run_directory: Path, call_log: Path) -> RunCost:
        """Write the run's cost file in ``run_directory``, its dollars read from ``call_log``; return it."""
        containers: dict[str, ContainerCost] = {}
        for seen in self._seen.values():
            earlier = containers.get(seen.name)
            kept = earlier.memory_rest_bytes if earlier else None
            rest = seen.rest if kept is None else kept
            containers[seen.name] = ContainerCost(
                memory_rest_bytes=rest,
                memory_peak_bytes=max(seen.peak, earlier.memory_peak_bytes if earlier else 0),
                memory_limit_bytes=seen.limit,
                cpu_seconds=(seen.cpu_before + seen.cpu) / NANOSECONDS
                + (earlier.cpu_seconds if earlier else 0),
            )
        cost = RunCost(
            wall_seconds=self.ended - self.started,
            phases=self._split_phases(),
            containers=dict(sorted(containers.items())),
            containers_rest_bytes=self._rest,
            containers_peak_bytes=self._peak,
            host_memory_peak_bytes=self._host_peak,
            images=dict(sorted(self._images.items())),
            volumes=dict(sorted(self._volumes.items())),
            records=measure_records(run_directory),
            dollars=read_dollars(call_log),
            sampler=SamplerReport(
                interval_seconds=self.settings.sample_seconds,
                samples=self._samples,
                failed_samples=self._failed,
                last_error=self._last_error,
            ),
        )
        (run_directory / COST_FILE).write_text(cost.model_dump_json(indent=2) + "\n")
        return cost


@contextlib.contextmanager
def record_cost(
    settings: CostSettings,
    project: str = "",
    rest_at: str = "",
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[CostRecorder]:
    """Record what the run inside costs: sample compose project ``project`` until the block ends."""
    recorder = CostRecorder(settings, project, rest_at, clock)
    recorder.started = clock()
    watcher = threading.Thread(target=recorder.watch, daemon=True)
    _active.append(recorder)
    watcher.start()
    try:
        yield recorder
    finally:
        _active.remove(recorder)
        recorder.stop()
        watcher.join()
        recorder.ended = clock()


def mark_phase[**P, R](phase: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Mark ``phase`` around each call of the function, in the cost of the run being recorded, if one is."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(function)
        def run(*args: P.args, **kwargs: P.kwargs) -> R:
            if not _active:
                return function(*args, **kwargs)
            recorder = _active[-1]
            started = recorder.start_phase(phase)
            try:
                return function(*args, **kwargs)
            finally:
                recorder.end_phase(phase, started)

        return run

    return decorate


def read_dollars(call_log: Path) -> Dollars:
    """The dollars of every answered call in ``call_log``, the gateway's record; none without the file."""
    by_model: dict[str, float] = defaultdict(float)
    by_caller: dict[str, float] = defaultdict(float)
    lines = call_log.read_text(encoding="utf-8").splitlines() if call_log.exists() else []
    for line in lines:
        try:
            call = GatewayCall.model_validate_json(line)
        except ValidationError:
            continue
        cost = read_usage(call.response).get("cost") if call.status == HTTPStatus.OK else None
        if isinstance(cost, bool) or not isinstance(cost, int | float):
            continue
        model = call.request.get("model") if isinstance(call.request, dict) else None
        by_model[str(model)] += cost
        by_caller[read_agent(call) or call.sponsor or call.caller] += cost

    def round_each(dollars: Mapping[str, float]) -> dict[str, float]:
        return {name: round(amount, 6) for name, amount in sorted(dollars.items())}

    total = round(sum(by_model.values()), 6)
    return Dollars(total=total, by_model=round_each(by_model), by_caller=round_each(by_caller))


def measure_records(run_directory: Path) -> dict[str, int]:
    """The bytes of each entry of ``run_directory``, a directory as the files under it; not the cost file."""
    sizes = {}
    for entry in sorted(run_directory.iterdir()):
        if entry.name == COST_FILE or entry.is_symlink():
            continue
        files = [entry] if entry.is_file() else [path for path in entry.rglob("*") if path.is_file()]
        sizes[entry.name] = sum(path.lstat().st_size for path in files)
    return sizes


def read_cost(run_directory: Path) -> RunCost:
    """The cost file of the run in ``run_directory``."""
    return RunCost.model_validate_json((run_directory / COST_FILE).read_text(encoding="utf-8"))


def summarize(cost: RunCost) -> str:
    """One line of ``cost`` for the run's summary."""
    phases = "".join(f" {name}={seconds:.0f}s" for name, seconds in cost.phases.items())
    return (
        f"cost wall={cost.wall_seconds:.0f}s{phases} "
        f"containers_peak={_format_mebibytes(cost.containers_peak_bytes)} "
        f"host_peak={_format_mebibytes(cost.host_memory_peak_bytes)} "
        f"dollars={_format_dollars(cost.dollars.total)}"
    )


def _format_mebibytes(value: float | None) -> str:
    return "-" if value is None else f"{value / 2**20:.1f} MiB"


def _format_seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} s"


def _format_dollars(value: float | None) -> str:
    return "-" if value is None else f"${value:.4f}".replace("$-", "-$")


Cell = tuple[str, float | None, Callable[[float | None], str]]


def _list_rows(cost: RunCost) -> list[tuple[str, list[Cell]]]:
    """Every figure of ``cost`` as a labelled row of named cells, each with how it prints."""
    scalar: list[Cell] = [
        ("wall time", cost.wall_seconds, _format_seconds),
        *((f"  {phase}", seconds, _format_seconds) for phase, seconds in cost.phases.items()),
        ("containers at rest", cost.containers_rest_bytes, _format_mebibytes),
        ("containers at peak", cost.containers_peak_bytes, _format_mebibytes),
        ("host at peak", cost.host_memory_peak_bytes, _format_mebibytes),
        ("images", sum(cost.images.values()), _format_mebibytes),
        ("volumes", sum(cost.volumes.values()), _format_mebibytes),
        ("records", sum(cost.records.values()), _format_mebibytes),
        ("dollars", cost.dollars.total, _format_dollars),
        *((f"  model {model}", amount, _format_dollars) for model, amount in cost.dollars.by_model.items()),
        *((f"  caller {name}", amount, _format_dollars) for name, amount in cost.dollars.by_caller.items()),
    ]
    containers: list[tuple[str, list[Cell]]] = [
        (
            f"container {name}",
            [
                ("rest", container.memory_rest_bytes, _format_mebibytes),
                ("peak", container.memory_peak_bytes, _format_mebibytes),
                ("limit", container.memory_limit_bytes, _format_mebibytes),
                ("cpu", container.cpu_seconds, _format_seconds),
            ],
        )
        for name, container in cost.containers.items()
    ]
    rows = [*((label, [("", value, show)]) for label, value, show in scalar), *containers]
    # A label is a service's or a model's name, which the run's code chose: printed only as printable text.
    return [("".join(c if c.isprintable() else "?" for c in label), cells) for label, cells in rows]


def render_costs(cost: RunCost, other: RunCost | None = None) -> str:
    """What ``cost`` holds, a figure a row; with ``other``, its figures and how each differs from ``cost``."""
    first = dict(_list_rows(cost))
    rows = _list_rows(other) if other else list(first.items())
    missing = [label for label in first if label not in dict(rows)] if other else []
    added = {label for label, _ in rows if label not in first}
    width = max(len(label) for label in [*dict(rows), *missing])
    lines = []
    for label, cells in rows:
        before = {name: value for name, value, _ in first.get(label, [])}
        shown = []
        for name, value, show in cells:
            earlier = before.get(name)
            changed = other is not None and value is not None and earlier is not None
            delta = f" ({'+' if value - earlier >= 0 else ''}{show(value - earlier)})" if changed else ""
            shown.append(f"{name} {show(value)}{delta}".strip())
        new = "  only in the second run" if label in added else ""
        lines.append(f"{label:<{width}}  " + "  ".join(shown) + new)
    lines += [f"{label:<{width}}  only in the first run" for label in missing]
    return "\n".join(lines)
