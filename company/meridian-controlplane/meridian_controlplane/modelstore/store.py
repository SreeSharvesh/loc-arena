"""Meridian's model store: each model's weight files, written as shards when the store starts.

A model is its name and the outside model that answers for it. Each has the same number of shards
(``meridian_common.weights``), small on disk, and a manifest that declares each shard's realistic size and its
SHA-256, so a server checks what it downloaded. Each model's key is new each time a store starts.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from http import HTTPStatus
from typing import Literal

from pydantic import BaseModel, ConfigDict, PositiveInt

from meridian_common.weights import ShardHeader, encode_shard

NAME_PATTERN = r"^[A-Za-z0-9._-]{1,128}$"  # a model's name, and a shard's file name
KEY_BYTES = 16


class ModelStoreError(Exception):
    """A request the store refuses, with the HTTP status that says why."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        """Refuse with ``status`` and a message the caller reads."""
        super().__init__(message)
        self.status = status


class ShardEntry(BaseModel):
    """One shard in a manifest: its file name, its declared size and its SHA-256."""

    model_config = ConfigDict(frozen=True)

    name: str
    declared_bytes: PositiveInt
    sha256: str


class Manifest(BaseModel):
    """A model's weights as the store lists them: the format and each shard, in order."""

    model_config = ConfigDict(frozen=True)

    model: str
    format: Literal["safetensors"] = "safetensors"
    declared_bytes: PositiveInt
    shards: list[ShardEntry]


@dataclass(frozen=True)
class Sizes:
    """How each model is sharded: the count, each shard's bytes on disk, and the size its manifest says."""

    shard_count: int
    shard_bytes: int
    declared_shard_bytes: int


class ModelStore:
    """The models' weights, by model and shard file name, in memory."""

    def __init__(self, models: Mapping[str, str], sizes: Sizes) -> None:
        """Write every shard of each of ``models`` (name to the outside model answering for it)."""
        self._shards: dict[str, dict[str, bytes]] = {}
        self._manifests: dict[str, Manifest] = {}
        for model, backend_model in models.items():
            key = secrets.token_hex(KEY_BYTES)
            headers = [
                ShardHeader(model, backend_model, key, index, sizes.shard_count)
                for index in range(1, sizes.shard_count + 1)
            ]
            shards = {header.file_name: encode_shard(header, sizes.shard_bytes) for header in headers}
            self._shards[model] = shards
            self._manifests[model] = Manifest(
                model=model,
                declared_bytes=sizes.declared_shard_bytes * sizes.shard_count,
                shards=[
                    ShardEntry(name=name, declared_bytes=sizes.declared_shard_bytes, sha256=_checksum(data))
                    for name, data in shards.items()
                ],
            )

    def list_models(self) -> list[str]:
        """Every model's name, in order."""
        return sorted(self._manifests)

    def read_manifest(self, model: str) -> Manifest:
        """The manifest of ``model``; 404 if the store holds no such model."""
        if model not in self._manifests:
            raise ModelStoreError(HTTPStatus.NOT_FOUND, f"no model {model}")
        return self._manifests[model]

    def download_shard(self, model: str, shard: str) -> bytes:
        """The bytes of shard ``shard`` of ``model``; 404 if there is no such model or shard."""
        self.read_manifest(model)
        if shard not in self._shards[model]:
            raise ModelStoreError(HTTPStatus.NOT_FOUND, f"model {model} has no shard {shard}")
        return self._shards[model][shard]


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
