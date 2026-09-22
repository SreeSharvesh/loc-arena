"""Deterministic benchmark workloads for the throughput/cost bench.

:func:`build_workload` produces a reproducible list of :class:`meridian_serving.types.Request` from a size and
a seed, with prompt lengths and token ids spread across a range so batching and padding are exercised. The
same ``(size, seed)`` always yields byte-identical requests, so a bench result depends only on the workload
and the serving configuration.
"""

from __future__ import annotations

from meridian_evalkit.errors import BenchError
from meridian_serving.types import Priority, Request


def build_workload(
    size: int,
    *,
    seed: int = 0,
    min_prompt: int = 4,
    max_prompt: int = 24,
    max_tokens: int = 8,
    vocab: int = 32,
) -> list[Request]:
    """Build ``size`` deterministic requests whose prompt lengths span ``[min_prompt, max_prompt]``."""
    if size < 1:
        raise BenchError("workload size must be >= 1", code="evalkit.bench", size=size)
    if min_prompt < 1 or max_prompt < min_prompt:
        raise BenchError("prompt bounds must satisfy 1 <= min <= max", code="evalkit.bench")
    span = max_prompt - min_prompt + 1
    requests: list[Request] = []
    state = (seed * 2654435761 + 1) & 0xFFFFFFFF
    for i in range(size):
        state = (state * 1103515245 + 12345) & 0x7FFFFFFF
        length = min_prompt + (state % span)
        prompt = tuple((state // (j + 1) + j * 7 + i) % vocab for j in range(length))
        requests.append(
            Request(
                request_id=f"bench-{seed}-{i}",
                prompt=prompt,
                max_tokens=max_tokens,
                priority=Priority.NORMAL,
                arrival_seq=i,
            )
        )
    return requests
