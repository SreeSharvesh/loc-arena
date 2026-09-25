from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loc_arena import harness, live
from loc_arena.config import load_run_config
from loc_arena.logging_ import inspect_export

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


def _bundle_inputs(tmp_path: Path) -> SimpleNamespace:
    sealed = tmp_path / "src.sealed.jsonl"
    mirror = tmp_path / "src.mirror.jsonl"
    sealed.write_text("")
    mirror.write_text("")
    return SimpleNamespace(sealed_path=sealed, mirror_path=mirror)


def test_write_bundle_writes_the_real_eval_when_the_flag_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_write(path: Path, **kwargs: Any) -> Path:
        seen["path"] = path
        seen["episodes"] = kwargs["episodes"]
        return path

    monkeypatch.setattr(inspect_export, "write_run_eval", fake_write)
    monkeypatch.setattr(harness, "_eval_episodes", lambda cfg, episode, calibration: ["from-trace"])
    config = dataclasses.replace(LIVE, agent_transcript=True)
    out = tmp_path / "bundle"
    out.mkdir()
    harness._write_bundle(
        config, out, "run-x", {}, _bundle_inputs(tmp_path), 1, "attack", 0.5, write_report=False
    )
    assert seen == {"path": out / "run-x.eval", "episodes": ["from-trace"]}
    assert not (out / "run-x.eval").exists()


def test_write_bundle_keeps_the_placeholder_when_the_flag_is_off(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    harness._write_bundle(
        LIVE, out, "run-x", {}, _bundle_inputs(tmp_path), 1, "attack", 0.5, write_report=False
    )
    assert json.loads((out / "run-x.eval").read_text())["run_name"] == "run-x"
