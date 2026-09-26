from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.logging_.events import AppendOnlyLog, Event, read_events

EP = "ep-test"


def _ev(seq: int, ts: float = 0.0, kind: str = "action") -> Event:
    return Event(episode_id=EP, seq=seq, ts=ts, actor_uid="agent-main", actor_role="untrusted", kind=kind)  # ty: ignore[invalid-argument-type]


def test_accepts_increasing_seq_and_reads_in_order(tmp_path: Path) -> None:
    log = AppendOnlyLog(tmp_path / "sealed.jsonl", EP)
    for i in range(5):
        log.append(_ev(i, ts=float(i)))
    seqs = [e.seq for e in read_events(tmp_path / "sealed.jsonl")]
    assert seqs == [0, 1, 2, 3, 4]
    assert log.last_seq == 4


def test_accepts_increasing_but_non_contiguous_seq(tmp_path: Path) -> None:
    log = AppendOnlyLog(tmp_path / "s.jsonl", EP)
    log.append(_ev(0))
    log.append(_ev(5))  # strictly increasing, allowed
    assert [e.seq for e in read_events(tmp_path / "s.jsonl")] == [0, 5]


def test_rejects_duplicate_seq_and_does_not_mutate(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    log = AppendOnlyLog(path, EP)
    log.append(_ev(0))
    log.append(_ev(1))
    before = path.read_text()
    with pytest.raises(ValueError):
        log.append(_ev(1))
    assert path.read_text() == before  # rejected append left the file byte-for-byte unchanged


def test_rejects_out_of_order_seq(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    log = AppendOnlyLog(path, EP)
    log.append(_ev(3))
    before = path.read_text()
    with pytest.raises(ValueError):
        log.append(_ev(2))
    assert path.read_text() == before


def test_rejects_foreign_episode(tmp_path: Path) -> None:
    log = AppendOnlyLog(tmp_path / "s.jsonl", EP)
    other = Event(episode_id="other", seq=0, ts=0.0, actor_uid="a", actor_role="untrusted", kind="action")
    with pytest.raises(ValueError):
        log.append(other)


def test_fingerprint_is_written_and_stable(tmp_path: Path) -> None:
    log = AppendOnlyLog(tmp_path / "s.jsonl", EP)
    written = log.append(_ev(0))
    assert written.fp and len(written.fp) == 64
    (readback,) = list(read_events(tmp_path / "s.jsonl"))
    assert readback.fp == written.fp == readback.compute_fp()


def test_reopen_recovers_last_seq(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    AppendOnlyLog(path, EP).append(_ev(0))
    log2 = AppendOnlyLog(path, EP)
    assert log2.last_seq == 0
    log2.append(_ev(1))
    assert [e.seq for e in read_events(path)] == [0, 1]
