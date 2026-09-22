"""The serve endpoint: admit, schedule, cache, and decode requests into responses.

:class:`ServingEngine` ties the stack together. It admits requests, forms padded batches (whose padded-token
total is the serving-step cost), maintains a KV cache keyed by request content, decodes each request's tokens
with the deterministic sampler, and reports a ``served_checksum`` over the per-request cached results plus the
scheduling cost. Everything is deterministic given the inputs, so a workload always produces the same result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meridian_serving.cache.kv_cache import KVCache
from meridian_serving.queue.admission import AdmissionController, AdmissionLimits
from meridian_serving.sampler.sampling import Sampler
from meridian_serving.scheduler.batch import BatchScheduler
from meridian_serving.types import Request, Response

_VOCAB = 32


@dataclass(frozen=True)
class ServeResult:
    """The outcome of serving a workload: the responses, the cost, and the served checksum."""

    responses: tuple[Response, ...]
    schedule_cost: int
    served_checksum: int
    cache_evictions: int
    admitted: int
    rejected: int
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def total_generated(self) -> int:
        """The total tokens generated across all responses."""
        return sum(r.generated_len for r in self.responses)


def _request_value(request: Request) -> int:
    """A deterministic integer summarizing a request's prompt (the cached KV value)."""
    acc = 1469598103934665603
    for tok in request.prompt:
        acc = (acc ^ (tok + 1)) * 1099511628211 % (2**61 - 1)
    return acc % 100003


def _logits(context: tuple[int, ...]) -> list[float]:
    """A deterministic logit vector over a small vocabulary derived from the current context."""
    base = sum((i + 1) * (t + 1) for i, t in enumerate(context)) if context else 1
    return [((base * (v + 3)) % 97) / 13.0 for v in range(_VOCAB)]


class ServingEngine:
    """Serves a workload deterministically through the queue/scheduler/cache/sampler stack."""

    def __init__(
        self,
        *,
        batch_size: int = 8,
        cache_capacity: int = 64,
        cache_key_space: int = 128,
        sampler: Sampler | None = None,
        limits: AdmissionLimits | None = None,
    ) -> None:
        """Configure batch size, cache capacity and key space, the sampler, and admission limits."""
        self._scheduler = BatchScheduler(batch_size)
        self._cache = KVCache(cache_capacity)
        self._key_space = cache_key_space
        self._sampler = sampler if sampler is not None else Sampler(temperature=1.0, top_p=0.95)
        self._admission = AdmissionController(limits)

    def _decode(self, request: Request) -> Response:
        context = tuple(request.prompt)
        out: list[int] = []
        for _ in range(request.max_tokens):
            token = self._sampler.argmax(_logits(context))
            out.append(token)
            context = (*context, token)
        return Response(
            request_id=request.request_id,
            tokens=tuple(out),
            prompt_len=request.prompt_len,
            finish_reason="length",
        )

    def serve(self, requests: list[Request]) -> ServeResult:
        """Serve ``requests`` and return the responses, the scheduling cost, and the served checksum."""
        admitted: list[Request] = []
        rejected = 0
        for req in requests:
            if self._admission.admit(req).admitted:
                admitted.append(req)
            else:
                rejected += 1

        plan = self._scheduler.form_batches(admitted)
        checksum = 0
        responses: list[Response] = []
        for req in admitted:
            value = _request_value(req)
            key = f"k{value % self._key_space}"
            self._cache.put(key, value)
            checksum = (checksum + self._cache.served(key)) % (2**31 - 1)
            responses.append(self._decode(req))
            self._admission.retire(req)

        return ServeResult(
            responses=tuple(responses),
            schedule_cost=plan.total_padded_tokens,
            served_checksum=checksum,
            cache_evictions=self._cache.evictions,
            admitted=len(admitted),
            rejected=rejected,
        )
