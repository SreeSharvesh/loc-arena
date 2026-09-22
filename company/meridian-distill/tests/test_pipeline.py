from __future__ import annotations

from meridian_datapipe.shard import shard_documents
from meridian_datapipe.types import Document
from meridian_distill.pipeline import run_distillation
from meridian_distill.select import Selector
from meridian_distill.teacher import TeacherClient
from meridian_serving.api.serve import ServingEngine


def _corpus(n: int) -> list[Document]:
    return [Document(f"d{i}", f"document body number {i} " * (i + 1)) for i in range(n)]


def test_pipeline_consumes_shards_and_serving() -> None:
    docs = _corpus(6)
    client = TeacherClient(ServingEngine(), max_tokens=4)
    run = run_distillation(docs, client, num_shards=3)
    # Every document was distilled through the serving-backed teacher.
    assert run.dataset.num_records == 6
    assert run.telemetry.teacher_calls == 6
    # The pipeline sharded the corpus with datapipe and reports that manifest.
    assert run.manifest.num_shards == 3
    assert run.manifest.checksum == shard_documents(docs, 3).checksum
    assert run.telemetry.manifest_checksum == run.manifest.checksum


def test_pipeline_is_deterministic() -> None:
    docs = _corpus(5)
    a = run_distillation(docs, TeacherClient(ServingEngine(), max_tokens=4), num_shards=2)
    b = run_distillation(docs, TeacherClient(ServingEngine(), max_tokens=4), num_shards=2)
    assert a.dataset.checksum == b.dataset.checksum


def test_pipeline_selection_narrows_the_dataset() -> None:
    docs = _corpus(6)
    client = TeacherClient(ServingEngine(), max_tokens=4)
    run = run_distillation(
        docs,
        client,
        num_shards=2,
        selector=Selector("coverage"),
        select_k=3,
    )
    assert run.dataset.num_records == 3
    # Selection narrows the written dataset but the teacher was still queried for the whole pool.
    assert run.telemetry.teacher_calls == 6


def test_pipeline_few_shot_still_covers_every_document() -> None:
    docs = _corpus(4)
    client = TeacherClient(ServingEngine(), max_tokens=4)
    run = run_distillation(docs, client, num_shards=1, few_shot=2)
    assert run.dataset.num_records == 4
    assert {rec.doc_id for rec in run.dataset.records} == {d.doc_id for d in docs}
