"""Event schema, canonicalization, fingerprints, and append-only writers.

Enforces fingerprinted, append-only logging: every event carries a sha256 content fingerprint, a monotonic
``seq``, an ``episode_id`` and an ``actor_uid``; the log is append-only, never rewrites a line, and
refuses an out-of-order or duplicate ``seq``. This is the single writer path so those semantics are
guaranteed in one place. A sealed log and a mirror log are the same
writer over different files; their divergence is a first-class signal (the sealed-vs-tamperable split).

Pure standard-library. Canonicalization gives a stable, whitespace-free byte form (sorted keys, compact
separators) so equal logical payloads fingerprint identically regardless of key order.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

EventKind = Literal[
    "action",
    "message",
    "inference_call",
    "job",
    "pr",
    "spawn",
    "close",
    "monitor_verdict",
    "shared_surface",
    "tick",
    "config",
    "monitor_parse_error",
    "monitor_error",
    "forge_boot",
    "commit",
    "review",
    "ticket",
    "wiki",
    "slack",
    "siem",
]

_EVENT_KINDS: frozenset[str] = frozenset(
    {
        "action",
        "message",
        "inference_call",
        "job",
        "pr",
        "spawn",
        "close",
        "monitor_verdict",
        "shared_surface",
        "tick",
        "config",
        "monitor_parse_error",
        "monitor_error",
        "forge_boot",
        "commit",
        "review",
        "ticket",
        "wiki",
        "slack",
        "siem",
    }
)


def canonicalize(payload: Any) -> bytes:
    """Serialize ``payload`` to a stable, whitespace-free UTF-8 byte string.

    Object keys are sorted, separators carry no whitespace, and non-ASCII is preserved. The result is
    order-independent (two dicts with the same items canonicalize identically) and free of incidental
    whitespace, so it is a sound input to a content fingerprint.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def fingerprint(payload: Any) -> str:
    """Return the sha256 hex digest of the canonicalized ``payload`` (fingerprinting)."""
    return hashlib.sha256(canonicalize(payload)).hexdigest()


@dataclass(frozen=True)
class Event:
    """One append-only, fingerprinted event.

    ``kind`` is one of ``EventKind``. ``payload`` holds the kind-specific body (for ``inference_call`` it
    holds the generate-call record fields). ``fp`` is the content fingerprint over every field except
    ``fp`` itself; it is filled in by ``AppendOnlyLog.append`` if left empty so callers do not have to
    compute it.
    """

    episode_id: str
    seq: int
    ts: float
    actor_uid: str
    actor_role: str
    kind: EventKind
    payload: dict[str, Any] = field(default_factory=dict)
    target_id: str | None = None
    turn: int = 0
    parent_task: str | None = None
    tool: str | None = None
    result: dict[str, Any] | None = None
    fp: str = ""

    def __post_init__(self) -> None:
        """Validate the kind, seq bound, and required identifiers."""
        if self.kind not in _EVENT_KINDS:
            raise ValueError(f"unknown event kind {self.kind!r}")
        if self.seq < 0:
            raise ValueError(f"seq must be >= 0, got {self.seq}")
        if not self.episode_id:
            raise ValueError("episode_id must be non-empty")
        if not self.actor_uid:
            raise ValueError("actor_uid must be non-empty")

    def content(self) -> dict[str, Any]:
        """The event as a dict for fingerprinting/serialization, excluding ``fp``."""
        d = asdict(self)
        d.pop("fp", None)
        return d

    def compute_fp(self) -> str:
        """The content fingerprint of this event (over every field except ``fp``)."""
        return fingerprint(self.content())

    def with_fp(self) -> Event:
        """Return a copy with ``fp`` populated from ``compute_fp`` (idempotent)."""
        if self.fp:
            return self
        return Event(**{**self.content(), "fp": self.compute_fp()})


class AppendOnlyLog:
    """A per-episode, append-only JSONL writer with a monotonic ``seq`` (fingerprinted, append-only).

    Bound to one ``episode_id`` and one file. ``append`` refuses an event from another episode, an
    out-of-order ``seq`` (``<=`` the last written), or a wrong episode; it never rewrites an existing line
    and flushes each line durably (``fsync``) so a crash cannot lose a committed event. Use one instance
    for the sealed file and another for the mirror file.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        episode_id: str,
        *,
        on_append: Callable[[Event], None] | None = None,
    ) -> None:
        """Bind to one file and episode, recovering the last seq if the file already exists.

        ``on_append`` is an OPTIONAL, strictly non-blocking publish hook: after each event is durably
        written it is handed to the subscriber inside a guarded ``try/except`` so a slow or raising
        subscriber can NEVER block, delay, or crash the writer (it stays off the critical path).
        """
        self._path = Path(path)
        self._episode_id = episode_id
        self._on_append = on_append
        self._last_seq = -1
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Recover the last seq if the file already exists (append across process restarts).
        if self._path.exists():
            for ev in read_events(self._path):
                if ev.episode_id != episode_id:
                    raise ValueError(
                        f"log file {self._path} holds episode {ev.episode_id!r}, not {episode_id!r}"
                    )
                self._last_seq = max(self._last_seq, ev.seq)

    @property
    def last_seq(self) -> int:
        """The highest ``seq`` written so far, or -1 if empty."""
        return self._last_seq

    def next_seq(self) -> int:
        """The seq the next appended event must carry."""
        return self._last_seq + 1

    def append(self, event: Event) -> Event:
        """Append ``event`` (filling its ``fp`` if empty); return the written event.

        Raises ``ValueError`` on an episode mismatch or a non-increasing ``seq``, leaving the file
        unchanged.
        """
        if event.episode_id != self._episode_id:
            raise ValueError(f"event episode {event.episode_id!r} != log episode {self._episode_id!r}")
        if event.seq <= self._last_seq:
            raise ValueError(
                f"seq {event.seq} is not strictly increasing (last was {self._last_seq}); "
                "the sealed log is append-only and rejects out-of-order or duplicate seq"
            )
        written = event.with_fp()
        line = json.dumps(written.content() | {"fp": written.fp}, sort_keys=True, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self._last_seq = written.seq
        # Non-blocking publish hook (off the critical path): a slow/raising subscriber can never block,
        # delay, or crash the durable writer.
        if self._on_append is not None:
            try:
                self._on_append(written)
            except Exception:  # noqa: BLE001 - the subscriber must never affect the writer
                pass
        return written


def read_events(path: str | os.PathLike[str]) -> Iterator[Event]:
    """Yield events from a JSONL log file in file order (which is ``seq`` order for a valid sealed log)."""
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            data: dict[str, Any] = json.loads(line)
            yield Event(
                episode_id=data["episode_id"],
                seq=data["seq"],
                ts=data["ts"],
                actor_uid=data["actor_uid"],
                actor_role=data["actor_role"],
                kind=data["kind"],
                payload=data.get("payload", {}),
                target_id=data.get("target_id"),
                turn=data.get("turn", 0),
                parent_task=data.get("parent_task"),
                tool=data.get("tool"),
                result=data.get("result"),
                fp=data.get("fp", ""),
            )
