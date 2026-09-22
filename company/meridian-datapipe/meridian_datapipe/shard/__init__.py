"""Deterministic sharding and a manifest registry over the resulting shards."""

from __future__ import annotations

from meridian_datapipe.shard.manifest import ManifestRegistry
from meridian_datapipe.shard.sharder import Sharder, assign_shard, shard_documents

__all__ = [
    "ManifestRegistry",
    "Sharder",
    "assign_shard",
    "shard_documents",
]
