"""Cross-repo integration tests: the coupling edges evalkit shares with its dependencies.

Each edge is pinned so that a change to the depended-on repo's contract moves the evalkit value and fails a
test here:

* SERVING.scheduler <-> evalkit bench: the bench's scheduling cost is exactly the serving scheduler's padded
  cost for a fixed workload (edge (a)).
* DATAPIPE.contamination <-> evalkit metric: evalkit's contamination metric is the data platform's
  contamination rate for a fixed corpus and holdout (edge (b)).
* DISTILL.teacher features <-> evalkit feature reuse: the harness's per-item features are the distill
  platform's teacher features for the same tokens (edge (c)).
"""

from __future__ import annotations

import pytest

from meridian_datapipe import ContaminationFilter, Document, HoldoutRegistry
from meridian_distill.teacher import TeacherResponse, compute_features, teacher_features
from meridian_evalkit.bench import ThroughputBench
from meridian_evalkit.contamination import contamination_rate, corpus_contamination_rate
from meridian_evalkit.harness import DeterministicModel, EvalItem, Harness
from meridian_evalkit.harness.features import item_features
from meridian_evalkit.harness.model import Prediction
from meridian_serving.api.serve import ServingEngine
from meridian_serving.scheduler.batch import BatchScheduler
from meridian_serving.types import Request

# ---------------------------------------------------------------------------
# Fixed workload for the serving <-> bench coupling.
# ---------------------------------------------------------------------------
_BENCH_WORKLOAD = [
    Request(request_id=f"w{i}", prompt=tuple(range(1, plen + 1)), max_tokens=4, arrival_seq=i)
    for i, plen in enumerate([3, 5, 2, 8, 4, 6])
]
_EXPECTED_SCHEDULE_COST = 48
_EXPECTED_SERVED_CHECKSUM = 267731


# ----- edge (a): SERVING.scheduler <-> evalkit bench --------------------------


def test_bench_cost_equals_serving_scheduler() -> None:
    bench = ThroughputBench(ServingEngine(batch_size=4)).run(_BENCH_WORKLOAD)
    scheduler_cost = BatchScheduler(4).form_batches(list(_BENCH_WORKLOAD)).total_padded_tokens
    assert bench.schedule_cost == scheduler_cost


def test_bench_cost_equals_serving_engine() -> None:
    bench = ThroughputBench(ServingEngine(batch_size=4)).run(_BENCH_WORKLOAD)
    engine = ServingEngine(batch_size=4).serve(list(_BENCH_WORKLOAD))
    assert bench.schedule_cost == engine.schedule_cost
    assert bench.served_checksum == engine.served_checksum


def test_bench_cost_pinned_to_serving_contract() -> None:
    # this expected value is the serving contract for the fixed workload; a batching change moves it
    bench = ThroughputBench(ServingEngine(batch_size=4)).run(_BENCH_WORKLOAD)
    assert bench.schedule_cost == _EXPECTED_SCHEDULE_COST
    assert bench.served_checksum == _EXPECTED_SERVED_CHECKSUM


# ----- edge (b): DATAPIPE.contamination <-> evalkit metric --------------------


def _corpus() -> list[Document]:
    return [Document(doc_id=str(i), text=t, source="c") for i, t in enumerate(["aa", "bb", "cc", "dd", "ee"])]


def _holdout() -> HoldoutRegistry:
    return HoldoutRegistry.from_documents(
        [Document(doc_id="h1", text="bb"), Document(doc_id="h2", text="dd")],
        num_shards=1,
    )


def test_contamination_metric_equals_datapipe() -> None:
    docs = _corpus()
    registry = _holdout()
    evalkit_value = corpus_contamination_rate(docs, registry)
    datapipe_value = ContaminationFilter(registry).contamination_rate(docs)
    assert evalkit_value == datapipe_value


def test_contamination_metric_pinned_value() -> None:
    # two of five corpus documents are in the holdout; a datapipe contamination change moves this
    assert corpus_contamination_rate(_corpus(), _holdout()) == pytest.approx(0.4)


def test_contamination_over_eval_items() -> None:
    items = [EvalItem(item_id=str(i), prompt=(1, 2, i), reference=()) for i in range(5)]
    holdout = HoldoutRegistry.from_documents(
        [Document(doc_id="h", text="1 2 1"), Document(doc_id="h2", text="1 2 3")],
        num_shards=1,
    )
    assert contamination_rate(items, holdout) == pytest.approx(0.4)


# ----- edge (c): DISTILL.teacher features <-> evalkit feature reuse -----------


_EXPECTED_FEATURES = (0.25, 0.25, 0.25, 0.25, 0.0, 0.0, 0.0, 0.0085)


def test_harness_features_match_distill() -> None:
    item = EvalItem(item_id="x", prompt=(1, 2, 3), reference=(4, 5))
    prediction = Prediction(item_id="x", tokens=(7, 8, 9, 10), confidence=0.5)
    evalkit_features = item_features(item, prediction)
    distill_features = compute_features(prediction.tokens)
    assert evalkit_features == distill_features


def test_harness_features_pinned_to_distill_contract() -> None:
    # this expected vector is the distill feature contract for the tokens; a distill feature change moves it
    item = EvalItem(item_id="x", prompt=(1, 2, 3), reference=(4, 5))
    prediction = Prediction(item_id="x", tokens=(7, 8, 9, 10), confidence=0.5)
    assert item_features(item, prediction) == _EXPECTED_FEATURES


def test_harness_features_read_through_teacher_features() -> None:
    item = EvalItem(item_id="x", prompt=(1, 2, 3), reference=(4, 5))
    prediction = Prediction(item_id="x", tokens=(7, 8, 9, 10), confidence=0.5)
    response = TeacherResponse(
        request_key="eval-x",
        prompt=item.prompt,
        tokens=prediction.tokens,
        features=compute_features(prediction.tokens),
    )
    assert item_features(item, prediction) == teacher_features(response)


def test_report_features_match_distill_for_every_item() -> None:
    items = [EvalItem(item_id=f"q{i}", prompt=(1, i), reference=(i % 5, (i + 2) % 5)) for i in range(10)]
    model = DeterministicModel(accuracy=0.5, seed=4)
    report = Harness().run(model, items)
    for result in report.results:
        assert result.features == compute_features(result.prediction)


# ----- end-to-end wiring across evalkit's own modules -------------------------


def test_end_to_end_harness_store_leaderboard() -> None:
    from meridian_evalkit.leaderboard import Leaderboard
    from meridian_evalkit.store import EvalRun, ResultsStore

    items = [EvalItem(item_id=f"q{i}", prompt=(1, i % 4), reference=(i % 3,)) for i in range(60)]
    store = ResultsStore()
    board = Leaderboard()
    # a shared seed makes the correct-set monotonic in the target accuracy, so the models' scores are
    # distinct and strictly ordered (which keeps this test off the leaderboard's tie behavior)
    for name, acc in [("weak", 0.3), ("mid", 0.6), ("strong", 0.9)]:
        report = Harness().run(DeterministicModel(accuracy=acc, seed=11), items)
        run = EvalRun.from_report(f"run-{name}", name, report)
        store.append(run)
        board.add_run(run, metric="accuracy")
    assert len(store) == 3
    scores = sorted(e.score for e in board.entries)
    assert len(set(scores)) == 3
    assert board.ranking()[0].model == "strong"
    assert board.ranking()[-1].model == "weak"
