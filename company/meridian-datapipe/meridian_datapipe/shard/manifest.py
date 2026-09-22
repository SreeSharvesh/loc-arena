"""A registry of sharding manifests, keyed by dataset name, with membership lookup and verification.

The registry records the :class:`Manifest` produced for each named dataset so downstream consumers (for
example distill, which reads shards) can locate the shard holding a given document and verify a manifest's
checksums before trusting it. Registration rejects an inconsistent manifest, so a stored manifest is
always self-consistent.
"""

from __future__ import annotations

from meridian_common.serde.canonical import fingerprint
from meridian_datapipe.errors import ShardError
from meridian_datapipe.types import Manifest


def _shard_checksum(doc_ids: tuple[str, ...]) -> str:
    """The expected membership checksum for a shard's doc ids (stable under order)."""
    return fingerprint(sorted(doc_ids))


class ManifestRegistry:
    """Stores sharding manifests by dataset name and answers membership and verification queries."""

    def __init__(self) -> None:
        """Start with no registered manifests."""
        self._manifests: dict[str, Manifest] = {}

    def __len__(self) -> int:
        """The number of registered datasets."""
        return len(self._manifests)

    def __contains__(self, name: str) -> bool:
        """Whether a manifest is registered under ``name``."""
        return name in self._manifests

    def register(self, name: str, manifest: Manifest) -> None:
        """Register ``manifest`` under ``name`` after checking it is internally consistent."""
        if not self.verify(manifest):
            raise ShardError("manifest failed checksum verification", code="datapipe.shard", dataset=name)
        self._manifests[name] = manifest

    def get(self, name: str) -> Manifest:
        """The manifest registered under ``name`` (raises :class:`ShardError` if absent)."""
        if name not in self._manifests:
            raise ShardError("no manifest registered", code="datapipe.shard", dataset=name)
        return self._manifests[name]

    def locate(self, name: str, doc_id: str) -> int:
        """The shard index holding ``doc_id`` in dataset ``name`` (raises if the doc is not recorded)."""
        manifest = self.get(name)
        for shard in manifest.shards:
            if doc_id in shard:
                return shard.shard_id
        raise ShardError("document not recorded in manifest", code="datapipe.shard", doc_id=doc_id)

    def verify(self, manifest: Manifest) -> bool:
        """Whether every shard checksum and the top-level checksum recompute to the recorded values."""
        if manifest.num_shards != len(manifest.shards):
            return False
        for i, shard in enumerate(manifest.shards):
            if shard.shard_id != i:
                return False
            if shard.checksum != _shard_checksum(shard.doc_ids):
                return False
        expected = fingerprint([shard.checksum for shard in manifest.shards])
        return manifest.checksum == expected
