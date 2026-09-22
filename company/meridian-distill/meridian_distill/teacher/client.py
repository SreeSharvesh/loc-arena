"""The teacher client: run inference through the serving stack and cache the responses.

A :class:`TeacherClient` answers a prompt by running it through meridian-serving -- either a whole
:class:`~meridian_serving.api.serve.ServingEngine`, or a
:class:`~meridian_serving.sampler.sampling.Sampler` paired with a per-step logit function driven through the
streaming decoder. Each distinct prompt is generated once and cached by a request key; a metrics counter
records every real teacher call, which is the quota the platform budgets against. Generation is deterministic,
so a repeated prompt returns byte-identical tokens and features.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from meridian_common import cost
from meridian_common.metrics import MetricsRegistry
from meridian_common.serde.canonical import fingerprint
from meridian_distill.errors import TeacherError
from meridian_distill.teacher.response import DEFAULT_FEATURE_DIM, TeacherResponse, compute_features
from meridian_serving.api.serve import ServingEngine
from meridian_serving.api.streaming import StreamingDecoder
from meridian_serving.sampler.sampling import Sampler
from meridian_serving.types import Request

LogitFn = Callable[[tuple[int, ...]], list[float]]

_TEACHER_CALLS = "distill.teacher.calls"


def _request_key(prompt: tuple[int, ...], max_tokens: int) -> str:
    """A compact cache key over a request's prompt tokens and generation budget."""
    acc = 0
    for tok in prompt:
        acc = (acc + (tok + 1)) % (2**61 - 1)
    return fingerprint({"h": acc, "n": max_tokens})


class TeacherClient:
    """Runs prompts through the serving stack, caching each distinct request's teacher response."""

    def __init__(
        self,
        engine: ServingEngine | None = None,
        *,
        sampler: Sampler | None = None,
        logit_fn: LogitFn | None = None,
        max_tokens: int = 16,
        feature_dim: int = DEFAULT_FEATURE_DIM,
        metrics: MetricsRegistry | None = None,
        seed: int = 0,
    ) -> None:
        """Wire the client to a serving engine, or to a sampler plus a per-step logit function."""
        if max_tokens < 1:
            raise TeacherError("max_tokens must be at least 1", code="distill.teacher", max_tokens=max_tokens)
        if engine is None and (sampler is None or logit_fn is None):
            raise TeacherError(
                "provide a ServingEngine, or a Sampler together with a logit function",
                code="distill.teacher",
            )
        self._engine = engine
        self._decoder = (
            StreamingDecoder(sampler, logit_fn)
            if engine is None and sampler is not None and logit_fn
            else None
        )
        self._max_tokens = max_tokens
        self._feature_dim = feature_dim
        self._seed = seed
        self._metrics = metrics if metrics is not None else MetricsRegistry()
        self._counter = self._metrics.counter(_TEACHER_CALLS)
        self._cache: dict[str, TeacherResponse] = {}
        self._hits = 0
        self._seq = 0

    @property
    def metrics(self) -> MetricsRegistry:
        """The metrics registry recording this client's teacher-call counter."""
        return self._metrics

    @property
    def calls(self) -> int:
        """The number of real teacher calls made (the quota surface; excludes cache hits)."""
        return int(self._counter.value)

    @property
    def cache_hits(self) -> int:
        """The number of queries answered from the cache rather than a fresh teacher call."""
        return self._hits

    def _generate(self, prompt: tuple[int, ...]) -> tuple[int, ...]:
        request = Request(
            request_id=f"distill-{self._seq}",
            prompt=prompt,
            max_tokens=self._max_tokens,
        )
        self._seq += 1
        if self._engine is not None:
            result = self._engine.serve([request])
            if not result.responses:
                raise TeacherError("serving engine returned no response", code="distill.teacher")
            return result.responses[0].tokens
        assert self._decoder is not None  # guaranteed by __init__ validation
        return self._decoder.collect(request, seed=self._seed)

    def query(self, prompt: Sequence[int]) -> TeacherResponse:
        """Answer ``prompt`` through the serving stack, caching the response by its request key."""
        tokens_in = tuple(prompt)
        key = _request_key(tokens_in, self._max_tokens)
        cached = self._cache.get(key)
        if cached is not None:
            self._hits += 1
            return cached
        generated = self._generate(tokens_in)
        self._counter.inc()
        cost.record(
            "distill.teacher_call", self._max_tokens
        )  # the dominant per-call work (a real teacher call)
        response = TeacherResponse(
            request_key=key,
            prompt=tokens_in,
            tokens=generated,
            features=compute_features(generated, dim=self._feature_dim),
        )
        self._cache[key] = response
        return response
