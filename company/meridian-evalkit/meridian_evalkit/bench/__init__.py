"""Bench: a throughput/cost benchmark that drives serving and reports its cost.

Owns the deterministic workload builder and the :class:`ThroughputBench` that runs a workload through a
serving engine into a :class:`BenchResult`.
"""

from __future__ import annotations

from meridian_evalkit.bench.throughput import BenchResult, ThroughputBench
from meridian_evalkit.bench.workload import build_workload

__all__ = [
    "BenchResult",
    "ThroughputBench",
    "build_workload",
]
