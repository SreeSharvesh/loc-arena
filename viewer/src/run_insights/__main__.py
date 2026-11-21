"""Open local reports without installing the evaluation engine or a coding-agent CLI."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from run_insights.bundle import load_analysis, load_run
from run_insights.render import render_report
from run_insights.server import ServerSettings, create_app


def main() -> None:
    """Serve selected run bundles, or export one complete offline report."""
    parser = argparse.ArgumentParser(description="Read agent runs and inspect saved analysis locally.")
    parser.add_argument("path", type=Path, help="A graded run bundle, or directory containing them")
    parser.add_argument("--out", type=Path, help="Write one offline HTML report instead of serving")
    parser.add_argument("--port", type=int, default=ServerSettings().port)
    args = parser.parse_args()
    if args.out:
        run = load_run(args.path)
        print(render_report(run, load_analysis(args.path, run), args.out))
    else:
        settings = ServerSettings(port=args.port)
        print(f"Run insights: http://127.0.0.1:{settings.port}")
        uvicorn.run(create_app(args.path, settings=settings), host="127.0.0.1", port=settings.port)


if __name__ == "__main__":
    main()
