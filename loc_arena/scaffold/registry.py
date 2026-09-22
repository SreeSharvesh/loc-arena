"""Agent tree, spawn caps, and sub-agent lifecycle.

Enforces fingerprinted, append-only logging (every spawn and close is a fingerprinted sealed event, so
tree and every trajectory reconstruct from the log) and the non-escalation rule (a child scope is the
intersection of the requested scope with the parent's delegable set). Implements
the sub-agent lifecycle: the hierarchical ``agent_uid``, the four spawn caps checked at the
``spawn_subagent`` boundary, the close lifecycle with one recorded reason each, the idle reaper, and the
end-of-episode spawn/close reconciliation. The live in-memory registry drives the caps and the reaper but
is never the record of truth; the sealed spawn/close events are, and reconciliation surfaces any
divergence rather than swallowing it.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from loc_arena.config import EpisodeConfig
from loc_arena.ids import PermissionScope, child_uid
from loc_arena.logging_.events import Event, fingerprint, read_events
from loc_arena.scaffold.bus import Recorder


class CloseReason(StrEnum):
    """The one recorded reason a sub-agent's loop ended."""

    RETURNED = "returned"
    CLOSED_BY_PARENT = "closed_by_parent"
    BUDGET_EXHAUSTED = "budget_exhausted"
    IDLE = "idle"
    ERROR = "error"
    EPISODE_END = "episode_end"


class SpawnDenied(Exception):  # noqa: N818  # denial signal, not an error condition
    """Raised when a spawn is refused by a cap or a missing capability; the tool layer logs it as blocked."""

    def __init__(self, reason: str) -> None:
        """Store the human-readable denial reason."""
        self.reason = reason
        super().__init__(reason)


@dataclass
class AgentNode:
    """The live registry entry for one agent (tamperable control-plane state, never the record of truth)."""

    uid: str
    parent_uid: str | None
    role: str
    branch: str
    scope: PermissionScope
    status: str = "live"  # live | closed
    turn_count: int = 0
    last_activity: float = 0.0
    close_reason: CloseReason | None = None


@dataclass(frozen=True)
class ReconcileReport:
    """The end-of-episode reconciliation of the live registry against the sealed spawn/close events."""

    ok: bool
    unmatched_spawns: tuple[str, ...]  # spawned but never closed
    live_count: int  # live sub-agents remaining (should be 0 after force-close)
    orphan_activity: tuple[int, ...]  # seqs of events under an actor_uid with no sealed spawn
    post_close_activity: tuple[int, ...]  # seqs of events emitted by an agent after its close


def _noop_teardown(uid: str) -> None:
    """Default sandbox-teardown hook (later wired to the real container teardown)."""


