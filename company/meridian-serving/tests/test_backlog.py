"""Contract tests tracking open backlog tickets against meridian-serving (and one upstream ticket).

Each is a terse ``strict`` xfail tied to its ticket: it asserts documented behavior, currently fails, and
turns
into a hard failure the moment the ticket is resolved. Mechanisms are intentionally not described here.
"""

from __future__ import annotations

import random

import pytest

from meridian_common.retry import JitteredBackoff
from meridian_serving.cache import KVCache
from meridian_serving.sampler import Sampler
from meridian_serving.sampler.sampling import _nucleus_mask, softmax


@pytest.mark.xfail(strict=True, reason="MER-SERVING-31 (open)")
def test_cache_serves_fresh_value_after_eviction_reuse() -> None:
    cache = KVCache(capacity=2)
    cache.put("a", 10)
    cache.put("b", 20)
    cache.value("a")  # touch a so b is the eviction victim
    cache.put("c", 30)  # evicts b and reuses its slot for c
    assert cache.served("c") == cache.value("c")


@pytest.mark.xfail(strict=True, reason="MER-SERVING-47 (open)")
def test_top_p_nucleus_matches_temperature_scaled_distribution() -> None:
    temperature, top_p = 0.5, 0.7
    logits = [2.0, 1.0, 0.0]
    sampler = Sampler(temperature=temperature, top_p=top_p)
    expected = _nucleus_mask(softmax([x / temperature for x in logits]), top_p)
    assert sampler.nucleus(logits) == expected


@pytest.mark.xfail(strict=True, reason="MER-COMMON-14 (open)")
def test_serving_retry_backoff_meets_minimum_wait_sla() -> None:
    # a serving retry loop must never wait below half the exponential term for a given attempt (the SLA the
    # serving team relies on for tail-latency budgeting); attempt 3 -> term 4.0, floor 2.0
    backoff = JitteredBackoff(base=1.0, cap=100.0, rng=random.Random(7))
    waits = [backoff.delay(3) for _ in range(200)]
    assert min(waits) >= 2.0
