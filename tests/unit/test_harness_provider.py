from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from loc_arena import harness, live
from loc_arena.config import load_run_config
from loc_arena.logging_ import inspect_export, transcript_render
from loc_arena.logging_.agent_trace import AgentTrace

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

    def fake_transcripts(eval_path: Path, out_dir: Path) -> tuple[Path, Path]:
        seen["transcripts"] = (eval_path, out_dir)
        return out_dir / "transcript.html", out_dir / "transcript.txt"

    monkeypatch.setattr(inspect_export, "write_run_eval", fake_write)
    monkeypatch.setattr(transcript_render, "write_transcripts", fake_transcripts)
    monkeypatch.setattr(
        harness, "_eval_episodes", lambda cfg, episode, calibration, graded_sealed: ["from-trace"]
    )
    config = dataclasses.replace(LIVE, agent_transcript=True)
    out = tmp_path / "bundle"
    out.mkdir()
    harness._write_bundle(
        config, out, "run-x", {}, _bundle_inputs(tmp_path), 1, "attack", 0.5, write_report=False
    )
    assert seen == {
        "path": out / "inspect" / "run-x.eval",
        "episodes": ["from-trace"],
        "transcripts": (out / "inspect" / "run-x.eval", out),
    }
    assert not (out / "run-x.eval").exists()


def test_write_bundle_keeps_the_placeholder_when_the_flag_is_off(tmp_path: Path) -> None:
    out = tmp_path / "bundle"
    out.mkdir()
    untraced = dataclasses.replace(LIVE, agent_transcript=False)
    harness._write_bundle(
        untraced, out, "run-x", {}, _bundle_inputs(tmp_path), 1, "attack", 0.5, write_report=False
    )
    assert json.loads((out / "run-x.eval").read_text())["run_name"] == "run-x"


def _assembled(tmp_path: Path, name: str) -> SimpleNamespace:
    return SimpleNamespace(trace=AgentTrace().finish(last_sealed_seq=-1), sealed_path=tmp_path / name)


def test_eval_episodes_puts_the_root_first_and_labels_the_calibration_twin(tmp_path: Path) -> None:
    episode, twin = _assembled(tmp_path, "a"), _assembled(tmp_path, "h")
    exports = harness._eval_episodes(LIVE, episode, twin, graded_sealed=tmp_path / "bundle-copy")
    assert [(e.sample_id, e.sealed_path) for e in exports] == [
        ("episode", tmp_path / "bundle-copy"),
        ("honest_cal", tmp_path / "h"),
    ]
    assert exports[0].agent_order[0] == "agent-main"
    assert set(exports[0].agent_order) == {a.id for a in LIVE.agents}


def test_eval_episodes_without_a_twin_has_one_sample(tmp_path: Path) -> None:
    assert [
        e.sample_id
        for e in harness._eval_episodes(LIVE, _assembled(tmp_path, "a"), None, graded_sealed=tmp_path / "a")
    ] == ["episode"]


def test_eval_episodes_refuses_an_untraced_episode(tmp_path: Path) -> None:
    untraced = SimpleNamespace(trace=None, sealed_path=tmp_path / "a")
    with pytest.raises(ValueError, match="episode was assembled without an agent trace"):
        harness._eval_episodes(LIVE, untraced, None, graded_sealed=tmp_path / "a")


def test_run_episode_gives_the_honest_twin_its_own_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def capture(cfg: object, workdir: Path, *, robust: bool, provider: Any = None) -> SimpleNamespace:
        seen[workdir.name] = provider
        if workdir.name == "honest_cal":
            raise _StopAfterAssemblyError
        return SimpleNamespace()

    monkeypatch.setattr(harness, "_assemble_by_policy", capture)
    graded, twin = object(), object()
    with pytest.raises(_StopAfterAssemblyError):
        harness.run_episode(
            LIVE, mode="attack", out_root=tmp_path, provider=graded, calibration_provider=twin
        )
    assert seen == {"episode": graded, "honest_cal": twin}


class _StopAfterAssemblyError(Exception):
    pass


def test_an_export_failure_still_leaves_the_rest_of_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_write(path: Path, **kwargs: Any) -> Path:
        raise inspect_export.UnassignedEventError("seq 3 has no lane")

    monkeypatch.setattr(inspect_export, "write_run_eval", failing_write)
    monkeypatch.setattr(harness, "_eval_episodes", lambda cfg, episode, calibration, graded_sealed: [])
    out = tmp_path / "bundle"
    out.mkdir()
    config = dataclasses.replace(LIVE, agent_transcript=True)
    with pytest.raises(inspect_export.UnassignedEventError):
        harness._write_bundle(
            config, out, "run-x", {}, _bundle_inputs(tmp_path), 1, "attack", 0.5, write_report=False
        )
    assert {p.name for p in out.iterdir()} >= {"scores.json", "decisions.md", "events.sealed.jsonl"}
