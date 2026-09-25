"""Per-agent transcript as self-contained HTML and plain text; contract in docs/agent-log/spec.md."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from loc_arena.logging_.transcript_lanes import Block, SampleTranscript

TRANSCRIPT_HTML = "transcript.html"
TRANSCRIPT_TEXT = "transcript.txt"


def write_transcripts(eval_path: Path, out_dir: Path) -> tuple[Path, Path]:
    raise NotImplementedError


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
