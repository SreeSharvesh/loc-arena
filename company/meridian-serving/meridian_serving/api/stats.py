"""Serving statistics: aggregate throughput, latency, and cache effectiveness.

A :class:`ServingStats` accumulator folds per-request outcomes into running totals and derives the numbers an
operator watches: throughput (tokens per second), the padding-efficiency ratio (real tokens over padded
tokens), the cache hit rate, and the mean generated length. It is deterministic and dependency-light so the
serve loop can update it every step and export a snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ServingStats:
    """Running serving counters and the derived operator metrics."""

    requests: int = 0
    prompt_tokens: int = 0
    generated_tokens: int = 0
    padded_tokens: int = 0
    cache_hits: int = 0
    cache_lookups: int = 0
    wall_seconds: float = 0.0

    def record(
        self,
        *,
        prompt_len: int,
        generated: int,
        padded: int,
        cache_hit: bool,
        seconds: float,
    ) -> None:
        """Fold one request's outcome into the totals."""
        self.requests += 1
        self.prompt_tokens += prompt_len
        self.generated_tokens += generated
        self.padded_tokens += padded
        self.cache_lookups += 1
        if cache_hit:
            self.cache_hits += 1
        self.wall_seconds += seconds

    @property
    def throughput_tps(self) -> float:
        """Generated tokens per second (0.0 if no wall time recorded)."""
        return self.generated_tokens / self.wall_seconds if self.wall_seconds > 0 else 0.0

    @property
    def padding_efficiency(self) -> float:
        """Real prompt tokens over padded tokens (1.0 is perfect, lower means more padding waste)."""
        return self.prompt_tokens / self.padded_tokens if self.padded_tokens > 0 else 1.0

    @property
    def cache_hit_rate(self) -> float:
        """The fraction of lookups that hit the cache."""
        return self.cache_hits / self.cache_lookups if self.cache_lookups > 0 else 0.0

    @property
    def mean_generated_len(self) -> float:
        """The mean number of generated tokens per request."""
        return self.generated_tokens / self.requests if self.requests else 0.0

    def snapshot(self) -> dict[str, float]:
        """A deterministic mapping of the derived metrics for export."""
        return {
            "requests": float(self.requests),
            "throughput_tps": round(self.throughput_tps, 6),
            "padding_efficiency": round(self.padding_efficiency, 6),
            "cache_hit_rate": round(self.cache_hit_rate, 6),
            "mean_generated_len": round(self.mean_generated_len, 6),
        }
