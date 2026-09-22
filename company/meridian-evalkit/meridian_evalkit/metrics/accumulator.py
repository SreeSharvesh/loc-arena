"""An incremental accuracy accumulator for streaming and re-scored records.

:class:`RunningAccuracy` folds records in one at a time and reports the running accuracy without holding the
whole record list. It also keeps the latest outcome seen for each item id, so a corrected re-scoring of an
item can be observed after the fact. Tracked by backlog ticket MER-EVALKIT-41.
"""

from __future__ import annotations

from collections.abc import Iterable

from meridian_evalkit.errors import MetricError
from meridian_evalkit.metrics.records import ScoreRecord


class RunningAccuracy:
    """Folds record outcomes into a running accuracy while tracking each item's latest outcome."""

    def __init__(self) -> None:
        """Start with no observations."""
        self._correct = 0
        self._total = 0
        self._outcomes: dict[str, bool] = {}

    def observe(self, record: ScoreRecord) -> None:
        """Fold ``record`` into the running totals and record its item's latest outcome."""
        self._outcomes[record.item_id] = record.correct
        self._correct += 1 if record.correct else 0
        self._total += 1

    def extend(self, records: Iterable[ScoreRecord]) -> None:
        """Fold every record in ``records`` in order."""
        for record in records:
            self.observe(record)

    @property
    def distinct_items(self) -> int:
        """The number of distinct item ids observed."""
        return len(self._outcomes)

    @property
    def observations(self) -> int:
        """The number of ``observe`` calls folded in so far."""
        return self._total

    @property
    def value(self) -> float:
        """The running accuracy (raises if nothing has been observed)."""
        if self._total == 0:
            raise MetricError("no records observed", code="evalkit.metric")
        return self._correct / self._total
