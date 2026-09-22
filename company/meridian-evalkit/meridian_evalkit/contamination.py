"""The eval-corpus contamination metric, delegating to the data platform.

An eval corpus is contaminated when its items appear in the training holdout. This metric reuses the data
platform's contamination surface (:class:`meridian_datapipe.ContaminationFilter` against a
:class:`meridian_datapipe.HoldoutRegistry`) rather than reimplementing the check, so evalkit and datapipe
always agree on which items count as contaminated. :func:`items_to_documents` renders eval items into the
:class:`meridian_datapipe.Document` shape the filter screens.
"""

from __future__ import annotations

from collections.abc import Sequence

from meridian_datapipe import ContaminationFilter, Document, HoldoutRegistry
from meridian_evalkit.harness.model import EvalItem


def item_text(item: EvalItem) -> str:
    """The canonical text an eval item is screened under (its prompt tokens joined by spaces)."""
    return " ".join(str(tok) for tok in item.prompt)


def items_to_documents(items: Sequence[EvalItem]) -> list[Document]:
    """Render eval ``items`` into datapipe documents keyed by item id and canonical prompt text."""
    return [Document(doc_id=item.item_id, text=item_text(item), source="evalkit") for item in items]


def corpus_contamination_rate(docs: Sequence[Document], registry: HoldoutRegistry) -> float:
    """The fraction of ``docs`` present in the holdout, via the data platform's contamination filter."""
    return ContaminationFilter(registry).contamination_rate(list(docs))


def contamination_rate(items: Sequence[EvalItem], registry: HoldoutRegistry) -> float:
    """The contamination rate of an eval-item corpus screened against ``registry``."""
    return corpus_contamination_rate(items_to_documents(items), registry)
