"""Render one connected run and its cited insights as a self-contained report."""

from __future__ import annotations

import json
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path

from run_insights.models import Analysis, RunData
from run_insights.session import link_session_calls


def render_html(
    run: RunData,
    analysis: Analysis | None,
    *,
    live: bool = False,
    run_key: str = "",
) -> str:
    """Build a report with complete source records and no external assets or credentials."""
    templates = files("run_insights").joinpath("templates")
    page = templates.joinpath("report.html").read_text(encoding="utf-8")
    data = json.dumps(
        {
            "run": run.model_dump(mode="json"),
            "analysis": analysis.model_dump(mode="json") if analysis else None,
            "live": live,
            "run_key": run_key if live else "",
            "session_links": asdict(link_session_calls(run.records)),
        },
        ensure_ascii=False,
        allow_nan=False,
    )
    # JSON remains data even when an untrusted record contains an HTML closing tag.
    data = data.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    for name in ("report.css", "report.js"):
        page = page.replace(f"__{name}__", templates.joinpath(name).read_text(encoding="utf-8"))
    return page.replace("__DATA__", data)


def render_report(
    run: RunData,
    analysis: Analysis | None,
    out: Path,
    live: bool = False,
    run_key: str = "",
) -> Path:
    """Write the portable report to ``out``; opening it makes no model calls."""
    out.write_text(render_html(run, analysis, live=live, run_key=run_key), encoding="utf-8")
    return out
