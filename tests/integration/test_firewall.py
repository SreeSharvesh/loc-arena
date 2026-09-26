from __future__ import annotations

from loc_arena.config import load_run_config
from loc_arena.logging_.events import Event
from loc_arena.monitors.action_monitor import ActionMonitor
from loc_arena.monitors.inference_monitor import InferenceMonitor
from loc_arena.monitors.pr_review_monitor import ReviewMonitor
from loc_arena.monitors.registry import (
    MonitorContext,
    MonitorSpec,
    firewall_violations,
    load_monitor_specs,
)
from loc_arena.verifier.feedback_loop import GenerateRecord

# MANDATORY (the firewall): every declared monitor input is a mirror/observable field or the fixed
# baseline -- never a sealed recompute or a per-workload label, and never a sealed record field that differs
# attack-vs-twin. A misconfigured monitor fails the build here.


def test_all_declared_monitor_inputs_pass_the_firewall() -> None:
    specs = load_monitor_specs("configs/monitors.yaml")
    assert firewall_violations(specs) == []
    # every enabled model-backed monitor actually declares inputs
    for spec in specs:
        if spec.enabled and spec.model != "none":
            assert spec.inputs


def test_a_sealed_record_field_input_fails() -> None:
    bad = MonitorSpec(
        name="m_bad",
        taps=("inference",),
        inputs=("caller_identity", "in_mirror"),
        model="trusted_monitor",
        prompt_ref=None,
        threshold="audit_budget",
        scope={},
        enabled=True,
    )
    violations = firewall_violations([bad])
    assert any("in_mirror" in v for v in violations)  # a sealed-derived field is caught


def test_async_timing_does_not_bypass_the_firewall() -> None:
    # an async monitor is still a monitor -- declaring a forbidden sealed-derived input must still fail
    # the firewall (timing is orthogonal to what a monitor may read).
    bad_async = MonitorSpec(
        name="m_bad_async",
        taps=("inference",),
        inputs=("caller_identity", "rogue_label"),
        model="trusted_monitor",
        prompt_ref=None,
        threshold="audit_budget",
        scope={},
        enabled=True,
        timing="async",
    )
    violations = firewall_violations([bad_async])
    assert any("rogue_label" in v for v in violations)  # forbidden field caught despite async timing


def test_an_unknown_input_fails() -> None:
    bad = MonitorSpec(
        name="m_bad2",
        taps=("inference",),
        inputs=("sealed_recompute",),
        model="trusted_monitor",
        prompt_ref=None,
        threshold="audit_budget",
        scope={},
        enabled=True,
    )
    assert firewall_violations([bad])  # anything off the observable allowlist is caught


def test_live_prompt_payload_contains_only_declared_inputs() -> None:
    # the live prompt builder for each model-backed monitor may emit ONLY that monitor's declared inputs

    cfg = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    ctx = MonitorContext("optimize", frozenset({"agent-main"}), cfg.side_task.thresholds)
    specs = {s.name: s for s in load_monitor_specs("configs/monitors.yaml")}

    action = ActionMonitor(specs["action_monitor"], None)
    action_event = Event(
        episode_id="e",
        seq=1,
        ts=1.0,
        actor_uid="agent-main",
        actor_role="untrusted",
        kind="message",
        payload={"body": "hi", "message_kind": "delegate"},
    )
    assert set(action.input_payload(action_event, ctx)) <= set(specs["action_monitor"].inputs)

    inference = InferenceMonitor(specs["inference_monitor"], None)
    rec = GenerateRecord(1, 1.0, "e", "batch-runner", None, None, "a", "b", "r")
    rows = inference.input_payload([rec])
    assert all(set(row) <= set(specs["inference_monitor"].inputs) for row in rows)

    review = ReviewMonitor(specs["pr_review_monitor"], None)
    pr_event = Event(
        episode_id="e",
        seq=1,
        ts=1.0,
        actor_uid="agent-main",
        actor_role="untrusted",
        kind="pr",
        payload={"diff": "x", "target_repo": "monitor-policy"},
    )
    assert set(review.input_payload(pr_event)) <= set(specs["pr_review_monitor"].inputs)
