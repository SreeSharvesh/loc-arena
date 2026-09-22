"""Monitor coverage sets and the diff of a deployed set against a baseline.

A :class:`MonitorPolicy` names one observability component (for example ``"monitor"`` or ``"logging"``) and
the set of identities that component covers. A :class:`CoverageBaseline` is the declared, authoritative
coverage for one or more components that deployed policy is reconciled against. A :class:`CoverageDiff`
records, for one component, which identities the deployed policy dropped relative to the baseline and which it
added, so a reviewer can see at a glance whether a change narrows what monitoring observes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from meridian_controlplane.errors import PolicyError


def _clean_component(component: str) -> str:
    """Validate and normalize a component name (non-empty after stripping whitespace)."""
    name = component.strip()
    if not name:
        raise PolicyError("component name must be non-empty", path="component")
    return name


@dataclass(frozen=True)
class MonitorPolicy:
    """One observability component and the set of identities it covers.

    ``covered`` is the set of identity ids (service accounts, agents) that the ``component`` observes. The
    policy is immutable; :meth:`covering` and :meth:`dropping` derive changed copies rather than mutating.
    """

    component: str
    covered: frozenset[str]

    @classmethod
    def of(cls, component: str, covered: Iterable[str]) -> MonitorPolicy:
        """Build a policy for ``component`` covering ``covered`` (order-insensitive, de-duplicated)."""
        return cls(component=_clean_component(component), covered=frozenset(covered))

    def covers(self, identity: str) -> bool:
        """Whether ``identity`` is covered by this component."""
        return identity in self.covered

    def covering(self, *identities: str) -> MonitorPolicy:
        """A copy of this policy with ``identities`` added to the covered set."""
        return MonitorPolicy(component=self.component, covered=self.covered | frozenset(identities))

    def dropping(self, *identities: str) -> MonitorPolicy:
        """A copy of this policy with ``identities`` removed from the covered set."""
        return MonitorPolicy(component=self.component, covered=self.covered - frozenset(identities))

    @property
    def size(self) -> int:
        """The number of identities this component covers."""
        return len(self.covered)


@dataclass(frozen=True)
class CoverageDiff:
    """The per-component difference between a deployed coverage set and a baseline.

    ``removed`` are identities the baseline covers that the deployed policy does not (a coverage reduction);
    ``added`` are identities the deployed policy covers beyond the baseline.
    """

    component: str
    removed: frozenset[str]
    added: frozenset[str]

    @property
    def reduces_coverage(self) -> bool:
        """Whether the deployed policy covers strictly fewer identities than the baseline."""
        return bool(self.removed)

    @property
    def unchanged(self) -> bool:
        """Whether deployed coverage equals the baseline for this component."""
        return not self.removed and not self.added

    def removed_sorted(self) -> tuple[str, ...]:
        """The dropped identities in sorted order (stable for reporting)."""
        return tuple(sorted(self.removed))


@dataclass(frozen=True)
class CoverageBaseline:
    """The declared, authoritative coverage for a set of components.

    A baseline is the reference deployed policy is reconciled against: the set of identities each component is
    expected to cover. It is built once from the sanctioned configuration and compared, never mutated.
    """

    components: Mapping[str, frozenset[str]]

    @classmethod
    def from_policies(cls, policies: Iterable[MonitorPolicy]) -> CoverageBaseline:
        """Build a baseline from a set of per-component policies (one policy per component)."""
        components: dict[str, frozenset[str]] = {}
        for policy in policies:
            if policy.component in components:
                raise PolicyError(f"duplicate component {policy.component!r} in baseline", path="component")
            components[policy.component] = policy.covered
        return cls(components=dict(components))

    def covered(self, component: str) -> frozenset[str]:
        """The baseline coverage for ``component`` (empty if the component is not declared)."""
        return self.components.get(component, frozenset())

    def component_names(self) -> tuple[str, ...]:
        """The declared component names in sorted order."""
        return tuple(sorted(self.components))
