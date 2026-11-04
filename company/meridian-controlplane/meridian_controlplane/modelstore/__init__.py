"""The model store: the weights of each model Meridian serves, as shards an inference server downloads.

``store`` writes and holds them, and ``service`` the HTTP API and the MCP tools over a store, authorized by
the central authority a deployment hands in (``Authority``, shared with the tickets service).
"""

from __future__ import annotations

from meridian_controlplane.modelstore.service import SERVICE, build_modelstore_server, create_modelstore_api
from meridian_controlplane.modelstore.store import Manifest, ModelStore, ModelStoreError, ShardEntry, Sizes

__all__ = [
    "SERVICE",
    "Manifest",
    "ModelStore",
    "ModelStoreError",
    "ShardEntry",
    "Sizes",
    "build_modelstore_server",
    "create_modelstore_api",
]
