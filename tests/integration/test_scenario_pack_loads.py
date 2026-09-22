"""A scenario pack is a self-contained directory the loader resolves by name.

``scenarios/<name>/`` holds ``scenario.yaml`` (scorer/verifier names + the platform/world anchor repo),
``main.py``/``side.py`` (imports register the scorer/verifier), and a sealed ``reference/`` only the grader
reads (the grade harness plus the reference numbers). The graded checkout is the seven side-by-side
``company/`` repos (see ``main_task_grader.seed_checkout``), not a per-pack toy seed. Loading the pack
runs its
registrations and exposes the sealed reference path.
"""

from __future__ import annotations

from scenarios.loader import load_scenario


def test_scenario_pack_loads_and_exposes_seed_and_reference() -> None:
    sc = load_scenario("aurora_efficiency")
    assert sc.scorer == "aurora_pipeline_cost"
    assert sc.verifier == "kill_chain_v1"
    assert sc.seed_repo == "meridian-serving"  # the platform/world anchor repo
    # the sealed reference exists and holds the grade harness + reference numbers (only the grader reads it)
    assert (sc.reference_dir / "reference.json").exists()
    assert (sc.reference_dir / "grade_bench.py").exists()
    # the graded checkout is built from the shared company monorepo, not a per-pack seed dir
    from loc_arena.tasks.main_task_grader import _COMPANY_ROOT, _REPOS

    assert len(_REPOS) == 7 and (_COMPANY_ROOT / sc.seed_repo).is_dir()


def test_loading_the_pack_runs_its_registrations() -> None:
    from loc_arena.registry import is_scorer, is_verifier

    load_scenario("aurora_efficiency")
    assert is_scorer("aurora_pipeline_cost")  # main.py's @register_scorer ran on import
    assert is_verifier("kill_chain_v1")  # side.py bound the verifier on import


def test_loading_is_idempotent() -> None:
    # re-loading must not trip the registry's duplicate-registration guard (importlib re-exec would)
    a = load_scenario("aurora_efficiency")
    b = load_scenario("aurora_efficiency")
    assert a.scorer == b.scorer and a.verifier == b.verifier
