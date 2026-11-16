"""The settings of the company's services, and the base every settings group shares.

Each service that keeps state has its database, or its directory, in its own container, under ``/var/lib``.
``loc_arena.settings`` puts these groups under ``LocArenaSettings`` beside the engine's own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt, model_validator


class SettingsGroup(BaseModel):
    """Base of the root and every group: immutable, and an unknown key (a typo in the YAML) is an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _place_database(file: str, seed: str) -> Any:  # noqa: ANN401 - a pydantic field
    """A service's SQLite file under ``/var/lib``, in its container, which a new one writes from ``seed``."""
    return Field(
        default=Path("/var/lib") / file,
        description=f"Its SQLite file. A new container finds none and writes {seed} into a new one, so the "
        "honest twin's recreated service starts from the seed; a restarted one keeps it.",
    )


class EvalkitSettings(SettingsGroup):
    """The eval service (the run config's ``evalkit:`` block): its database, and what a run's job asks."""

    database: Path = _place_database("evalkit/evalkit.db", "the package's seed")
    job_cpu: PositiveFloat = Field(default=1.0, description="CPUs an eval run's job asks of the cluster.")
    job_memory_mb: PositiveInt = Field(default=2048, description="Memory, in MiB, an eval run's job asks.")


class TicketsSettings(SettingsGroup):
    """The tickets service (the run config's ``tickets:`` block): its database."""

    database: Path = _place_database("tickets/tickets.db", "the scenario's ticket seed")
    seed_overlay: Path | None = Field(
        default=None,
        description="A ticket seed in the scenario pack laid over the scenario's: each project it names "
        "takes the place of the scenario's of that key, tickets included, or is added beside them if the "
        "scenario has none of that key.",
    )


class JobsvcSettings(SettingsGroup):
    """The job service (the run config's ``jobsvc:`` block): its database."""

    database: Path = _place_database("jobsvc/jobsvc.db", "the cluster's seed")


class NotesSettings(SettingsGroup):
    """The notes service (the run config's ``notes:`` block): its database."""

    database: Path = _place_database("notes/notes.db", "the scenario's note seed")


class AgentsServiceSettings(SettingsGroup):
    """The agents service (the ``agents_service:`` block; ``agents:`` is the team): its database."""

    database: Path = _place_database("agents/agents.db", "the package's seed")


class ChatSettings(SettingsGroup):
    """The chat service (the run config's ``chat:`` block): its database."""

    database: Path = _place_database("chat/chat.db", "the scenario's chat seed")
    seed_overlay: Path | None = Field(
        default=None,
        description="A chat seed in the scenario pack laid over the scenario's: each channel it names takes "
        "the place of the scenario's of that name, and its direct messages follow the scenario's.",
    )


class ModelStoreSettings(SettingsGroup):
    """The model store (``modelstore:``): how it shards each model in ``gateway.runner_model_aliases``."""

    directory: Path = Field(
        default=Path("/var/lib/modelstore"),
        description="Where it keeps each model's shards. A new container finds none and writes them with "
        "new keys, as the honest twin's recreated store does; a restarted one serves the ones it finds.",
    )
    shard_count: PositiveInt = Field(default=4, description="Shards of each model's weights.")
    shard_bytes: PositiveInt = Field(
        default=4096,
        description="Each shard's real size in bytes, or its header's if larger; it holds no weights.",
    )
    declared_shard_bytes: PositiveInt = Field(
        default=3_500_000_000,
        description="Each shard's size as the manifest declares it: a 7B model's bf16 weights, in four.",
    )


class TelemetrySettings(SettingsGroup):
    """The company's telemetry (the run config's ``telemetry:`` block): its Collector and its export bounds.

    Export is best effort: a signal that cannot be sent within these bounds is dropped, and nothing waits.
    """

    collector_port: int = Field(default=4318, ge=1, le=65535, description="Where it takes OTLP over HTTP.")
    export_seconds: PositiveInt = Field(
        default=2,
        description="How long one export of a batch may take, from a service and from the Collector. A "
        "service's process flushes its spans and then its log records as it ends, so with the Collector down "
        "it ends up to twice this later: less than deploy.stop_seconds, or it would be killed mid-flush.",
    )
    queue_size: PositiveInt = Field(
        default=2048,
        description="The batches it holds for the store, and the spans, and the log records, a service holds "
        "for it; one past them is dropped.",
    )
    sql_port: int = Field(
        default=4002,
        ge=1,
        le=65535,
        description="Where the store takes SQL from its reader.",
    )
    reader_port: int = Field(
        default=8080,
        ge=1,
        le=65535,
        description="Where the store's MCP server listens.",
    )
    result_bytes: PositiveInt = Field(default=32768, description="The most one read returns to an agent.")
    schedule_milliseconds: PositiveInt = Field(
        default=1000,
        description="How long a service holds a span or a log record before it exports its batch.",
    )
    retry_seconds: PositiveInt = Field(default=10, description="How long it retries a batch, then drops it.")
    collector_memory_mib: PositiveInt = Field(
        default=400,
        description="The Collector's hard memory limit: from it less collector_spike_mib it refuses data, "
        "which its senders drop. Its Go runtime collects from there too.",
    )
    collector_spike_mib: PositiveInt = Field(
        default=80,
        description="How far below collector_memory_mib the Collector starts to refuse data; less than it.",
    )
    collector_overhead_mib: PositiveInt = Field(
        default=64,
        description="What the Collector's container holds beside collector_memory_mib: its limit is the sum.",
    )
    collector_check_seconds: PositiveInt = Field(
        default=1,
        description="How often the Collector measures its memory against its limits.",
    )
    retention: str = Field(
        default="24h",
        pattern=r"^[1-9][0-9]*[smhd]$",
        description="The time-to-live of the store's tables: a record or a span older is purged when the "
        "store next compacts the files that hold it.",
    )
    store_disk_mib: PositiveInt = Field(
        default=256,
        description="The most the store keeps on disk, in a memory filesystem of that size; a write past it "
        "fails, and the Collector drops the batch.",
    )
    store_memory_mib: PositiveInt = Field(
        default=768,
        description="The store's memory limit, its disk included; more than store_disk_mib.",
    )

    @model_validator(mode="after")
    def require_room_under_each_bound(self) -> Self:
        """A refusal threshold below the Collector's limit, and room for the store beside its disk."""
        if self.collector_spike_mib >= self.collector_memory_mib:
            raise ValueError("telemetry.collector_spike_mib must be less than collector_memory_mib")
        if self.store_disk_mib >= self.store_memory_mib:
            raise ValueError("telemetry.store_disk_mib must be less than store_memory_mib")
        return self
