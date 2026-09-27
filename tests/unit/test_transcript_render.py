from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.logging_ import transcript_render
from loc_arena.logging_.agent_trace import AgentTrace
from loc_arena.logging_.events import AppendOnlyLog, Event
from loc_arena.logging_.inspect_export import EpisodeExport, write_run_eval
from loc_arena.logging_.transcript_lanes import WORLD, Block, SampleTranscript
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
            ),
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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


def _transcript(sample_id: str = "episode") -> SampleTranscript:
    return SampleTranscript(sample_id=sample_id, lanes=(WORLD, "agent-main"), rows=(-1, 0), cells={})


def test_render_html_is_a_self_contained_ascii_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        transcript_render,
        "_grid_html",
        lambda t: f"<div>grid {t.sample_id} caf\u00e9 \u2014</div>",
    )
    page = transcript_render.render_html("run-x", [_transcript("episode"), _transcript("honest_cal")])
    assert page.isascii()
    assert "caf&#233; &#8212;" in page
    assert "<h2>sample episode</h2>" in page and "<h2>sample honest_cal</h2>" in page
    assert "<script" not in page and "src=" not in page and "href=" not in page


def test_render_text_lists_every_lane_with_its_blocks_in_round_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_render, "_row_label", lambda row: f"r{row}")
    transcript = SampleTranscript(
        sample_id="episode",
        lanes=(WORLD, "agent-main", "idle-agent"),
        rows=(-1, 0),
        cells={
            ("agent-main", 0): (Block("tool", "read_file", "ok", code='{"path": "a"}'),),
            (WORLD, -1): (Block("info", "tick", "t0"),),
        },
    )
    assert transcript_render.render_text("run-x", [transcript]) == (
        "run-x\n\n== sample episode ==\n\n-- World --\n[r-1] tick\n    t0\n\n"
        '-- agent-main --\n[r0] read_file\n    {"path": "a"}\n    ok\n\n'
        "-- idle-agent --\n    (no activity)\n"
    )


def test_grid_has_a_head_per_lane_and_a_cell_per_lane_per_round(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_render, "_row_label", lambda row: f"r{row}")
    monkeypatch.setattr(transcript_render, "_block_html", lambda block: f"[{block.title}]")
    transcript = SampleTranscript(
        sample_id="episode",
        lanes=(WORLD, "agent-main"),
        rows=(-1, 0),
        cells={("agent-main", 0): (Block("reply", "reply", "x"),)},
    )
    grid = transcript_render._grid_html(transcript)
    assert grid.startswith('<div class="scroll">')
    assert grid.count("repeat(2, minmax(18rem, 1fr))") == 2
    assert '<div class="grid head-row"' in grid
    assert grid.count('<div class="head">') == 3
    assert grid.count('<div class="cell">') == 4
    assert (
        '<div class="row-label">r0</div>\n<div class="cell"></div>\n<div class="cell">[reply]</div>' in grid
    )


def test_a_prompt_block_is_collapsed_and_escaped() -> None:
    article = transcript_render._block_html(
        Block("prompt", "prompt as agent-main (deciding)", "<b>brief</b>"),
    )
    assert article == (
        '<article class="block prompt"><div class="title">prompt as agent-main (deciding)</div>'
        "<details><summary>prompt, 12 chars</summary><pre>&lt;b&gt;brief&lt;/b&gt;</pre></details></article>"
    )


def test_a_blocked_tool_block_carries_the_blocked_class_and_its_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transcript_render, "_code_html", lambda code: f"<code>{code}</code>")
    article = transcript_render._block_html(Block("tool", "spawn (blocked)", "no", code="{}", blocked=True))
    assert article.startswith('<article class="block tool blocked">')
    assert "<code>{}</code><pre>no</pre>" in article


def test_code_is_highlighted_as_json_inside_the_code_class() -> None:
    rendered = transcript_render._code_html('{"path": "a.py"}')
    assert rendered.startswith('<div class="code">')
    assert "a.py" in rendered and '<span class="' in rendered


def test_row_labels() -> None:
    assert [transcript_render._row_label(r) for r in (-1, 0, 7)] == ["before round 0", "round 0", "round 7"]


def test_control_characters_become_visible_escapes_in_both_outputs() -> None:
    block = Block("tool", "run_tests", "\x1b[31mFAILED\x1b[0m\x00\tdone\n")
    article = transcript_render._block_html(block)
    assert "\x1b" not in article and "\x00" not in article
    assert "\\x1b[31mFAILED\\x1b[0m\\x00\tdone" in article
    transcript = SampleTranscript("episode", ("agent-main",), (0,), {("agent-main", 0): (block,)})
    text = transcript_render.render_text("run-x", [transcript])
    assert "\x1b" not in text and "\\x1b[31m" in text
