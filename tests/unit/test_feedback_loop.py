from __future__ import annotations

from loc_arena.verifier.feedback_loop import GenerateRecord, find_self_feeding_runs, longest_self_feeding_run


def _rec(seq: int, prompt_fp: str, output_fp: str, caller: str = "c") -> GenerateRecord:
    return GenerateRecord(
        seq=seq,
        ts=float(seq),
        episode_id="e",
        caller_identity=caller,
        turn_id=None,
        batch_id=None,
        prompt_fp=prompt_fp,
        output_fp=output_fp,
        model_role="r",
    )


def test_detects_single_maximal_chain() -> None:
    recs = [_rec(0, "seed", "o0"), _rec(1, "o0", "o1"), _rec(2, "o1", "o2")]
    (run,) = find_self_feeding_runs(recs)
    assert [r.seq for r in run] == [0, 1, 2]


def test_unlinked_calls_are_singletons() -> None:
    recs = [_rec(0, "a", "b"), _rec(1, "c", "d"), _rec(2, "e", "f")]
    runs = find_self_feeding_runs(recs)
    assert runs == []  # nothing links, so no real self-feeding run
    assert longest_self_feeding_run(recs) == []


def test_chains_are_per_caller() -> None:
    recs = [
        _rec(0, "seed", "o0", caller="a"),
        _rec(1, "o0", "o1", caller="b"),  # different caller: does NOT extend a's chain
        _rec(2, "o1", "o2", caller="b"),
    ]
    runs = find_self_feeding_runs(recs)
    lens = sorted(len(r) for r in runs)
    assert lens == [2]  # a: singleton (dropped); b: a real 2-chain


def test_longest_run_selected() -> None:
    recs = [
        _rec(0, "x", "y"),  # singleton
        _rec(1, "seed", "o1"),
        _rec(2, "o1", "o2"),
        _rec(3, "o2", "o3"),
    ]
    assert len(longest_self_feeding_run(recs)) == 3
