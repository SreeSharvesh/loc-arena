"""The deterministic run's golden bundle, compared but for the run's wall-clock time."""

from __future__ import annotations

import json
from pathlib import Path

GOLDEN = Path(__file__).parent / "golden" / "aurora-efficiency.deterministic"


def scores_without_wall_clock(path: Path) -> dict[str, object]:
    scores = json.loads(path.read_text())
    del scores["wall_clock_seconds"]
    return scores
