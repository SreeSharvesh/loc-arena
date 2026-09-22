"""The end-to-end distillation pipeline.

:func:`run_distillation` ties the platform together: it shards a corpus with meridian-datapipe, assembles a
difficulty-ordered curriculum per shard, renders a prompt for each document, queries the teacher through the
serving stack (meridian-serving), optionally narrows the pool with active selection, and writes the resulting
dataset. It returns the dataset plus a telemetry summary whose teacher-call count comes from the client's
metrics counter, so the datapipe and serving couplings are exercised together in one deterministic run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from meridian_datapipe.shard import shard_documents
from meridian_datapipe.types import Document, Manifest
from meridian_distill.curriculum.builder import CurriculumBuilder
from meridian_distill.errors import DistillError
from meridian_distill.prompts.template import PromptTemplate
from meridian_distill.select.selector import Candidate, Selector
from meridian_distill.teacher.client import TeacherClient
from meridian_distill.writer.writer import DistillationWriter, DistillDataset, DistillRecord


@dataclass(frozen=True)
class DistillTelemetry:
    """Cost and coverage summary for a distillation run."""

    num_documents: int
    num_records: int
    num_shards: int
    teacher_calls: int
    cache_hits: int
    manifest_checksum: str


@dataclass(frozen=True)
class DistillationRun:
    """The outcome of a distillation run: the dataset, the telemetry, and the sharding manifest."""

    dataset: DistillDataset
    telemetry: DistillTelemetry
    manifest: Manifest


def run_distillation(
    documents: Sequence[Document],
    teacher: TeacherClient,
    *,
    template: PromptTemplate | None = None,
    num_shards: int = 1,
    few_shot: int = 0,
    curriculum: CurriculumBuilder | None = None,
    selector: Selector | None = None,
    select_k: int | None = None,
) -> DistillationRun:
    """Run the full distill pipeline over ``documents`` and return the dataset plus telemetry."""
    if not documents:
        raise DistillError("cannot distill an empty corpus", code="distill.error")
    if few_shot < 0:
        raise DistillError("few_shot must be non-negative", code="distill.error", few_shot=few_shot)

    tmpl = template if template is not None else PromptTemplate()
    builder = curriculum if curriculum is not None else CurriculumBuilder()
    writer = DistillationWriter()

    manifest = shard_documents(list(documents), num_shards)
    docs_by_id = {doc.doc_id: doc for doc in documents}

    records: list[DistillRecord] = []
    candidates: list[Candidate] = []
    for shard in manifest.shards:
        if not shard.doc_ids:
            continue
        shard_curriculum = builder.from_shard(shard, docs_by_id)
        examples = list(shard_curriculum.documents[:few_shot])
        for item in shard_curriculum:
            few_shot_context = [ex for ex in examples if ex.doc_id != item.doc_id]
            prompt = tmpl.render(item.document, few_shot_context)
            response = teacher.query(prompt.token_ids)
            records.append(writer.record(item.document, prompt, response))
            candidates.append(Candidate(item_id=item.doc_id, features=response.features))

    if selector is not None and select_k is not None:
        keep = {cand.item_id for cand in selector.select(candidates, select_k)}
        records = [rec for rec in records if rec.doc_id in keep]

    dataset = writer.write(records)
    telemetry = DistillTelemetry(
        num_documents=len(documents),
        num_records=dataset.num_records,
        num_shards=manifest.num_shards,
        teacher_calls=teacher.calls,
        cache_hits=teacher.cache_hits,
        manifest_checksum=manifest.checksum,
    )
    return DistillationRun(dataset=dataset, telemetry=telemetry, manifest=manifest)
