"""The settings of the company's services, and the base every settings group shares.

Each service that keeps state has its database, or its directory, in its own container, under ``/var/lib``.
``loc_arena.settings`` puts these groups under ``LocArenaSettings`` beside the engine's own.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, PositiveInt


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
