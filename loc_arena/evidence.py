"""An episode's records read strictly: bounded in size, and every line exactly the type it must be."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter

from loc_arena.logging_.events import Event

_EVENT: Final = TypeAdapter(Event)


class EvidenceError(ValueError):
    """A record file is too large, or one of its lines is not the record it must be."""


def _read_bounded_text(path: Path, max_bytes: int) -> str:
    with path.open("rb") as file:
        data = file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise EvidenceError(f"{path} is larger than the {max_bytes}-byte limit")
    return data.decode("utf-8")


def read_event_log(path: Path, max_bytes: int) -> list[Event]:
    """Every event of the log at ``path``, in file order (which is seq order)."""
    return _parse_lines(path, max_bytes, lambda line: _EVENT.validate_json(line, strict=True))


def _parse_lines[RecordT](path: Path, max_bytes: int, parse: Callable[[str], RecordT]) -> list[RecordT]:
    records: list[RecordT] = []
    for number, line in enumerate(_read_bounded_text(path, max_bytes).splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(parse(line))
        except ValueError as error:  # pydantic's ValidationError, or an event's own checks (__post_init__)
            raise EvidenceError(f"{path} line {number}: {error}") from error
    return records
