"""An episode's records read strictly: bounded in size, and every line exactly the type it must be.

The records come from other processes: the host reads the sealed and mirror logs it copied out of the stack
and the runner's export, and the runner reads the mirror the edge wrote. A file larger than
``settings.docker.evidence_max_bytes`` is refused before it is read in full, and each line must validate as
its exact type in pydantic's strict mode: an unknown field or a wrong type is an error, never dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter, ValidationError

from loc_arena.logging_.events import Event
from loc_arena.stack.contracts import ModelCallRecord, RunnerEpisodeExport

_EVENT: Final = TypeAdapter(Event)


class EvidenceError(ValueError):
    """A record file is too large, or one of its lines is not the record it must be."""


def read_bounded_text(path: Path, max_bytes: int) -> str:
    """The text of ``path``; ``EvidenceError`` past ``max_bytes`` (no more than that is ever read)."""
    with path.open("rb") as file:
        data = file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise EvidenceError(f"{path} is larger than the {max_bytes}-byte limit")
    return data.decode("utf-8")


def read_event_log(path: Path, max_bytes: int) -> list[Event]:
    """Every event of the log at ``path``, in file order (which is seq order)."""
    return _parse_lines(path, max_bytes, lambda line: _EVENT.validate_json(line, strict=True))


def read_model_call_log(path: Path, max_bytes: int) -> list[ModelCallRecord]:
    """Every provider call of the sealed model-call log at ``path``; none when the core made no call."""
    if not path.exists():
        return []
    return _parse_lines(path, max_bytes, lambda line: ModelCallRecord.model_validate_json(line, strict=True))


def read_runner_export(path: Path, max_bytes: int) -> RunnerEpisodeExport:
    """The runner's export of one episode."""
    try:
        return RunnerEpisodeExport.model_validate_json(read_bounded_text(path, max_bytes))
    except ValidationError as error:
        raise EvidenceError(f"{path} is not a runner episode export: {error}") from error


def _parse_lines[RecordT](path: Path, max_bytes: int, parse: Callable[[str], RecordT]) -> list[RecordT]:
    records: list[RecordT] = []
    for number, line in enumerate(read_bounded_text(path, max_bytes).splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(parse(line))
        except ValueError as error:  # pydantic's ValidationError, or an event's own checks (__post_init__)
            raise EvidenceError(f"{path} line {number}: {error}") from error
    return records
