"""The cluster ledger: per-identity compute and inference spend with quota enforcement.

Each identity accrues :class:`Spend` (compute units and inference units). An optional :class:`Quota` caps
each dimension; a charge that would push either dimension past its cap is rejected with
:class:`QuotaExceeded` and the ledger is left unchanged, so accounting stays consistent under a rejected
charge. :meth:`report` returns a stable, sorted snapshot for billing and dashboards.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_jobsvc.errors import QuotaExceeded


@dataclass(frozen=True)
class Spend:
    """Accrued spend for one identity: compute units and inference units."""

    compute: float = 0.0
    inference: float = 0.0

    def plus(self, *, compute: float = 0.0, inference: float = 0.0) -> Spend:
        """This spend with ``compute`` and ``inference`` added."""
        return Spend(compute=self.compute + compute, inference=self.inference + inference)


@dataclass(frozen=True)
class Quota:
    """A per-identity cap on compute and inference units (``None`` means unbounded on that dimension)."""

    compute: float | None = None
    inference: float | None = None

    def admits(self, spend: Spend) -> bool:
        """Whether ``spend`` stays within both caps."""
        if self.compute is not None and spend.compute > self.compute:
            return False
        return not (self.inference is not None and spend.inference > self.inference)


class Ledger:
    """Records per-identity spend and enforces per-identity quotas."""

    def __init__(self) -> None:
        """Start with no recorded spend and no quotas set."""
        self._spend: dict[str, Spend] = {}
        self._quota: dict[str, Quota] = {}

    def set_quota(
        self,
        identity: str,
        *,
        compute: float | None = None,
        inference: float | None = None,
    ) -> None:
        """Set (or replace) the quota for ``identity``."""
        self._quota[identity] = Quota(compute=compute, inference=inference)

    def quota(self, identity: str) -> Quota:
        """The quota for ``identity`` (an unbounded quota if none was set)."""
        return self._quota.get(identity, Quota())

    def charge(self, identity: str, *, compute: float = 0.0, inference: float = 0.0) -> Spend:
        """Add a charge to ``identity`` and return the new total.

        Raises:
            QuotaExceeded: if the charge would push either dimension past the identity's quota; the ledger
                is left unchanged.
        """
        if compute < 0 or inference < 0:
            raise QuotaExceeded(f"charge for {identity!r} must be non-negative", path=identity)
        current = self._spend.get(identity, Spend())
        updated = current.plus(compute=compute, inference=inference)
        quota = self.quota(identity)
        if not quota.admits(updated):
            raise QuotaExceeded(
                f"charge would exceed quota for {identity!r}",
                path=identity,
                compute=updated.compute,
                inference=updated.inference,
            )
        self._spend[identity] = updated
        return updated

    def spent(self, identity: str) -> Spend:
        """The spend accrued by ``identity`` so far."""
        return self._spend.get(identity, Spend())

    def remaining(self, identity: str) -> Quota:
        """How much of each capped dimension ``identity`` has left (``None`` where uncapped)."""
        quota = self.quota(identity)
        spent = self.spent(identity)
        compute = None if quota.compute is None else max(quota.compute - spent.compute, 0.0)
        inference = None if quota.inference is None else max(quota.inference - spent.inference, 0.0)
        return Quota(compute=compute, inference=inference)

    def report(self) -> dict[str, Spend]:
        """A snapshot of every identity's spend, keyed by identity in sorted order."""
        return {identity: self._spend[identity] for identity in sorted(self._spend)}

    def total(self) -> Spend:
        """The aggregate spend across every identity."""
        total = Spend()
        for spend in self._spend.values():
            total = total.plus(compute=spend.compute, inference=spend.inference)
        return total
