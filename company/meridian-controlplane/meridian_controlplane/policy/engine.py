"""The monitor-policy engine: apply coverage changes and reconcile deployed coverage against a baseline.

:class:`PolicyEngine` applies validated changes to a :class:`MonitorPolicy` and compares a deployed policy to
a declared :class:`CoverageBaseline`, producing a :class:`CoverageDiff` per component. Its
:meth:`reconcile` walks every declared component and reports whether any component's deployed coverage is
narrower than the baseline, which is the signal a reviewer relies on when a change would reduce what
monitoring observes.
"""

from __future__ import annotations

from collections.abc import Iterable

from meridian_controlplane.errors import PolicyError
from meridian_controlplane.policy.coverage import (
    CoverageBaseline,
    CoverageDiff,
    MonitorPolicy,
)


class PolicyEngine:
    """Applies coverage changes and reconciles deployed coverage against a baseline."""

    def apply(
        self,
        policy: MonitorPolicy,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
    ) -> MonitorPolicy:
        """Return ``policy`` with ``add`` identities covered and ``remove`` identities dropped.

        Raises:
            PolicyError: if the same identity is both added and removed in one change.
        """
        add_set = frozenset(add)
        remove_set = frozenset(remove)
        conflict = add_set & remove_set
        if conflict:
            raise PolicyError(f"identities both added and removed: {sorted(conflict)}", path="change")
        return policy.covering(*add_set).dropping(*remove_set)

    def diff(self, deployed: MonitorPolicy, baseline: CoverageBaseline) -> CoverageDiff:
        """Diff a deployed policy against the baseline coverage for its component."""
        expected = baseline.covered(deployed.component)
        removed = expected - deployed.covered
        added = deployed.covered - expected
        return CoverageDiff(component=deployed.component, removed=removed, added=added)

    def reduces_coverage(self, deployed: MonitorPolicy, baseline: CoverageBaseline) -> bool:
        """Whether the deployed policy covers strictly fewer identities than the baseline."""
        return self.diff(deployed, baseline).reduces_coverage

    def reconcile(
        self, deployed: Iterable[MonitorPolicy], baseline: CoverageBaseline
    ) -> dict[str, CoverageDiff]:
        """Diff every deployed component against the baseline, keyed by component name.

        Every deployed component must be declared in the baseline; an undeclared component has no reference
        to reconcile against and is rejected.
        """
        diffs: dict[str, CoverageDiff] = {}
        for policy in deployed:
            if policy.component not in baseline.components:
                raise PolicyError(
                    f"component {policy.component!r} is not declared in the baseline",
                    path="component",
                )
            diffs[policy.component] = self.diff(policy, baseline)
        return diffs

    def reduced_components(
        self, deployed: Iterable[MonitorPolicy], baseline: CoverageBaseline
    ) -> tuple[str, ...]:
        """The names of components whose deployed coverage is narrower than the baseline, sorted."""
        diffs = self.reconcile(deployed, baseline)
        return tuple(sorted(name for name, diff in diffs.items() if diff.reduces_coverage))
