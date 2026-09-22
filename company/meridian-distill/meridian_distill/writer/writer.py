"""The distillation-data writer and dataset assembly.

A :class:`DistillRecord` bundles one training example -- the source document, its rendered prompt tokens, the
teacher's generated tokens, and the teacher features -- and the :class:`DistillationWriter` assembles records
into a :class:`DistillDataset` with a manifest checksum. Serialization is canonical (via
:mod:`meridian_common.serde`), so a dataset built twice from the same records has identical records and an
identical checksum, and a duplicate record id is rejected.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from meridian_common.serde.canonical import fingerprint
from meridian_datapipe.types import Document
from meridian_distill.errors import WriterError
from meridian_distill.prompts.template import RenderedPrompt
from meridian_distill.teacher.response import TeacherResponse


@dataclass(frozen=True)
class DistillRecord:
    """One distillation example: the source document, the prompt, the teacher tokens, and the features."""

    doc_id: str
    source: str
    prompt_tokens: tuple[int, ...]
    teacher_tokens: tuple[int, ...]
    features: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        """A canonical, JSON-friendly mapping for this record."""
        return {
            "doc_id": self.doc_id,
            "source": self.source,
            "prompt_tokens": list(self.prompt_tokens),
            "teacher_tokens": list(self.teacher_tokens),
            "features": list(self.features),
        }


@dataclass(frozen=True)
class DistillDataset:
    """An assembled distillation dataset: the records, their count, and a manifest checksum."""

    records: tuple[DistillRecord, ...]
    num_records: int
    checksum: str

    def __len__(self) -> int:
        """The number of records."""
        return len(self.records)

    def to_dict(self) -> dict[str, Any]:
        """A canonical, JSON-friendly mapping for the whole dataset."""
        return {
            "records": [record.to_dict() for record in self.records],
            "num_records": self.num_records,
            "checksum": self.checksum,
        }


class DistillationWriter:
    """Builds :class:`DistillRecord` values and assembles them into a checksummed dataset."""

    @staticmethod
    def record(document: Document, prompt: RenderedPrompt, response: TeacherResponse) -> DistillRecord:
        """Build one record from a document, its rendered prompt, and the teacher response."""
        return DistillRecord(
            doc_id=document.doc_id,
            source=document.source,
            prompt_tokens=prompt.token_ids,
            teacher_tokens=response.tokens,
            features=response.features,
        )

    def write(self, records: Sequence[DistillRecord]) -> DistillDataset:
        """Assemble ``records`` into a dataset, rejecting duplicate doc ids and checksumming the result."""
        seen: set[str] = set()
        ordered: list[DistillRecord] = []
        for rec in records:
            if rec.doc_id in seen:
                raise WriterError("duplicate record id", code="distill.writer", doc_id=rec.doc_id)
            seen.add(rec.doc_id)
            ordered.append(rec)
        checksum = fingerprint([rec.to_dict() for rec in ordered])
        return DistillDataset(records=tuple(ordered), num_records=len(ordered), checksum=checksum)
