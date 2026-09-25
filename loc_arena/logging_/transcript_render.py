"""Per-agent transcript as self-contained HTML and plain text; contract in docs/agent-log/spec.md."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from inspect_ai.log import read_eval_log

from loc_arena.logging_.transcript_lanes import Block, SampleTranscript, build_transcript

TRANSCRIPT_HTML = "transcript.html"
TRANSCRIPT_TEXT = "transcript.txt"


def write_transcripts(eval_path: Path, out_dir: Path) -> tuple[Path, Path]:
    log = read_eval_log(str(eval_path))
    transcripts = [build_transcript(sample) for sample in log.samples or []]
    title = log.eval.run_id or eval_path.stem
    html_path, text_path = out_dir / TRANSCRIPT_HTML, out_dir / TRANSCRIPT_TEXT
    html_path.write_text(render_html(title, transcripts), encoding="ascii")
    text_path.write_text(render_text(title, transcripts), encoding="utf-8")
    return html_path, text_path


def render_html(title: str, transcripts: Sequence[SampleTranscript]) -> str:
    raise NotImplementedError


def render_text(title: str, transcripts: Sequence[SampleTranscript]) -> str:
    raise NotImplementedError


def _grid_html(transcript: SampleTranscript) -> str:
    raise NotImplementedError


def _block_html(block: Block) -> str:
    raise NotImplementedError


def _code_html(code: str) -> str:
    raise NotImplementedError


def _row_label(row: int) -> str:
    raise NotImplementedError
