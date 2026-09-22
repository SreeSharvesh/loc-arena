"""The stored eval-run value type.

An :class:`EvalRun` is a single benchmark run's durable record: the run id, the model that was evaluated, the
reduced metric values, and the ids of the items that were scored. Its :attr:`EvalRun.fingerprint` is a stable
content id over the canonical encoding of those fields (via :func:`meridian_common.serde.fingerprint`), so two
structurally equal runs share a fingerprint regardless of how they were built.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from meridian_common.serde import canonical_json, fingerprint
from meridian_evalkit.errors import StoreError
from meridian_evalkit.harness.harness import HarnessReport


@dataclass(frozen=True)
class EvalRun:
    """One benchmark run: its id, the model, the reduced metrics, and the scored item ids."""

    run_id: str
    model: str
    metrics: dict[str, float] = field(default_factory=dict)
    item_ids: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """A JSON-friendly mapping of this run (the canonical payload its fingerprint is taken over)."""
        return {
            "run_id": self.run_id,
            "model": self.model,
            "metrics": dict(self.metrics),
            "item_ids": list(self.item_ids),
        }

    def to_json(self) -> str:
        """The canonical JSON encoding of this run (stable and order-independent)."""
        return canonical_json(self.to_payload())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EvalRun:
        """Rebuild a run from a payload produced by :meth:`to_payload` (raises on a missing field)."""
        try:
            return cls(
                run_id=str(payload["run_id"]),
                model=str(payload["model"]),
                metrics={str(k): float(v) for k, v in payload["metrics"].items()},
                item_ids=tuple(str(i) for i in payload["item_ids"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError("malformed eval-run payload", code="evalkit.store") from exc

    @property
    def fingerprint(self) -> str:
        """A stable content fingerprint over the run's canonical payload."""
        return fingerprint(self.to_payload())

    @classmethod
    def from_report(cls, run_id: str, model: str, report: HarnessReport) -> EvalRun:
        """Build a run record from a harness report's metrics and scored item ids."""
        return cls(
            run_id=run_id,
            model=model,
            metrics=dict(report.metrics),
            item_ids=tuple(r.item_id for r in report.results),
        )
