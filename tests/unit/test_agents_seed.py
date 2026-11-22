"""The agents service's seed as the verifier reads it: a file of another shape fails rather than miscounts."""

from collections.abc import Callable
from pathlib import Path

import pytest
from loc_arena.verifier.agents_seed import read_seeded_agents, read_seeded_schedules
from pydantic import ValidationError


@pytest.mark.parametrize("read", [read_seeded_agents, read_seeded_schedules], ids=["agents", "schedules"])
@pytest.mark.parametrize(
    "document",
    ["{}\n", "eval-summarizer:\n  created_by: batch-runner\n"],
    ids=["an empty mapping", "a mapping by name"],
)
def test_a_seed_file_that_holds_no_list_is_refused(
    tmp_path: Path,
    read: Callable[[Path], object],
    document: str,
) -> None:
    seed = tmp_path / "seed.yaml"
    seed.write_text(document, encoding="utf-8")

    with pytest.raises(ValidationError):
        read(seed)
