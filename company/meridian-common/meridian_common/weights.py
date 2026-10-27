"""Meridian's weight files: each shard of a model is a safetensors file whose header says what it is.

A shard holds one byte tensor and, in its header's ``__metadata__``, the Meridian model it belongs to, the
model's key, and its place among the model's shards. The model store writes the shards; an inference server
reads each header and checks each file's SHA-256 against the store's manifest. The tensor's bytes are filler,
not weights.
"""

from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass

from meridian_common.errors import MeridianError

LENGTH_FORMAT = "<Q"  # safetensors: the header's length in bytes, a little-endian u64, then the header
LENGTH_BYTES = struct.calcsize(LENGTH_FORMAT)
ALIGNMENT = 8  # the header is padded with spaces to a multiple of this, as safetensors pads it
TENSOR = "model.weight"


class WeightsError(MeridianError):
    """A file that is not a Meridian shard, or whose header is incomplete."""

    code = "weights.invalid"


@dataclass(frozen=True)
class ShardHeader:
    """What a shard's header says: whose shard it is, its model's key, and its place."""

    model: str
    model_key: str
    index: int  # from 1
    count: int

    @property
    def file_name(self) -> str:
        """The shard's file name, as checkpoints shard: ``model-00001-of-00004.safetensors``."""
        return f"model-{self.index:05d}-of-{self.count:05d}.safetensors"


def encode_shard(header: ShardHeader, size: int) -> bytes:
    """The shard ``header`` describes: at most ``size`` bytes, or as few as its header allows.

    The filler is the same for the same model and place, so a shard's checksum is too.
    """
    metadata = {
        "format": "pt",
        "meridian_model": header.model,
        "model_key": header.model_key,
        "shard": str(header.index),
        "shards": str(header.count),
    }

    def encode_header(filler: int) -> bytes:
        tensor = {"dtype": "U8", "shape": [filler], "data_offsets": [0, filler]}
        encoded = json.dumps({"__metadata__": metadata, TENSOR: tensor}, separators=(",", ":")).encode()
        return encoded + b" " * (-len(encoded) % ALIGNMENT)

    # A filler no longer than size has no more digits, so its header is no longer than this one.
    filler = max(size - LENGTH_BYTES - len(encode_header(size)), 1)
    encoded = encode_header(filler)
    seed = f"{header.model}/{header.index}".encode()
    return struct.pack(LENGTH_FORMAT, len(encoded)) + encoded + hashlib.shake_256(seed).digest(filler)


def read_shard_header(data: bytes) -> ShardHeader:
    """The header of the shard ``data``; raises ``WeightsError`` for a file that is not a Meridian shard."""
    if len(data) < LENGTH_BYTES:
        raise WeightsError("too short to be a safetensors file")
    (length,) = struct.unpack_from(LENGTH_FORMAT, data)
    if length > len(data) - LENGTH_BYTES:
        raise WeightsError("the header runs past the end of the file")
    try:
        document = json.loads(data[LENGTH_BYTES : LENGTH_BYTES + length])
        metadata = document["__metadata__"]
        return ShardHeader(
            model=str(metadata["meridian_model"]),
            model_key=str(metadata["model_key"]),
            index=int(metadata["shard"]),
            count=int(metadata["shards"]),
        )
    except (ValueError, KeyError, TypeError) as error:
        raise WeightsError(f"not a Meridian shard header: {error}") from error
