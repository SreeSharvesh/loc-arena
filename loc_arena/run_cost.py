"""What a run cost: its memory and CPU per container, its storage, its wall time by phase, and its dollars.

``record_cost`` is the engine's profiler. For as long as a run lasts, a thread of the run's host process
samples the Docker daemon for the containers of the run's compose project (and, while the run is graded, the
containers its agents' code is graded in), so nothing an agent reaches holds the Docker socket. ``timed``
marks where play and grading start and end. ``CostRecorder.write`` then writes ``cost.json`` in the run
directory: no score, event, monitor or verifier reads it. A sample that fails is counted and said in the file,
and never stops the run. ``render_costs`` prints one run's cost, or the difference of two.
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

import docker
import psutil
from docker.errors import DockerException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loc_arena.agent_code import CONTAINER_NAME_PREFIX
from loc_arena.gateway.model_gateway import GatewayCall
from loc_arena.session_events import read_agent, read_usage
from loc_arena.settings import CostSettings

COST_FILE = "cost.json"
PLAY, GRADING = "play", "grading"  # the two phases a run marks; the other three lie around them
PROJECT_LABEL, SERVICE_LABEL = "com.docker.compose.project", "com.docker.compose.service"
GRADING_CONTAINER = "agent-code"  # every container the agents' code is graded in, under one name
NANOSECONDS = 1_000_000_000
SAMPLE_ERRORS = (DockerException, OSError, ValueError, KeyError, TypeError)  # OSError: no daemon answered
_active: list[CostRecorder] = []  # the recorder of the run this process plays, if it records one


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ContainerCost(_Frozen):
    """One container of the run; a service the honest twin's renewal recreated is one container here."""

    memory_rest_bytes: int | None  # at the last sample before play; none for one that started later
    memory_peak_bytes: int
    memory_limit_bytes: int | None  # none: it has no limit
    cpu_seconds: float


class Dollars(_Frozen):
    """What the run's model calls cost, as each answered call's ``usage.cost`` in the gateway's record."""

    total: float
    by_model: dict[str, float]
    by_caller: dict[str, float]  # an agent, else who launched the workload, else the gateway's name for it


class SamplerReport(_Frozen):
    """How the containers were sampled, and what went wrong."""

    interval_seconds: float
    samples: int
    failed_samples: int
    errors: list[str]


class RunCost(_Frozen):
    """The cost file of one run."""

    wall_seconds: float
    phases: dict[str, float]  # stack_start, play, copy, grading, teardown; empty for a run that marked none
    containers: dict[str, ContainerCost]
    containers_rest_bytes: int | None  # every container's memory at the last sample before play
    containers_peak_bytes: int  # the most every container held at one sample
    host_memory_peak_bytes: int | None  # the most memory in use on the machine the run's CLI is on
    images: dict[str, int]
    volumes: dict[str, int]  # at the end of grading, before the stack is removed
    records: dict[str, int]  # each entry of the run directory
    dollars: Dollars
    sampler: SamplerReport


class _Listed(BaseModel):
    """A container as the Docker daemon lists it."""

    id: str = Field(alias="Id")
    names: list[str] = Field(alias="Names")
    image: str = Field(alias="Image")
    labels: dict[str, str] | None = Field(default=None, alias="Labels")


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


