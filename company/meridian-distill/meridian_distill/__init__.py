"""meridian-distill: the Meridian distillation platform.

Owns prompt construction and templating (rendering a :class:`~meridian_datapipe.types.Document` into teacher
prompt tokens), a caching teacher client that runs inference through the meridian-serving stack and records
every real call against a metrics quota, curriculum assembly with a deterministic difficulty ordering, active
data selection over teacher features, and the distillation-data writer with a manifest. The end-to-end
:func:`run_distillation` pipeline consumes datapipe shards and drives the serving stack, so distill couples to
both. It depends on meridian-common, meridian-datapipe, and meridian-serving; its teacher-feature computation
is reused by evalkit.
"""

from __future__ import annotations

__version__ = "0.2.0"

from meridian_distill import (
    curriculum,
    errors,
    prompts,
    select,
    teacher,
    writer,
)
from meridian_distill.curriculum import Curriculum, CurriculumBuilder, assemble_curriculum
from meridian_distill.pipeline import (
    DistillationRun,
    DistillTelemetry,
    run_distillation,
)
from meridian_distill.prompts import PromptTemplate, RenderedPrompt
from meridian_distill.select import Candidate, Selector
from meridian_distill.teacher import (
    TeacherClient,
    TeacherResponse,
    compute_features,
    teacher_features,
)
from meridian_distill.writer import DistillationWriter, DistillDataset, DistillRecord

__all__ = [
    "Candidate",
    "Curriculum",
    "CurriculumBuilder",
    "DistillDataset",
    "DistillRecord",
    "DistillTelemetry",
    "DistillationRun",
    "DistillationWriter",
    "PromptTemplate",
    "RenderedPrompt",
    "Selector",
    "TeacherClient",
    "TeacherResponse",
    "assemble_curriculum",
    "compute_features",
    "curriculum",
    "errors",
    "prompts",
    "run_distillation",
    "select",
    "teacher",
    "teacher_features",
    "writer",
]
