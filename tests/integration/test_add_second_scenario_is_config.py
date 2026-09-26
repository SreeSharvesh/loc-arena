"""Adding a (main task, side task) pair at a new point is CONFIG, not an engine change.

Dropping a second scenario pack -- a directory with ``scenario.yaml`` + ``main.py`` (registers a scorer) +
``side.py`` (registers a verifier) -- and pointing a config at it by name is all it takes. No file under
``loc_arena/`` is edited: the loader runs the pack's registrations and the harness dispatches by name.
"""

from __future__ import annotations

from pathlib import Path

from loc_arena.registry import get_scorer, get_verifier, is_scorer, is_verifier
from scenarios.loader import load_scenario


def _write_pack(root: Path) -> None:
    pack = root / "toy_scenario"
    (pack / "seed" / "toy-repo").mkdir(parents=True)
    (pack / "reference").mkdir(parents=True)
    (pack / "scenario.yaml").write_text(
        "name: toy_scenario\nscorer: toy_scorer\nverifier: toy_verifier\nseed_repo: toy-repo\n",
    )
    (pack / "main.py").write_text(
        "from loc_arena.registry import register_scorer\n\n\n"
        "@register_scorer('toy_scorer')\n"
        "def score(checkout, config, reference_dir):\n"
        "    return {'final_score': 1.0}\n",
    )
    (pack / "side.py").write_text(
        "from loc_arena.registry import register_verifier\n\n\n"
        "@register_verifier('toy_verifier')\n"
        "def verify(events, config, deadline_ts):\n"
        "    return {'landed': False}\n",
    )


def test_a_new_pack_registers_by_name_with_no_engine_change(tmp_path: Path) -> None:
    _write_pack(tmp_path)
    sc = load_scenario("toy_scenario", root=tmp_path)
    assert sc.scorer == "toy_scorer" and sc.verifier == "toy_verifier"

    # the pack imports registered both; the harness dispatches these exactly as it does the shipped run

    assert is_scorer("toy_scorer") and is_verifier("toy_verifier")
    assert get_scorer("toy_scorer")(sc.repo_seed(), None, sc.reference_dir) == {"final_score": 1.0}
    assert get_verifier("toy_verifier")([], None, 0.0) == {"landed": False}
