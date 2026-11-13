"""The model store's shards on disk: written once, and served again by a store that starts on them."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from meridian_controlplane.modelstore import ModelStore, Sizes

SIZES = Sizes(shard_count=2, shard_bytes=512, declared_shard_bytes=1_000)


def test_a_store_opened_again_on_its_directory_serves_the_shards_the_first_one_wrote(tmp_path: Path) -> None:
    first = ModelStore(tmp_path, ["aurora-7b-v3"], SIZES)
    manifest = first.read_manifest("aurora-7b-v3")

    again = ModelStore(tmp_path, ["aurora-7b-v3", "aurora-13b-v2"], SIZES)

    downloaded = [again.download_shard("aurora-7b-v3", shard.name) for shard in manifest.shards]
    assert again.read_manifest("aurora-7b-v3") == manifest
    assert [hashlib.sha256(shard).hexdigest() for shard in downloaded] == [s.sha256 for s in manifest.shards]
    assert again.list_models() == ["aurora-13b-v2", "aurora-7b-v3"]


@pytest.mark.parametrize("model", ["..", "a/b", ""])
def test_a_store_refuses_a_model_whose_name_is_no_directory_of_its_own(tmp_path: Path, model: str) -> None:
    with pytest.raises(ValueError, match="is no model's name"):
        ModelStore(tmp_path / "store", [model], SIZES)

    assert not list(tmp_path.rglob("*.safetensors"))
