"""Meridian's model store: each model's weight files, written as shards the first time a store holds it.

A model is its Meridian name. Each has the same number of shards
(``meridian_common.weights``), small on disk, and a manifest that declares each shard's realistic size and its
SHA-256, so a server checks what it downloaded. A model's key is new when its shards are written, and a store
that starts again on the same directory serves the same shards.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
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
    """The models' weights, by model and shard file name, as files under one directory."""

    def __init__(self, directory: Path, models: Iterable[str], sizes: Sizes) -> None:
        """Serve each of ``models`` from ``directory``, writing the shards of one it does not hold yet.

        A model's shards are written aside and moved into place together, so a store that starts again
        finds every shard of a model or none, and serves the ones it finds under the key they were written
        with.
        """
        self._directory = directory
        self._manifests: dict[str, Manifest] = {}
        directory.mkdir(parents=True, exist_ok=True)
        for model in models:
            if not re.fullmatch(NAME_PATTERN, model) or model in {".", ".."}:
                raise ValueError(f"{model!r} is no model's name: it names the directory of its shards")
            if not (directory / model).exists():
                with tempfile.TemporaryDirectory(dir=directory) as building:
                    key, written = secrets.token_hex(KEY_BYTES), Path(building) / model
                    written.mkdir()
                    for index in range(1, sizes.shard_count + 1):
                        header = ShardHeader(model, key, index, sizes.shard_count)
                        (written / header.file_name).write_bytes(encode_shard(header, sizes.shard_bytes))
                    written.rename(directory / model)
            self._manifests[model] = Manifest(
                model=model,
                declared_bytes=sizes.declared_shard_bytes * sizes.shard_count,
                shards=[
                    ShardEntry(
                        name=shard.name,
                        declared_bytes=sizes.declared_shard_bytes,
                        sha256=hashlib.sha256(shard.read_bytes()).hexdigest(),
                    )
                    for shard in sorted((directory / model).iterdir())
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
        if shard not in {entry.name for entry in self.read_manifest(model).shards}:
            raise ModelStoreError(HTTPStatus.NOT_FOUND, f"model {model} has no shard {shard}")
        return (self._directory / model / shard).read_bytes()  # both names are the manifest's own
