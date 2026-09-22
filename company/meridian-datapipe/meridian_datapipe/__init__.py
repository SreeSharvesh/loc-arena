"""meridian-datapipe: the Meridian data platform.

Owns source ingest and schema normalization (raw records and JSONL into :class:`Document` values), a
deterministic word/byte tokenizer with a vocabulary and a corpus tokenization step, exact and near-duplicate
deduplication (an exhaustive pairwise baseline plus MinHash + LSH), a contamination filter against a sharded
holdout registry, deterministic sharding with a manifest registry, quality gates, and a streaming loader with
prefetch. It depends only on meridian-common; its shards feed distill and its contamination metric feeds
evalkit.
"""

from __future__ import annotations

__version__ = "0.3.0"

from meridian_datapipe import (
    contamination,
    dedup,
    ingest,
    loader,
    quality,
    shard,
    tokenize,
    types,
)
from meridian_datapipe.contamination import ContaminationFilter, HoldoutRegistry
from meridian_datapipe.dedup import DedupResult, exact_dedup, lsh_dedup
from meridian_datapipe.dedup import dedup as near_dedup
from meridian_datapipe.ingest import read_jsonl, read_records
from meridian_datapipe.loader import StreamingLoader
from meridian_datapipe.quality import QualityGate, QualityReport
from meridian_datapipe.shard import ManifestRegistry, Sharder, shard_documents
from meridian_datapipe.tokenize import Tokenizer, Vocabulary
from meridian_datapipe.types import Document, Manifest, Shard

__all__ = [
    "ContaminationFilter",
    "DedupResult",
    "Document",
    "HoldoutRegistry",
    "Manifest",
    "ManifestRegistry",
    "QualityGate",
    "QualityReport",
    "Shard",
    "Sharder",
    "StreamingLoader",
    "Tokenizer",
    "Vocabulary",
    "contamination",
    "dedup",
    "exact_dedup",
    "ingest",
    "loader",
    "lsh_dedup",
    "near_dedup",
    "quality",
    "read_jsonl",
    "read_records",
    "shard",
    "shard_documents",
    "tokenize",
    "types",
]
