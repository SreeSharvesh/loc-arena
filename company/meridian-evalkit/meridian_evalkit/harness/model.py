"""Eval item and model value types for the benchmark harness.

An :class:`EvalItem` is one benchmark question: a stable id, the prompt token ids fed to the model, the
reference (gold) token ids it is scored against, a category label for stratified reporting, and free-form
metadata. A :class:`Prediction` is a model's answer to an item: the generated token ids and a confidence in
``[0, 1]`` used by calibration metrics. :class:`Model` is the protocol a harness drives, and
:class:`DeterministicModel` is a dependency-free reference model whose accuracy and confidence are fixed by a
seed so tests are reproducible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from meridian_evalkit.errors import HarnessError


@dataclass(frozen=True)
class EvalItem:
    """One benchmark item: its id, prompt tokens, reference tokens, category, and metadata."""

    item_id: str
    prompt: tuple[int, ...]
    reference: tuple[int, ...]
    category: str = "default"
    meta: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze ``meta`` behind a read-only view so the frozen item is truly immutable."""
        object.__setattr__(self, "meta", MappingProxyType(dict(self.meta)))

    @property
    def prompt_len(self) -> int:
        """The number of prompt tokens."""
        return len(self.prompt)

    @property
    def reference_len(self) -> int:
        """The number of reference tokens."""
        return len(self.reference)


@dataclass(frozen=True)
class Prediction:
    """A model's answer to an item: the generated tokens and a confidence in ``[0, 1]``."""

    item_id: str
    tokens: tuple[int, ...]
    confidence: float = 1.0

    def __post_init__(self) -> None:
        """Reject a confidence outside ``[0, 1]``."""
        if not 0.0 <= self.confidence <= 1.0:
            raise HarnessError(
                "confidence must be within [0, 1]",
                code="evalkit.harness",
                confidence=self.confidence,
            )

    @property
    def generated_len(self) -> int:
        """The number of generated tokens."""
        return len(self.tokens)


@runtime_checkable
class Model(Protocol):
    """A model the harness can drive: it maps an :class:`EvalItem` to a :class:`Prediction`."""

    def predict(self, item: EvalItem) -> Prediction:
        """Answer ``item`` with a prediction."""
        ...


class DeterministicModel:
    """A dependency-free reference model with a fixed, seed-controlled accuracy and confidence.

    The model answers an item correctly (echoing the reference tokens) unless the item's id hashes into the
    error band implied by ``accuracy``; a wrong answer perturbs the reference tokens. Confidence is a stable
    function of the item id, so calibration metrics see a spread rather than a constant.
    """

    def __init__(self, *, accuracy: float = 1.0, seed: int = 0) -> None:
        """Fix the target accuracy in ``[0, 1]`` and a seed that decorrelates items."""
        if not 0.0 <= accuracy <= 1.0:
            raise HarnessError("accuracy must be within [0, 1]", code="evalkit.harness", accuracy=accuracy)
        self._accuracy = accuracy
        self._seed = seed

    def _roll(self, item_id: str) -> float:
        """A stable pseudo-uniform draw in ``[0, 1)`` for an item id."""
        acc = self._seed & 0xFFFFFFFF
        for ch in item_id:
            acc = (acc * 1099511628211 + ord(ch)) & 0xFFFFFFFFFFFFFFFF
        return (acc % 1_000_000) / 1_000_000.0

    def predict(self, item: EvalItem) -> Prediction:
        """Answer ``item``: correct when the item's draw falls under the target accuracy."""
        draw = self._roll(item.item_id)
        correct = draw < self._accuracy
        if correct:
            tokens = item.reference
            confidence = 0.5 + 0.5 * (1.0 - draw)
        else:
            tokens = tuple((t + 1) % 32 for t in item.reference) if item.reference else (0,)
            confidence = 0.5 + 0.5 * draw
        return Prediction(item_id=item.item_id, tokens=tokens, confidence=round(confidence, 12))
