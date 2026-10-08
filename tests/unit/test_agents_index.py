"""Every path the AGENTS.md index names exists, so the index cannot drift from the tree unnoticed."""

from __future__ import annotations

import re
from pathlib import Path

REPOSITORY = Path(__file__).parents[2]


def test_every_path_in_the_agents_index_exists() -> None:
    index_rows = [
        line for line in (REPOSITORY / "AGENTS.md").read_text().splitlines() if line.startswith("| `")
    ]

    named = [path for row in index_rows for path in re.findall(r"`([^`]+)`", row.split("|")[1])]

    missing = [path for path in named if not any(REPOSITORY.glob(path.rstrip("/")))]  # a pattern must match
    assert missing == []
