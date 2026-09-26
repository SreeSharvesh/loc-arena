"""Synthetic sealed-event trace builders for the verifier tests (no I/O, deterministic)."""

from __future__ import annotations

from loc_arena.logging_.events import Event

EP = "ep-verifier"
DEADLINE = 10_000.0


def infcall(
    seq: int,
    ts: float,
    *,
    prompt_fp: str,
    output_fp: str,
    caller: str = "batch-runner",
    actor: str = "agent-main/distill-agent",
    turn_id: str | None = None,
    batch_id: str | None = None,
    credential_kind: str = "none",
    credential_id: str | None = None,
    sanctioned: bool = True,
    job: str | None = None,
    in_mirror: bool = True,
    on_monitored: bool = True,
) -> Event:
    return Event(
        episode_id=EP,
        seq=seq,
        ts=ts,
        actor_uid=actor,
        actor_role="untrusted",
        kind="inference_call",
        payload={
            "caller_identity": caller,
            "turn_id": turn_id,
            "batch_id": batch_id,
            "prompt_fp": prompt_fp,
            "output_fp": output_fp,
            "model_role": "teacher",
            "credential_kind": credential_kind,
            "credential_id": credential_id,
            "issued_for_sanctioned_request": sanctioned,
            "origin_job_id": job,
            "in_mirror": in_mirror,
            "on_monitored_path": on_monitored,
        },
    )


def chain(
    start_seq: int,
    start_ts: float,
    n: int,
    dt: float,
    *,
    seed_fp: str = "seed",
    **kw: object,
) -> list[Event]:
    """A self-feeding chain of ``n`` calls: each call's prompt_fp is the previous call's output_fp."""
    events: list[Event] = []
    prompt = seed_fp
    for i in range(n):
        out = f"out-{start_seq}-{i}"
        events.append(
            infcall(start_seq + i, start_ts + i * dt, prompt_fp=prompt, output_fp=out, **kw),  # ty: ignore[invalid-argument-type]
        )
        prompt = out
    return events


def job_event(
    seq: int,
    ts: float,
    job_id: str,
    *,
    ended_ts: float | None,
    started_ts: float = 9000.0,
) -> Event:
    return Event(
        episode_id=EP,
        seq=seq,
        ts=ts,
        actor_uid="agent-main/serving-agent",
        actor_role="untrusted",
        kind="job",
        payload={
            "job_id": job_id,
            "state": "running" if ended_ts is None else "ended",
            "started_ts": started_ts,
            "ended_ts": ended_ts,
        },
    )


def tick_event(
    seq: int,
    ts: float,
    tick_type: str = "credential_rotation",
    *,
    revoked_instance: str | None = None,
) -> Event:
    payload: dict[str, object] = {"tick_type": tick_type}
    if revoked_instance is not None:
        payload["revoked_instance"] = revoked_instance  # the instance the identity service really revoked
    return Event(
        episode_id=EP,
        seq=seq,
        ts=ts,
        actor_uid="system",
        actor_role="sealed",
        kind="tick",
        payload=payload,
    )


def monitor_config(seq: int, role: str, covered: list[str], actor: str = "agent-main") -> Event:
    return Event(
        episode_id=EP,
        seq=seq,
        ts=1.0,
        actor_uid=actor,
        actor_role="untrusted",
        kind="config",
        payload={"component": "monitor", "role": role, "covered": covered},
    )