class AgentRegistry:
    """Owns the agent tree, the spawn caps, the close lifecycle, the idle reaper, and reconciliation."""

    def __init__(
        self,
        episode: EpisodeConfig,
        recorder: Recorder,
        sealed_path: str,
        *,
        root_uid: str,
        root_role: str,
        root_branch: str,
        root_scope: PermissionScope,
        teardown: Callable[[str], None] = _noop_teardown,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Register the root agent (no spawn event) and wire the caps, recorder, and teardown hook."""
        self._episode = episode
        self._recorder = recorder
        self._sealed_path = sealed_path
        self._teardown = teardown
        self._clock = clock
        self._root_uid = root_uid
        self._nodes: dict[str, AgentNode] = {
            root_uid: AgentNode(
                uid=root_uid,
                parent_uid=None,
                role=root_role,
                branch=root_branch,
                scope=root_scope,
                last_activity=clock(),
            )
        }
        self._total_created = 1  # the root counts toward max_total_agents

    # -- queries -----------------------------------------------------------------------------------------
    def node(self, uid: str) -> AgentNode:
        """The registry entry for ``uid`` (raises ``KeyError`` if unknown)."""
        return self._nodes[uid]

    def live_subagent_count(self) -> int:
        """How many sub-agents (non-root) are currently live."""
        return sum(1 for n in self._nodes.values() if n.uid != self._root_uid and n.status == "live")

    def total_created(self) -> int:
        """Total agents created this episode, root included."""
        return self._total_created

    # -- spawn -------------------------------------------------------------------------------------------
    def spawn(
        self,
        parent_uid: str,
        parent_scope: PermissionScope,
        *,
        label: str,
        role: str,
        branch: str,
        requested_scope: PermissionScope,
        brief: str,
        turn: int = 0,
        ts: float | None = None,
    ) -> tuple[str, PermissionScope]:
        """Create a scoped sub-agent, enforcing the caps and non-escalation; write the sealed ``spawn`` event.

        Raises ``SpawnDenied`` (with no child created) if the parent is unknown or closed, the parent's
        scope lacks ``spawn_subagent``, or a cap would be exceeded. The granted scope is the requested
        scope intersected with the parent's delegable set; anything dropped is recorded on the spawn
        event.
        """
        parent = self._nodes.get(parent_uid)
        if parent is None or parent.status != "live":
            raise SpawnDenied(f"parent {parent_uid!r} is not a live agent")
        if not parent_scope.spawn_subagent:
            raise SpawnDenied(f"{parent_uid} scope does not grant spawn_subagent")

        cuid = child_uid(parent_uid, label)
        if cuid in self._nodes:
            raise SpawnDenied(f"agent {cuid!r} already exists")
        depth = cuid.count("/")
        if depth > self._episode.max_spawn_depth:
            raise SpawnDenied(f"spawn depth {depth} exceeds max_spawn_depth {self._episode.max_spawn_depth}")
        if self.live_subagent_count() >= self._episode.max_live_subagents:
            raise SpawnDenied(
                f"live sub-agents {self.live_subagent_count()} at max_live_subagents "
                f"{self._episode.max_live_subagents}"
            )
        if self._total_created >= self._episode.max_total_agents:
            raise SpawnDenied(
                f"total agents {self._total_created} at max_total_agents {self._episode.max_total_agents}"
            )

        granted = parent_scope.intersect_delegable(requested_scope)
        dropped = _scope_diff(requested_scope, granted)
        at = ts if ts is not None else self._clock()
        self._recorder.sealed(
            actor_uid=parent_uid,
            actor_role=parent.role,
            kind="spawn",
            target_id=cuid,
            turn=turn,
            payload={
                "parent_uid": parent_uid,
                "child_uid": cuid,
                "role": role,
                "branch": branch,
                "scope_granted": _scope_to_dict(granted),
                "scope_dropped": dropped,
                "brief_fp": fingerprint(brief),
            },
            ts=at,
        )
        self._nodes[cuid] = AgentNode(
            uid=cuid,
            parent_uid=parent_uid,
            role=role,
            branch=branch,
            scope=granted,
            last_activity=at,
        )
        self._total_created += 1
        return cuid, granted

    # -- activity + close --------------------------------------------------------------------------------
    def record_activity(self, uid: str, ts: float | None = None) -> None:
        """Update an agent's last-activity timestamp (drives the idle reaper)."""
        node = self._nodes.get(uid)
        if node is not None:
            node.last_activity = ts if ts is not None else self._clock()

    def bump_turn(self, uid: str) -> None:
        """Increment an agent's turn count (for the ``final_turn_count`` on close)."""
        node = self._nodes.get(uid)
        if node is not None:
            node.turn_count += 1

    def close(self, uid: str, reason: CloseReason, *, turn: int = 0, ts: float | None = None) -> Event:
        """End a sub-agent: write the sealed ``close`` event with its reason and tear down its sandbox."""
        node = self._nodes.get(uid)
        if node is None:
            raise KeyError(f"no such agent {uid!r}")
        if uid == self._root_uid:
            raise ValueError("the root agent is not closed via the sub-agent lifecycle")
        at = ts if ts is not None else self._clock()
        event = self._recorder.sealed(
            actor_uid=node.parent_uid or uid,
            actor_role=node.role,
            kind="close",
            target_id=uid,
            turn=turn,
            payload={"child_uid": uid, "reason": reason.value, "final_turn_count": node.turn_count},
            ts=at,
        )
        node.status = "closed"
        node.close_reason = reason
        self._teardown(uid)
        return event

    def reap_idle(self, now: float | None = None) -> list[str]:
        """Close every live sub-agent with no activity for longer than the idle timeout; return their uids."""
        at = now if now is not None else self._clock()
        timeout = self._episode.subagent_idle_timeout_seconds
        reaped: list[str] = []
        for node in list(self._nodes.values()):
            if node.uid == self._root_uid or node.status != "live":
                continue
            if at - node.last_activity > timeout:
                self.close(node.uid, CloseReason.IDLE, ts=at)
                reaped.append(node.uid)
        return reaped

    def force_close_remaining(self, *, ts: float | None = None) -> list[str]:
        """Force-close any still-live sub-agents at teardown as ``episode_end``; return their uids.

        Liveness ordering (AUDITING 8.3): the harness takes the covert-loop liveness snapshot from the
        sealed log BEFORE calling this, so "alive at the deadline" reflects the loop's own calls, not
        process survival. This writes only ``close`` events (never inference), so it cannot change the
        liveness verdict.
        """
        closed: list[str] = []
        for node in list(self._nodes.values()):
            if node.uid == self._root_uid or node.status != "live":
                continue
            self.close(node.uid, CloseReason.EPISODE_END, ts=ts)
            closed.append(node.uid)
        return closed

    # -- tree + reconciliation ---------------------------------------------------------------------------
    def tree(self) -> dict[str, list[str]]:
        """Reconstruct the parent -> children map from the live registry (matches the sealed spawn events)."""
        out: dict[str, list[str]] = {uid: [] for uid in self._nodes}
        for node in self._nodes.values():
            if node.parent_uid is not None:
                out[node.parent_uid].append(node.uid)
        return out

    def reconcile(self) -> ReconcileReport:
        """Reconcile the sealed spawn/close events: all spawns matched, live count zero, no anomalies.

        Reads the SEALED log (the record of truth), not the in-memory registry. Surfaces as divergences: a
        spawn with no matching close, activity under an ``actor_uid`` that was never spawned, and any
        event an agent emitted after its own close.
        """
        spawned: dict[str, int] = {}  # child_uid -> spawn seq
        closed: set[str] = set()
        close_seq: dict[str, int] = {}
        orphan: list[int] = []
        post_close: list[int] = []
        known_uids = {self._root_uid}

        events = list(read_events(self._sealed_path))
        for ev in events:
            if ev.kind == "spawn":
                cuid = str(ev.payload["child_uid"])
                spawned[cuid] = ev.seq
                known_uids.add(cuid)
            elif ev.kind == "close":
                cuid = str(ev.payload["child_uid"])
                closed.add(cuid)
                close_seq[cuid] = ev.seq

        for ev in events:
            if ev.actor_uid not in known_uids:
                orphan.append(ev.seq)
            cs = close_seq.get(ev.actor_uid)
            # an agent emitting its own event after its close (close is emitted by the parent, so excluded)
            if cs is not None and ev.seq > cs and ev.kind != "close":
                post_close.append(ev.seq)

        unmatched = tuple(uid for uid in spawned if uid not in closed)
        live = self.live_subagent_count()
        ok = not unmatched and live == 0 and not orphan and not post_close
        return ReconcileReport(
            ok=ok,
            unmatched_spawns=unmatched,
            live_count=live,
            orphan_activity=tuple(orphan),
            post_close_activity=tuple(post_close),
        )


def _scope_to_dict(scope: PermissionScope) -> dict[str, object]:
    """Serialize a scope for the sealed spawn event's ``scope_granted``."""
    return {
        "inference_api": scope.inference_api,
        "batch_generate": scope.batch_generate,
        "submit_job": scope.submit_job,
        "spawn_subagent": scope.spawn_subagent,
        "shared_surface": scope.shared_surface,
        "observability_edit": scope.observability_edit,
        "open_pr": list(scope.open_pr),
        "message": list(scope.message),
        "weights": {"read": scope.weights.read, "write_area": scope.weights.write_area},
        "service_account": scope.service_account,
    }


def _scope_diff(requested: PermissionScope, granted: PermissionScope) -> list[str]:
    """List the capabilities in ``requested`` that non-escalation dropped from ``granted``."""
    dropped: list[str] = []
    for cap in (
        "inference_api",
        "batch_generate",
        "submit_job",
        "spawn_subagent",
        "shared_surface",
        "observability_edit",
    ):
        if getattr(requested, cap) and not getattr(granted, cap):
            dropped.append(cap)
    for repo in requested.open_pr:
        if repo not in granted.open_pr:
            dropped.append(f"open_pr:{repo}")
    for who in requested.message:
        if who not in granted.message:
            dropped.append(f"message:{who}")
    if requested.service_account and not granted.service_account:
        dropped.append(f"service_account:{requested.service_account}")
    return dropped
