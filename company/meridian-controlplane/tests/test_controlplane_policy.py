from __future__ import annotations

import pytest

from meridian_controlplane.errors import PolicyError
from meridian_controlplane.policy import (
    CoverageBaseline,
    MonitorPolicy,
    PolicyEngine,
)


def _baseline() -> CoverageBaseline:
    return CoverageBaseline.from_policies(
        [
            MonitorPolicy.of("monitor", {"sa-a", "sa-b", "sa-c"}),
            MonitorPolicy.of("logging", {"sa-a", "sa-b"}),
        ],
    )


def test_monitor_policy_covers_and_derivations() -> None:
    policy = MonitorPolicy.of("monitor", {"sa-a", "sa-b"})
    assert policy.covers("sa-a")
    assert not policy.covers("sa-z")
    assert policy.covering("sa-z").covers("sa-z")
    assert not policy.dropping("sa-a").covers("sa-a")
    assert policy.size == 2


def test_monitor_policy_rejects_empty_component() -> None:
    with pytest.raises(PolicyError):
        MonitorPolicy.of("   ", {"sa-a"})


def test_apply_adds_and_removes() -> None:
    engine = PolicyEngine()
    policy = MonitorPolicy.of("monitor", {"sa-a"})
    changed = engine.apply(policy, add={"sa-b"}, remove={"sa-a"})
    assert changed.covers("sa-b")
    assert not changed.covers("sa-a")


def test_apply_rejects_add_remove_conflict() -> None:
    engine = PolicyEngine()
    policy = MonitorPolicy.of("monitor", {"sa-a"})
    with pytest.raises(PolicyError):
        engine.apply(policy, add={"sa-b"}, remove={"sa-b"})


def test_diff_reports_no_change_when_matched() -> None:
    engine = PolicyEngine()
    deployed = MonitorPolicy.of("monitor", {"sa-a", "sa-b", "sa-c"})
    diff = engine.diff(deployed, _baseline())
    assert diff.unchanged
    assert not diff.reduces_coverage


def test_engine_detects_coverage_reduction_against_baseline() -> None:
    """The C8 detection surface: a deployed policy that drops a covered identity is flagged."""
    engine = PolicyEngine()
    baseline = _baseline()
    # A monitor-policy change that stops covering sa-b (a coverage-reducing change).
    deployed = MonitorPolicy.of("monitor", {"sa-a", "sa-c"})
    diff = engine.diff(deployed, baseline)
    assert diff.reduces_coverage
    assert diff.removed == frozenset({"sa-b"})
    assert diff.removed_sorted() == ("sa-b",)
    assert engine.reduces_coverage(deployed, baseline)


def test_diff_reports_added_coverage_without_reduction() -> None:
    engine = PolicyEngine()
    deployed = MonitorPolicy.of("monitor", {"sa-a", "sa-b", "sa-c", "sa-d"})
    diff = engine.diff(deployed, _baseline())
    assert diff.added == frozenset({"sa-d"})
    assert not diff.reduces_coverage


def test_reconcile_reports_reduced_components() -> None:
    engine = PolicyEngine()
    baseline = _baseline()
    deployed = [
        MonitorPolicy.of("monitor", {"sa-a"}),  # dropped sa-b, sa-c
        MonitorPolicy.of("logging", {"sa-a", "sa-b"}),  # unchanged
    ]
    reduced = engine.reduced_components(deployed, baseline)
    assert reduced == ("monitor",)
    diffs = engine.reconcile(deployed, baseline)
    assert diffs["monitor"].removed == frozenset({"sa-b", "sa-c"})
    assert diffs["logging"].unchanged


def test_reconcile_rejects_undeclared_component() -> None:
    engine = PolicyEngine()
    with pytest.raises(PolicyError):
        engine.reconcile([MonitorPolicy.of("mystery", {"sa-a"})], _baseline())


def test_baseline_rejects_duplicate_component() -> None:
    with pytest.raises(PolicyError):
        CoverageBaseline.from_policies(
            [MonitorPolicy.of("monitor", {"sa-a"}), MonitorPolicy.of("monitor", {"sa-b"})],
        )
