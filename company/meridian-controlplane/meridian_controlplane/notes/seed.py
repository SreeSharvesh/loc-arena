"""What the notes start with: the team's notes, written through a new store in the seed's order."""

from __future__ import annotations

import tempfile
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


def open_seeded_store(database: Path, seed: NoteSeed) -> NoteStore:
    """The store at ``database``, built from ``seed`` if there is none: a restarted service keeps its notes.

    A new store is built aside and moved into place whole, so a seed that fails leaves no store to reopen.
    Each seeded note is written as a caller's would be, within the same bounds.
    """
    if not database.exists():
        database.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=database.parent) as building:
            store = NoteStore(Path(building) / database.name)
            for note in seed.notes:
                store.write_note(note.key, note.text)
            store.close()
            (Path(building) / database.name).replace(database)
    return NoteStore(database)
