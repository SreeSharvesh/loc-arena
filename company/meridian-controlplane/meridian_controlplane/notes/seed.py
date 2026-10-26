"""What the notes start with: the team's notes, written through the store in the order the seed lists them."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field

from meridian_controlplane.notes.store import KEY_PATTERN, NoteStore


class _SeedModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SeedNote(_SeedModel):
    """One note: its key and its text."""

    key: Annotated[str, Field(pattern=KEY_PATTERN)]
    text: str


class NoteSeed(_SeedModel):
    """The notes a store starts with."""

    notes: list[SeedNote]


def load_note_seed(path: Path) -> NoteSeed:
    """The seed in the YAML file at ``path``."""
    return NoteSeed.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def open_seeded_store(seed: NoteSeed) -> NoteStore:
    """A store holding the seed's notes, each written as a caller's would be, within the same bounds."""
    store = NoteStore()
    for note in seed.notes:
        store.write_note(note.key, note.text)
    return store
