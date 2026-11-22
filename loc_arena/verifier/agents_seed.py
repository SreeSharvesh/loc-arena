"""The agents service's seed as the verifier reads it: the company's own agents and their schedules.

The scenario names each seed file, relative to the repository, and the agents a run stands up are told apart
from these. The host that grades does not import the company's packages, so a file is read into models of the
fields the verifier needs, and a file of any other shape fails the grade rather than being miscounted.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, JsonValue, TypeAdapter

from loc_arena.config import RunConfig
from loc_arena.iam_seed import REPOSITORY
from loc_arena.task import resolve_scenario

AGENTS_SEED_KEY, SCHEDULES_SEED_KEY = "agents_seed", "agents_schedules_seed"  # keys of scenario.yaml


class SeededAgent(BaseModel):
    """A company agent the agents service starts with."""

    name: str


class SeededSchedule(BaseModel):
    """A schedule the agents service starts with, and who created it."""

    created_by: str
    schedule: dict[str, JsonValue]


def find_agents_seed(config: RunConfig, key: str) -> Path:
    """The seed file that the scenario names under ``key``."""
    return REPOSITORY / resolve_scenario(config).meta[key]


def read_seeded_agents(path: Path) -> list[SeededAgent]:
    """The agents in the agents service's seed file at ``path``."""
    return TypeAdapter(list[SeededAgent]).validate_python(yaml.safe_load(path.read_text(encoding="utf-8")))


def read_seeded_schedules(path: Path) -> list[SeededSchedule]:
    """The schedules in the agents service's schedules seed file at ``path``."""
    return TypeAdapter(list[SeededSchedule]).validate_python(yaml.safe_load(path.read_text(encoding="utf-8")))
