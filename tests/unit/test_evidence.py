"""Reading an episode's records strictly: size-bounded, each line exactly its type."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from loc_arena.evidence import EvidenceError, read_event_log, read_model_call_log

GOLDEN_SEALED = (
    Path(__file__).parent / "golden" / "aurora-efficiency.deterministic" / "attack" / "events.sealed.jsonl"
)
LIMIT = 1_000_000


def _first_event_with(tmp_path: Path, **changes: object) -> Path:
    event = json.loads(GOLDEN_SEALED.read_text().splitlines()[0]) | changes
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(event) + "\n")
    return path


def test_a_log_larger_than_the_limit_is_refused() -> None:
    size = GOLDEN_SEALED.stat().st_size

    with pytest.raises(EvidenceError, match="limit"):
        read_event_log(GOLDEN_SEALED, size - 1)


def test_an_event_with_a_field_no_event_has_is_refused(tmp_path: Path) -> None:
    path = _first_event_with(tmp_path, planted="by agent code")

    with pytest.raises(EvidenceError, match="line 1"):
        read_event_log(path, LIMIT)


def test_an_event_whose_seq_is_a_string_is_refused(tmp_path: Path) -> None:
    path = _first_event_with(tmp_path, seq="0")

    with pytest.raises(EvidenceError, match="line 1"):
        read_event_log(path, LIMIT)


def test_a_missing_model_call_log_reads_as_no_calls(tmp_path: Path) -> None:
    calls = read_model_call_log(tmp_path / "model_calls.jsonl", LIMIT)

    assert calls == []
