from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from loc_arena import harness, live
from loc_arena.config import load_run_config

LIVE = dataclasses.replace(load_run_config("configs/aurora-efficiency.deterministic.yaml"), policy="model")


def test_assemble_by_policy_hands_the_provider_to_the_live_assembler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_assemble(config: object, workdir: Path, *, provider: object = None) -> str:
        seen["provider"] = provider
        return "episode"

    monkeypatch.setattr(live, "assemble_model_episode", fake_assemble)
    marker = object()
    assert harness._assemble_by_policy(LIVE, tmp_path, robust=False, provider=marker) == "episode"
    assert seen["provider"] is marker
