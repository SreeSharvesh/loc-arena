"""Token-streaming decode: yield tokens one step at a time.

Interactive serving streams tokens as they are produced rather than returning the whole response at once. The
:class:`StreamingDecoder` yields a :class:`TokenEvent` per step, stopping at an end-of-sequence token or the
generation budget. It reuses the deterministic :class:`~meridian_serving.sampler.sampling.Sampler`, so a
streamed decode and a batch decode of the same request produce the same tokens.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from meridian_serving.sampler.sampling import Sampler
from meridian_serving.types import Request

LogitFn = Callable[[tuple[int, ...]], list[float]]


@dataclass(frozen=True)
class TokenEvent:
    """One streamed token: its index in the output, the token id, and whether it is the last."""

    index: int
    token: int
    is_last: bool


class StreamingDecoder:
    """Streams a request's tokens one step at a time using a deterministic sampler."""

    def __init__(self, sampler: Sampler, logit_fn: LogitFn, *, eos: int | None = None) -> None:
        """Wire the decoder to a sampler, a per-step logit function, and an optional EOS token."""
        self._sampler = sampler
        self._logit_fn = logit_fn
        self._eos = eos

    def stream(self, request: Request, *, seed: int = 0) -> Iterator[TokenEvent]:
        """Yield a :class:`TokenEvent` per generated token until EOS or the budget is reached."""
        context = tuple(request.prompt)
        for i in range(request.max_tokens):
            token = self._sampler.sample(self._logit_fn(context), seed=seed + i)
            context = (*context, token)
            is_eos = self._eos is not None and token == self._eos
            yield TokenEvent(index=i, token=token, is_last=is_eos or i == request.max_tokens - 1)
            if is_eos:
                return

    def collect(self, request: Request, *, seed: int = 0) -> tuple[int, ...]:
        """Run the stream to completion and return the produced tokens."""
        return tuple(event.token for event in self.stream(request, seed=seed))
