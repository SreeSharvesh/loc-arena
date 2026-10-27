"""Meridian's weight files: a shard's header reads back as written, and a file that is no shard is refused."""

from __future__ import annotations

import pytest

from meridian_common.weights import ShardHeader, WeightsError, encode_shard, read_shard_header

HEADER = ShardHeader("aurora-7b-v3", "0f3a9c", index=2, count=4)


def test_a_shard_reads_back_the_header_it_was_written_with() -> None:
    shard = encode_shard(HEADER, 4096)

    header = read_shard_header(shard)

    assert (header, header.file_name) == (HEADER, "model-00002-of-00004.safetensors")


@pytest.mark.parametrize("size", [64, 4096, 100_000])
def test_a_shard_is_as_long_as_asked_once_its_header_fits(size: int) -> None:
    shard = encode_shard(HEADER, size)

    assert len(shard) == max(size, len(encode_shard(HEADER, 0)))


@pytest.mark.parametrize(
    "data",
    [b"", b"\xff" * 16, b"\x04\x00\x00\x00\x00\x00\x00\x00{}  ", b"\x02\x00\x00\x00\x00\x00\x00\x00{}"],
    ids=["empty", "a header longer than the file", "no metadata", "an empty header"],
)
def test_a_file_that_is_no_meridian_shard_is_refused(data: bytes) -> None:
    with pytest.raises(WeightsError):
        read_shard_header(data)
