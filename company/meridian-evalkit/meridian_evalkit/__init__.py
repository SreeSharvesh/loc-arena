"""meridian-evalkit: the Meridian eval harness and leaderboard.

Owns the benchmark harness (running eval items through a model, scoring them, and annotating per-item teacher
features reused from the distillation platform), a registry of metrics (accuracy, calibration, ``pass@k``, and
a reference-embedding similarity), serving-driven eval runners with deterministic parallelism, a versioned
results store with run diffing, a leaderboard with a discriminative-power analysis, a throughput/cost
benchmark that drives the serving stack, and the eval-corpus contamination metric that reuses the data
platform. It depends on meridian-common, meridian-serving, meridian-datapipe, and meridian-distill, and holds
most of the workspace's cross-repo integration tests.
"""

from __future__ import annotations

__version__ = "0.2.0"

from meridian_evalkit import (
    bench,
    contamination,
    errors,
    harness,
    leaderboard,
    metrics,
    runners,
    store,
)
from meridian_evalkit.bench import BenchResult, ThroughputBench, build_workload
from meridian_evalkit.contamination import contamination_rate, corpus_contamination_rate
from meridian_evalkit.harness import (
    DeterministicModel,
    EvalItem,
    Harness,
    HarnessReport,
    ItemResult,
    Model,
    Prediction,
)
from meridian_evalkit.leaderboard import (
    Leaderboard,
    LeaderboardEntry,
    discriminative_power,
    score_spread,
)
from meridian_evalkit.metrics import (
    MetricRegistry,
    RunningAccuracy,
    ScoreRecord,
    default_registry,
)
from meridian_evalkit.runners import ParallelRunner, RunnerResult, SequentialRunner
from meridian_evalkit.store import EvalRun, ResultsStore, RunDiff

__all__ = [
    "BenchResult",
    "DeterministicModel",
    "EvalItem",
    "EvalRun",
    "Harness",
    "HarnessReport",
    "ItemResult",
    "Leaderboard",
    "LeaderboardEntry",
    "Model",
    "MetricRegistry",
    "Prediction",
    "ParallelRunner",
    "ResultsStore",
    "RunDiff",
    "RunnerResult",
    "RunningAccuracy",
    "ScoreRecord",
    "SequentialRunner",
    "ThroughputBench",
    "__version__",
    "bench",
    "build_workload",
    "contamination",
    "contamination_rate",
    "corpus_contamination_rate",
    "default_registry",
    "discriminative_power",
    "errors",
    "harness",
    "leaderboard",
    "metrics",
    "runners",
    "score_spread",
    "store",
]
