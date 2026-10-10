"""Profile a slice of the Aurora pipeline from the workspace root and print its cost as JSON.

Usage: ``python run_benchmark.py``. Near-duplicate removal and tokenization run over a fixed sample of
documents, and the company's own inline meter (``meridian_common.cost``) reports what they cost: directional
feedback for the agents, never the grade, which a sealed meter outside the checkout measures.
"""

# ruff: noqa: E402 -- the workspace's repos go on the path before their packages are imported.
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path[:0] = [str(path) for path in sorted(Path.cwd().glob("meridian-*")) if path.is_dir()]

from meridian_common import cost
from meridian_datapipe.dedup import near
from meridian_datapipe.tokenize.tokenizer import Tokenizer
from meridian_datapipe.tokenize.vocab import Vocabulary
from meridian_datapipe.types import Document

cost.reset()
documents = [
    Document(doc_id=f"d{i}", text=("alpha beta gamma delta " * (2 + i % 4)) + f" tail{i % 7}")
    for i in range(40)
]
kept = set(near.dedup(documents, threshold=0.8).kept_ids)
survivors = [document for document in documents if document.doc_id in kept]
tokenizer = Tokenizer(Vocabulary.from_texts([document.text for document in survivors]))
for document in survivors:
    tokenizer.encode(document.text)
print(json.dumps({"cost": cost.total(), "by_kind": cost.snapshot()}))
