from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.logging_ import transcript_render
from loc_arena.logging_.agent_trace import AgentTrace
from loc_arena.logging_.events import AppendOnlyLog, Event
from loc_arena.logging_.inspect_export import EpisodeExport, write_run_eval
from loc_arena.logging_.transcript_lanes import SampleTranscript
from loc_arena.logging_.transcript_render import write_transcripts

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")


def _exported(tmp_path: Path) -> Path:
    trace = AgentTrace(wall_clock=lambda: 1_700_000_000.0)
    sealed_path = tmp_path / "sealed.jsonl"
    sealed = AppendOnlyLog(sealed_path, "ep-render", on_append=trace.on_sealed_append)
    with trace.turn("agent-main", 0):
        sealed.append(
            Event(
                episode_id="ep-render",
                seq=0,
                ts=0.0,
                actor_uid="agent-main",
                actor_role="untrusted",
                kind="message",
            )
        )
    episode = EpisodeExport("episode", trace.finish(last_sealed_seq=0), sealed_path, ("agent-main",))
    return write_run_eval(
        tmp_path / "run.eval",
        run_name="run-x",
        config=CFG,
        mode="attack",
        seed=1,
        scores={},
        episodes=[episode],
    )


def test_write_transcripts_renders_every_sample_under_the_run_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_html(title: str, transcripts: Sequence[SampleTranscript]) -> str:
        return f"<html>{title}:{[t.sample_id for t in transcripts]}</html>"

    def fake_text(title: str, transcripts: Sequence[SampleTranscript]) -> str:
        return f"{title}:{[t.lanes for t in transcripts]}"

    monkeypatch.setattr(transcript_render, "render_html", fake_html)
    monkeypatch.setattr(transcript_render, "render_text", fake_text)
    html_path, text_path = write_transcripts(_exported(tmp_path), tmp_path)
    assert (html_path.name, text_path.name) == ("transcript.html", "transcript.txt")
    assert html_path.read_text() == "<html>run-x:['episode']</html>"
    assert text_path.read_text() == "run-x:[('World', 'agent-main')]"
