"""The structured reader refuses host path redirection before starting a model process."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.insights import analyze_run

from tests._stored_run import unpack_stored_run


@pytest.mark.parametrize("target", ["readings", "readings/insights.json", "readings/insights.calls.jsonl"])
def test_insights_refuse_an_output_link_before_starting_the_reader(tmp_path: Path, target: str) -> None:
    bundle = unpack_stored_run(tmp_path / "bundle")
    host_file = tmp_path / "host-file"
    host_file.write_text("keep the host's file")
    output = bundle / target
    output.parent.mkdir(exist_ok=True)
    output.symlink_to(host_file)
    config = load_run_config("configs/aurora-efficiency.fast.yaml")

    with pytest.raises(RuntimeError, match="holds a link"):
        analyze_run(bundle, config)

    assert host_file.read_text() == "keep the host's file"
