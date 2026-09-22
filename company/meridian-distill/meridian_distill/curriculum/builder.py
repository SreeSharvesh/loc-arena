"""Curriculum assembly with a deterministic difficulty ordering.

A :class:`Curriculum` is an ordered list of documents to distill, easiest first. Difficulty is a deterministic
score over a document -- its text length by default, or a numeric ``meta`` field -- with the document id as a
stable tie-break, so the same corpus always assembles to the same order regardless of input order. The builder
can assemble from a raw document list or from a datapipe :class:`~meridian_datapipe.types.Shard` and a lookup.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass

from meridian_datapipe.types import Document, Shard
from meridian_distill.errors import CurriculumError

DifficultyFn = Callable[[Document], float]


def _length_difficulty(document: Document) -> float:
    """Difficulty as the document's character length."""
    return float(document.length)


def _meta_difficulty(field: str) -> DifficultyFn:
    """A difficulty function reading a numeric ``meta`` field (rejects non-numeric values)."""

    def score(document: Document) -> float:
        value = document.meta.get(field, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CurriculumError(
                "difficulty meta field is not numeric", code="distill.curriculum", field=field
            )
        return float(value)

    return score


@dataclass(frozen=True)
class CurriculumItem:
    """One curriculum entry: the document and its computed difficulty score."""

    document: Document
    difficulty: float

    @property
    def doc_id(self) -> str:
        """The underlying document id."""
        return self.document.doc_id


@dataclass(frozen=True)
class Curriculum:
    """An ordered curriculum, easiest item first."""

    items: tuple[CurriculumItem, ...]

    def __len__(self) -> int:
        """The number of curriculum items."""
        return len(self.items)

    def __iter__(self) -> Iterator[CurriculumItem]:
        """Iterate items in curriculum (easiest-first) order."""
        return iter(self.items)

    @property
    def documents(self) -> tuple[Document, ...]:
        """The curriculum documents in order."""
        return tuple(item.document for item in self.items)


class CurriculumBuilder:
    """Assembles a :class:`Curriculum` from documents using a deterministic difficulty ordering."""

    def __init__(self, difficulty: DifficultyFn | None = None) -> None:
        """Configure the difficulty function (defaults to document length)."""
        self._difficulty: DifficultyFn = difficulty if difficulty is not None else _length_difficulty

    @classmethod
    def by_meta(cls, field: str) -> CurriculumBuilder:
        """A builder that scores difficulty from a numeric ``meta`` field."""
        return cls(_meta_difficulty(field))

    def build(self, documents: Sequence[Document]) -> Curriculum:
        """Assemble ``documents`` into a curriculum ordered by ascending difficulty then doc id."""
        if not documents:
            raise CurriculumError("cannot assemble a curriculum from no documents", code="distill.curriculum")
        scored = [CurriculumItem(document=doc, difficulty=self._difficulty(doc)) for doc in documents]
        scored.sort(key=lambda item: (item.difficulty, item.doc_id))
        return Curriculum(items=tuple(scored))

    def from_shard(self, shard: Shard, documents: Mapping[str, Document]) -> Curriculum:
        """Assemble a curriculum from the members of a datapipe ``shard`` via a document lookup."""
        members: list[Document] = []
        for doc_id in shard.doc_ids:
            if doc_id not in documents:
                raise CurriculumError(
                    "shard member is not in the document lookup", code="distill.curriculum", doc_id=doc_id
                )
            members.append(documents[doc_id])
        return self.build(members)


def assemble_curriculum(
    documents: Sequence[Document],
    difficulty: DifficultyFn | None = None,
) -> Curriculum:
    """Convenience: assemble a curriculum with a one-shot :class:`CurriculumBuilder`."""
    return CurriculumBuilder(difficulty).build(documents)