@dataclass
class _Seen:
    """One container, by its id, as the samples saw it."""

    name: str
    image: str
    used: int = 0
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
    clock: Callable[[], float]
    started: float = 0.0
    ended: float = 0.0
    marks: dict[str, tuple[float, float]] = field(default_factory=dict)
    _seen: dict[str, _Seen] = field(default_factory=dict)
    _open: set[str] = field(default_factory=set)  # the phases under way
    _latest: int = 0  # what every container held at the last sample
    _rest: int | None = None
    _peak: int = 0
    _host_peak: int | None = None
    _samples: int = 0
    _failed: int = 0
    _errors: list[str] = field(default_factory=list)
    _images: dict[str, int] = field(default_factory=dict)
    _volumes: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop: threading.Event = field(default_factory=threading.Event)

    def _connect(self) -> docker.DockerClient:
        timeout = self.settings.request_seconds
        if self.settings.docker_host is None:
            return docker.from_env(timeout=timeout)
        return docker.DockerClient(base_url=self.settings.docker_host, timeout=timeout)

    def _fail(self, error: Exception) -> None:
        self._failed += 1
        daemon = self.settings.docker_host or "the docker CLI's daemon"
        message = f"{daemon}: {type(error).__name__}: {error}"
        if message not in self._errors and len(self._errors) < self.settings.error_limit:
            self._errors.append(message)

    def _watch(self) -> None:
        """Sample until the run ends; a sample that fails is counted and the next one is tried."""
        client: docker.DockerClient | None = None
        while True:
            try:
                client = client or self._connect()
                self._sample(client)
            except SAMPLE_ERRORS as error:
                with self._lock:
                    self._fail(error)
            if self._stop.wait(self.settings.sample_seconds):
                break
        if client is not None:
            client.close()

    def _sample(self, client: docker.DockerClient) -> None:
        memory = psutil.virtual_memory()
        grading = GRADING in self._open
        used = 0
        for listed in client.api.containers():
            container = _Listed.model_validate(listed)
            labels = container.labels or {}
            name = container.names[0].lstrip("/")
            if labels.get(PROJECT_LABEL) == self.project:
                name = labels.get(SERVICE_LABEL, name)
            elif grading and name.startswith(CONTAINER_NAME_PREFIX):
                name = GRADING_CONTAINER
            else:
                continue
            stats = _Stats.model_validate(client.api.stats(container.id, stream=False, one_shot=True))
            usage, cpu = stats.memory_stats, stats.cpu_stats.cpu_usage.total_usage
            if usage.usage is None:
                continue
            # As `docker stats` shows it: usage less the inactive file cache, by cgroup v2's name or v1's.
            held = usage.usage - usage.stats.get("inactive_file", usage.stats.get("total_inactive_file", 0))
            with self._lock:
                seen = self._seen.setdefault(container.id, _Seen(name, container.image))
                if cpu < seen.cpu:
                    seen.cpu_before += seen.cpu
                seen.used, seen.cpu, seen.peak = held, cpu, max(seen.peak, held)
                seen.limit = usage.limit if usage.limit and usage.limit < memory.total else None
            used += held
        with self._lock:
            self._samples += 1
            self._peak = max(self._peak, used)
            self._host_peak = max(self._host_peak or 0, memory.total - memory.available)
            self._latest = used

    def start_phase(self, phase: str) -> float:
        """Mark the start of ``phase``; as play starts, what each container holds is its memory at rest."""
        with self._lock:
            self._open.add(phase)
            if phase == PLAY and self._samples:
                self._rest = self._latest
                for seen in self._seen.values():
                    seen.rest = seen.used
        return self.clock()

    def end_phase(self, phase: str, started: float) -> None:
        """Mark the end of ``phase``; as grading ends, the stack still stands, so its storage is measured."""
        self.marks[phase] = (started, self.clock())
        with self._lock:
            self._open.discard(phase)
        if phase == GRADING:
            try:
                self._measure_storage()
            except SAMPLE_ERRORS as error:
                with self._lock:
                    self._fail(error)

    def _measure_storage(self) -> None:
        client = self._connect()
        try:
            for image in {seen.image for seen in self._seen.values()}:
                self._images[image] = client.api.inspect_image(image)["Size"]
            usage = client.df()
            listed = (usage.get("VolumeUsage") or {}).get("Items") or usage.get("Volumes") or []
            for volume in map(_Volume.model_validate, listed):
                size = volume.usage.size if volume.usage else -1
                if (volume.labels or {}).get(PROJECT_LABEL) == self.project and size >= 0:
                    self._volumes[volume.name.removeprefix(f"{self.project}_")] = size
        finally:
            client.close()

    def _split_phases(self) -> dict[str, float]:
        if PLAY not in self.marks or GRADING not in self.marks:
            return {}
        (play_start, play_end), (grading_start, grading_end) = self.marks[PLAY], self.marks[GRADING]
        return {
            "stack_start": play_start - self.started,
            PLAY: play_end - play_start,
            "copy": grading_start - play_end,
            GRADING: grading_end - grading_start,
            "teardown": self.ended - grading_end,
        }

    def write(self, run_directory: Path, call_log: Path) -> RunCost:
        """Write the run's cost file in ``run_directory``, its dollars read from ``call_log``; return it."""
        containers: dict[str, ContainerCost] = {}
        for seen in self._seen.values():
            earlier = containers.get(seen.name)
            containers[seen.name] = ContainerCost(
                memory_rest_bytes=seen.rest if earlier is None else earlier.memory_rest_bytes or seen.rest,
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
                errors=self._errors,
            ),
        )
        (run_directory / COST_FILE).write_text(cost.model_dump_json(indent=2) + "\n")
        return cost


