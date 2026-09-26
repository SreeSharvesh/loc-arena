"""Deterministic beam search decoding.

Beam search keeps the ``beam_width`` highest-scoring partial sequences at each step, expands each by the
top candidate continuations, and keeps the best expansions. Scores are summed log-probabilities with an
optional length penalty, so a caller trades completeness for cost. Fully deterministic given the score
function; no sampling RNG is involved.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from meridian_serving.errors import SamplerError
from meridian_serving.sampler.sampling import softmax

# A score function: given the current token sequence, return the next-token logit vector.
ScoreFn = Callable[[tuple[int, ...]], list[float]]


@dataclass(frozen=True)
class Beam:
    """One partial hypothesis: its token sequence and cumulative log-probability."""

    tokens: tuple[int, ...]
    logprob: float
    finished: bool = False

    def score(self, length_penalty: float) -> float:
        """The length-normalized score (``logprob / len^length_penalty``)."""
        length = max(1, len(self.tokens))
        return self.logprob / float(length**length_penalty)


@dataclass
class BeamSearchResult:
    """The finished beams sorted best-first, plus how many steps ran."""

    beams: list[Beam] = field(default_factory=list)
    steps: int = 0

    def best(self, length_penalty: float = 1.0) -> Beam:
        """The highest-scoring beam."""
        if not self.beams:
            raise SamplerError("no beams produced")
        return max(self.beams, key=lambda b: b.score(length_penalty))


class BeamSearch:
    """Runs deterministic beam search with a configurable width and length penalty."""

    def __init__(self, *, beam_width: int = 4, max_steps: int = 32, length_penalty: float = 1.0) -> None:
        """Hold the beam width, the step cap, and the length penalty."""
        if beam_width < 1:
            raise SamplerError("beam_width must be >= 1")
        self._width = beam_width
        self._max_steps = max_steps
        self._length_penalty = length_penalty

    def search(
        self,
        prompt: tuple[int, ...],
        score_fn: ScoreFn,
        *,
        eos: int | None = None,
    ) -> BeamSearchResult:
        """Search from ``prompt`` using ``score_fn``, stopping a beam at ``eos`` or the step cap."""
        beams = [Beam(tokens=prompt, logprob=0.0)]
        finished: list[Beam] = []
        steps = 0
        for _ in range(self._max_steps):
            steps += 1
            candidates: list[Beam] = []
            for beam in beams:
                if beam.finished:
                    finished.append(beam)
                    continue
                probs = softmax(score_fn(beam.tokens))
                ranked = sorted(range(len(probs)), key=lambda i: (probs[i], -i), reverse=True)
                for token in ranked[: self._width]:
                    lp = beam.logprob + math.log(probs[token] + 1e-12)
                    is_eos = eos is not None and token == eos
                    candidates.append(Beam(tokens=(*beam.tokens, token), logprob=lp, finished=is_eos))
            if not candidates:
                break
            candidates.sort(key=lambda b: b.score(self._length_penalty), reverse=True)
            beams = candidates[: self._width]
            if all(b.finished for b in beams):
                finished.extend(beams)
                break
        finished.extend(b for b in beams if not b.finished)
        finished.sort(key=lambda b: b.score(self._length_penalty), reverse=True)
        return BeamSearchResult(beams=finished[: self._width], steps=steps)
