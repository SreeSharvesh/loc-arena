"""Per-agent transcript as self-contained HTML and plain text; contract in docs/agent-log/spec.md."""

from __future__ import annotations

import html
from collections.abc import Sequence
from pathlib import Path

from inspect_ai.log import read_eval_log
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import JsonLexer

from loc_arena.logging_.transcript_lanes import BEFORE_FIRST_ROUND, Block, SampleTranscript, build_transcript

TRANSCRIPT_HTML = "transcript.html"
TRANSCRIPT_TEXT = "transcript.txt"

_PAGE_CSS = """
body { font-family: system-ui, sans-serif; margin: 1.5rem; background: #f7f7f8; color: #1d1d1f; }
h1 { font-size: 1.3rem; margin: 0 0 0.3rem; }
header p { color: #555; margin: 0 0 1rem; }
.sample { margin-bottom: 2rem; }
.scroll { overflow: auto; max-height: 85vh; border: 1px solid #d9d9e0; border-radius: 6px; background: #f7f7f8; }
.grid { display: grid; gap: 0.4rem; align-items: start; padding: 0 0.4rem 0.4rem; }
.head-row { position: sticky; top: 0; z-index: 1; padding-top: 0.4rem; background: #f7f7f8; }
.head { font-weight: 600; background: #e9e9ee; padding: 0.4rem 0.5rem; border-radius: 4px; }
.row-label { font-size: 0.8rem; color: #555; padding: 0.4rem 0.2rem; }
.cell { display: flex; flex-direction: column; gap: 0.3rem; min-width: 0; }
.block { background: #fff; border: 1px solid #d9d9e0; border-left: 4px solid #999; border-radius: 4px; padding: 0.35rem 0.5rem; font-size: 0.8rem; }
.block.prompt { border-left-color: #4c7bd9; }
.block.reply { border-left-color: #2f9e44; }
.block.tool { border-left-color: #e8590c; }
.block.info { border-left-color: #868e96; }
.block.blocked { background: #fff5f5; border-left-color: #e03131; }
.title { font-weight: 600; margin-bottom: 0.2rem; }
pre { white-space: pre-wrap; word-break: break-word; margin: 0; font-size: 0.75rem; }
details summary { cursor: pointer; color: #4c7bd9; }
""".strip()

_CODE_CSS = HtmlFormatter(style="friendly").get_style_defs(".code")


def write_transcripts(eval_path: Path, out_dir: Path) -> tuple[Path, Path]:
    log = read_eval_log(str(eval_path))
    transcripts = [build_transcript(sample) for sample in log.samples or []]
    title = log.eval.run_id or eval_path.stem
    html_path, text_path = out_dir / TRANSCRIPT_HTML, out_dir / TRANSCRIPT_TEXT
    html_path.write_text(render_html(title, transcripts), encoding="ascii")
    text_path.write_text(render_text(title, transcripts), encoding="utf-8")
    return html_path, text_path


def render_html(title: str, transcripts: Sequence[SampleTranscript]) -> str:
    sections = "\n".join(
        f'<section class="sample"><h2>sample {html.escape(t.sample_id)}</h2>\n{_grid_html(t)}\n</section>'
        for t in transcripts
    )
    page = (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{html.escape(title)} transcript</title>\n<style>\n{_PAGE_CSS}\n{_CODE_CSS}\n</style>\n"
        "</head>\n<body>\n"
        f"<header><h1>{html.escape(title)}</h1>\n"
        "<p>One column per agent, one row per round. World holds events outside any agent turn.</p></header>\n"
        f"{sections}\n</body>\n</html>\n"
    )
    return page.encode("ascii", "xmlcharrefreplace").decode("ascii")


def render_text(title: str, transcripts: Sequence[SampleTranscript]) -> str:
    lines = [title]
    for transcript in transcripts:
        lines += ["", f"== sample {transcript.sample_id} =="]
        for lane in transcript.lanes:
            lane_blocks = [
                (row, block) for row in transcript.rows for block in transcript.cells.get((lane, row), ())
            ]
            if not lane_blocks:
                continue
            lines += ["", f"-- {lane} --"]
            for row, block in lane_blocks:
                lines.append(f"[{_row_label(row)}] {block.title}")
                lines += [f"    {line}" for line in (block.code or "").splitlines()]
                lines += [f"    {line}" for line in block.body.splitlines()]
    return "\n".join(lines) + "\n"


def _grid_html(transcript: SampleTranscript) -> str:
    columns = f"grid-template-columns: 8rem repeat({len(transcript.lanes)}, minmax(18rem, 1fr));"
    heads = "".join(f'<div class="head">{html.escape(lane)}</div>' for lane in transcript.lanes)
    parts = [
        '<div class="scroll">',
        f'<div class="grid head-row" style="{columns}"><div class="head"></div>{heads}</div>',
        f'<div class="grid" style="{columns}">',
    ]
    for row in transcript.rows:
        parts.append(f'<div class="row-label">{html.escape(_row_label(row))}</div>')
        for lane in transcript.lanes:
            blocks = "".join(_block_html(block) for block in transcript.cells.get((lane, row), ()))
            parts.append(f'<div class="cell">{blocks}</div>')
    parts += ["</div>", "</div>"]
    return "\n".join(parts)


def _block_html(block: Block) -> str:
    classes = " ".join(["block", block.kind, *(["blocked"] if block.blocked else [])])
    code = _code_html(block.code) if block.code else ""
    body = html.escape(block.body)
    if block.kind == "prompt":
        content = f"<details><summary>prompt, {len(block.body)} chars</summary><pre>{body}</pre></details>"
    else:
        content = f"<pre>{body}</pre>" if block.body else ""
    return f'<article class="{classes}"><div class="title">{html.escape(block.title)}</div>{code}{content}</article>'


def _code_html(code: str) -> str:
    return highlight(code, JsonLexer(), HtmlFormatter(cssclass="code"))


def _row_label(row: int) -> str:
    return "before round 0" if row == BEFORE_FIRST_ROUND else f"round {row}"
