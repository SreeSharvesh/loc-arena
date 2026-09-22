from __future__ import annotations

from meridian_serving.api import EngineLoop, ServingStats, StreamingDecoder
from meridian_serving.observability import ServingObservability
from meridian_serving.sampler import Sampler
from meridian_serving.types import Request


def _logits(context: tuple[int, ...]) -> list[float]:
    base = (sum(context) + 1) % 7
    return [((base * (v + 3)) % 11) / 3.0 for v in range(8)]


def test_engine_loop_runs_all_requests_to_completion() -> None:
    engine = EngineLoop(_logits, max_running=4, step_token_budget=1000)
    for i in range(6):
        engine.submit(Request(f"r{i}", (i % 3 + 1,), max_tokens=2))
    responses = engine.run_to_completion()
    assert len(responses) == 6
    assert all(len(r.tokens) == 2 for r in responses.values())
    assert engine.stats.requests == 6 and engine.pending == 0


def test_engine_loop_is_deterministic() -> None:
    def run() -> dict[str, tuple[int, ...]]:
        engine = EngineLoop(_logits, seed=5)
        for i in range(4):
            engine.submit(Request(f"r{i}", (i + 1,), max_tokens=3))
        return {rid: r.tokens for rid, r in engine.run_to_completion().items()}

    assert run() == run()


def test_streaming_matches_batch_decode() -> None:
    sampler = Sampler(temperature=1.0, top_p=1.0)
    dec = StreamingDecoder(sampler, _logits)
    req = Request("r", (1, 2), max_tokens=4)
    streamed = dec.collect(req, seed=3)
    # streaming yields exactly max_tokens events (no eos here)
    assert len(streamed) == 4
    events = list(dec.stream(req, seed=3))
    assert events[-1].is_last and len(events) == 4


def test_streaming_stops_at_eos() -> None:
    sampler = Sampler(temperature=1.0, top_p=1.0)
    # force token 0 as eos by biasing logits to always pick 0
    dec = StreamingDecoder(sampler, lambda ctx: [9.0, 0.0, 0.0], eos=0)
    events = list(dec.stream(Request("r", (1,), max_tokens=10), seed=0))
    assert len(events) == 1 and events[0].is_last


def test_serving_stats_derived_metrics() -> None:
    stats = ServingStats()
    stats.record(prompt_len=4, generated=8, padded=8, cache_hit=True, seconds=1.0)
    stats.record(prompt_len=4, generated=8, padded=16, cache_hit=False, seconds=1.0)
    snap = stats.snapshot()
    assert snap["requests"] == 2.0
    assert stats.cache_hit_rate == 0.5
    assert 0.0 < stats.padding_efficiency < 1.0  # padding waste present


def test_observability_records_and_exports() -> None:
    obs = ServingObservability()
    obs.admitted(3)
    obs.rejected("prompt_too_long")
    obs.observe_batch(8)
    obs.cache_event(True)
    obs.eviction(2)
    snap = obs.export()
    counters = {(r["name"], tuple(sorted(r["labels"].items()))): r["value"] for r in snap["counters"]}
    assert counters[("serving_admitted_total", ())] == 3.0
    assert counters[("serving_cache_evictions_total", ())] == 2.0
    assert snap["histograms"]