@contextlib.contextmanager
def record_cost(
    settings: CostSettings,
    project: str = "",
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[CostRecorder]:
    """Record what the run inside costs: sample compose project ``project`` until the block ends."""
    recorder = CostRecorder(settings, project, clock)
    recorder.started = clock()
    watcher = threading.Thread(target=recorder._watch, daemon=True)  # noqa: SLF001 - the recorder's own loop
    _active.append(recorder)
    watcher.start()
    try:
        yield recorder
    finally:
        _active.remove(recorder)
        recorder._stop.set()  # noqa: SLF001
        watcher.join()
        recorder.ended = clock()


def timed[**P, R](phase: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
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

    def rounded(dollars: Mapping[str, float]) -> dict[str, float]:
        return {name: round(amount, 6) for name, amount in sorted(dollars.items())}

    return Dollars(
        total=round(sum(by_model.values()), 6),
        by_model=rounded(by_model),
        by_caller=rounded(by_caller),
    )


def measure_records(run_directory: Path) -> dict[str, int]:
    """The bytes of each entry of ``run_directory``, a directory as the files under it; not the cost file."""
    sizes = {}
    for entry in sorted(run_directory.iterdir()):
        files = [entry] if entry.is_file() else [path for path in entry.rglob("*") if path.is_file()]
        if entry.name != COST_FILE and not entry.is_symlink():
            sizes[entry.name] = sum(path.lstat().st_size for path in files)
    return sizes


def read_cost(run_directory: Path) -> RunCost:
    """The cost file of the run in ``run_directory``."""
    return RunCost.model_validate_json((run_directory / COST_FILE).read_text(encoding="utf-8"))


def summarize(cost: RunCost) -> str:
    """One line of ``cost`` for the run's summary."""
    phases = " ".join(f"{name}={seconds:.0f}s" for name, seconds in cost.phases.items())
    return (
        f"cost wall={cost.wall_seconds:.0f}s {phases} "
        f"containers_peak={_mebibytes(cost.containers_peak_bytes)} "
        f"host_peak={_mebibytes(cost.host_memory_peak_bytes)} dollars={_dollars(cost.dollars.total)}"
    ).replace("  ", " ")


def _mebibytes(value: float | None) -> str:
    return "-" if value is None else f"{value / 2**20:.1f} MiB"


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f} s"


def _dollars(value: float | None) -> str:
    return "-" if value is None else f"${value:.4f}".replace("$-", "-$")


Row = tuple[str, list[tuple[str, float | None, Callable[[float | None], str]]]]


def _list_rows(cost: RunCost) -> list[Row]:
    """Every figure of ``cost`` as a labelled row of named cells, each with how it prints."""
    scalar: list[tuple[str, float | None, Callable[[float | None], str]]] = [
        ("wall time", cost.wall_seconds, _seconds),
        *((f"  {phase}", seconds, _seconds) for phase, seconds in cost.phases.items()),
        ("containers at rest", cost.containers_rest_bytes, _mebibytes),
        ("containers at peak", cost.containers_peak_bytes, _mebibytes),
        ("host at peak", cost.host_memory_peak_bytes, _mebibytes),
        ("images", sum(cost.images.values()), _mebibytes),
        ("volumes", sum(cost.volumes.values()), _mebibytes),
        ("records", sum(cost.records.values()), _mebibytes),
        ("dollars", cost.dollars.total, _dollars),
        *((f"  model {model}", amount, _dollars) for model, amount in cost.dollars.by_model.items()),
        *((f"  caller {caller}", amount, _dollars) for caller, amount in cost.dollars.by_caller.items()),
    ]
    containers: list[Row] = [
        (
            name,
            [
                ("rest", container.memory_rest_bytes, _mebibytes),
                ("peak", container.memory_peak_bytes, _mebibytes),
                ("limit", container.memory_limit_bytes, _mebibytes),
                ("cpu", container.cpu_seconds, _seconds),
            ],
        )
        for name, container in cost.containers.items()
    ]
    return [*((label, [("", value, show)]) for label, value, show in scalar), *containers]


def render_costs(cost: RunCost, other: RunCost | None = None) -> str:
    """What ``cost`` holds, a figure a row; with ``other``, its figures and how each differs from ``cost``."""
    first = dict(_list_rows(cost))
    rows = _list_rows(other) if other else list(first.items())
    missing = [(label, cells) for label, cells in first.items() if label not in dict(rows)] if other else []
    width = max(len(label) for label, _ in [*rows, *missing])
    lines = []
    for label, cells in rows:
        before = {name: value for name, value, _ in first.get(label, [])}
        shown = []
        for name, value, show in cells:
            earlier = before.get(name)
            changed = other is not None and value is not None and earlier is not None
            delta = f" ({'+' if value - earlier >= 0 else ''}{show(value - earlier)})" if changed else ""
            shown.append(f"{name} {show(value)}{delta}".strip())
        lines.append(f"{label:<{width}}  " + "  ".join(shown))
    lines += [f"{label:<{width}}  only in the first run" for label, _ in missing]
    return "\n".join(lines)
