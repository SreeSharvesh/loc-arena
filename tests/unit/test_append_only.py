from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from loc_arena.logging_.events import AppendOnlyLog, Event, EventDraft, read_events

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


LAST_APPENDED_SEQ = 4
CONCURRENT_RECORDS = 200
CONCURRENT_WRITERS = 8


def _draft(ts: float) -> EventDraft:
    return EventDraft(
        ts=ts,
        actor_uid="agent-main",
        actor_role="untrusted",
        kind="action",
        payload={"ts": ts},
    )


def _record_concurrently(log: AppendOnlyLog) -> list[Event]:
    """Record ``CONCURRENT_RECORDS`` drafts from a pool of writer threads; the events, in draft order."""
    with ThreadPoolExecutor(max_workers=CONCURRENT_WRITERS) as pool:
        return list(pool.map(lambda index: log.record(_draft(float(index))), range(CONCURRENT_RECORDS)))


def test_a_recorded_draft_becomes_the_event_after_the_last_append(tmp_path: Path) -> None:
    log = AppendOnlyLog(tmp_path / "s.jsonl", EP)
    log.append(_ev(LAST_APPENDED_SEQ))
    draft = _draft(1.0)

    written = log.record(draft)

    assert written == Event(episode_id=EP, seq=LAST_APPENDED_SEQ + 1, **vars(draft)).with_fp()


def test_concurrent_records_each_get_a_distinct_seq(tmp_path: Path) -> None:
    log = AppendOnlyLog(tmp_path / "s.jsonl", EP)

    written = _record_concurrently(log)

    assert sorted(event.seq for event in written) == list(range(CONCURRENT_RECORDS))


def test_concurrent_records_are_written_to_the_file_in_seq_order(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    log = AppendOnlyLog(path, EP)

    _record_concurrently(log)

    assert [event.seq for event in read_events(path)] == list(range(CONCURRENT_RECORDS))
