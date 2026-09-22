"""meridian-serving: the Meridian inference serving stack.

Owns the admission queue and priority request queue, the batch scheduler (padded batching), the paged KV cache
with pluggable eviction, the deterministic sampler (temperature / top-k / top-p / repetition penalty), the
paged-attention block allocator, the model router with a metrics exporter, and the serve endpoint.
Depends only
on meridian-common; its throughput and cost feed evalkit's benchmark.
"""

from __future__ import annotations

__version__ = "0.6.0"

from meridian_serving import api, cache, paged, queue, router, sampler, scheduler
from meridian_serving.types import Batch, Priority, Request, Response

__all__ = [
    "Batch",
    "Priority",
    "Request",
    "Response",
    "api",
    "cache",
    "paged",
    "queue",
    "router",
    "sampler",
    "scheduler",
]
