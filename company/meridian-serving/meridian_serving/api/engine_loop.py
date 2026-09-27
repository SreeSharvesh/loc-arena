"""The continuous-batching serving loop.

The engine runs in discrete steps. Each step it admits waiting requests up to a token budget, advances every
running request by one decode token, retires finished requests, and records per-step stats. This is the
continuous-batching pattern: new requests join the running batch as soon as there is room, rather than waiting
for the whole batch to finish. Deterministic given the inputs and the logit function.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from meridian_serving.api.stats import ServingStats
from meridian_serving.sampler.sampling import Sampler
from meridian_serving.scheduler.batch import ContinuousBatcher
from meridian_serving.types import Request, Response

LogitFn = Callable[[tuple[int, ...]], list[float]]


@dataclass
class _Running:
    request: Request
    context: list[int]
    produced: list[int] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return len(self.produced) >= self.request.max_tokens


@dataclass
class StepReport:
    """What one engine step did: admitted, still running, and finished this step."""

    step: int
    admitted: int
    running: int
    finished: tuple[str, ...]


class EngineLoop:
    """A continuous-batching decode loop over admission, batching, sampling, and stats."""

    def __init__(
        self,
        logit_fn: LogitFn,
        *,
        sampler: Sampler | None = None,
        step_token_budget: int = 4096,
        max_running: int = 64,
        seed: int = 0,
    ) -> None:
        """Wire the loop to a logit function, a sampler, and the per-step admission budget."""
        self._logit_fn = logit_fn
        self._sampler = sampler if sampler is not None else Sampler(temperature=1.0, top_p=0.95)
        self._batcher = ContinuousBatcher(step_token_budget, max_batch=max_running)
        self._max_running = max_running
        self._seed = seed
        self._waiting: list[Request] = []
        self._running: list[_Running] = []
        self._responses: dict[str, Response] = {}
        self.stats = ServingStats()
        self._step = 0

    def submit(self, request: Request) -> None:
        """Enqueue ``request`` for admission on a future step."""
        self._waiting.append(request)

    def _admit(self) -> int:
        room = self._max_running - len(self._running)
        if room <= 0 or not self._waiting:
            return 0
        admit, deferred = self._batcher.fill(self._waiting[:room])
        self._waiting = deferred + self._waiting[room:]
        for req in admit:
            self._running.append(_Running(request=req, context=list(req.prompt)))
        return len(admit)

    def step(self) -> StepReport:
        """Run one engine step: admit, advance each running request by one token, retire the finished."""
        self._step += 1
        admitted = self._admit()
        finished: list[str] = []
        for run in self._running:
            token = self._sampler.sample(
                self._logit_fn(tuple(run.context)),
                seed=self._seed + len(run.produced),
            )
            run.context.append(token)
            run.produced.append(token)
        still: list[_Running] = []
        for run in self._running:
            if run.done:
                self._responses[run.request.request_id] = Response(
                    request_id=run.request.request_id,
                    tokens=tuple(run.produced),
                    prompt_len=run.request.prompt_len,
                )
                self.stats.record(
                    prompt_len=run.request.prompt_len,
                    generated=len(run.produced),
                    padded=run.request.prompt_len,
                    cache_hit=False,
                    seconds=0.0,
                )
                finished.append(run.request.request_id)
            else:
                still.append(run)
        self._running = still
        return StepReport(
            step=self._step,
            admitted=admitted,
            running=len(self._running),
            finished=tuple(finished),
        )

    def run_to_completion(self, *, max_steps: int = 100000) -> dict[str, Response]:
        """Step until every submitted request has finished (or the step cap is hit)."""
        steps = 0
        while (self._waiting or self._running) and steps < max_steps:
            self.step()
            steps += 1
        return dict(self._responses)

    @property
    def pending(self) -> int:
        """How many requests are waiting or running."""
        return len(self._waiting) + len(self._running)
