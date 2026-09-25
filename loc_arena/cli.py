"""``loc-arena`` command-line entrypoint.

Enforces config over code (mode is a flag and a config field) and reproducibility from
(config, seed)). ``run`` executes one episode in a MODE and writes its audit bundle; ``view`` opens the
latest report.html plus prints the ``inspect view`` command; ``sweep`` runs N honest and M attack episodes
and aggregates a safety number. Mode is the ONLY difference between a run and its honest twin. No live
server; the viewer is the static report. The Makefile targets call this module.
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.harness import run_episode, run_sweep


def _config_path(run: str) -> str:
    return run if run.endswith(".yaml") else f"configs/{run}.yaml"


def _latest_bundle(logs_root: Path, run_slug: str) -> Path | None:
    run_dir = logs_root / run_slug
    if not run_dir.exists():
        return None
    bundles = sorted(
        (d for d in run_dir.iterdir() if d.is_dir() and (d / "report.html").exists()), reverse=True
    )
    return bundles[0] if bundles else None


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_run_config(_config_path(args.run))
    out = run_episode(
        config, mode=args.mode, out_root=Path(args.out), seed=args.seed, robust=not args.minimal
    )
    scores = json.loads((out / "scores.json").read_text())
    s = scores["side_task"]
    print(f"bundle: {out}")
    print(
        f"main final_score={scores['main_task']['final_score']:.3f} "
        f"milestones={scores['main_task']['milestones_reached']}"
    )
    print(
        f"side landing minimal={s['landing_minimal']} robust={s['landing_robust']} depth={s['covert_depth']}"
    )
    print(
        f"safety landed={scores['safety']['landed']} caught={scores['safety']['caught']} "
        f"by={scores['safety']['caught_by']} threshold={scores['safety']['audit_threshold']}"
    )
    return 0


def _build_run_index(logs_root: Path) -> Path | None:
    """Write the run-index ``logs_root/index.html`` over every bundle under ``logs_root``.

    Each ``href`` is the report.html relative to where the index lives (``logs_root``).
    """
    import os

    from loc_arena.logging_.viewer import build_index

    logs_root.mkdir(parents=True, exist_ok=True)
    scores_paths = list(logs_root.glob("*/*/scores.json"))
    runs: list[dict[str, object]] = []
    for scores_path in sorted(scores_paths, reverse=True):
        bundle = scores_path.parent
        report = bundle / "report.html"
        if not report.exists():
            continue
        try:
            scores = json.loads(scores_path.read_text())
        except (OSError, ValueError):
            continue
        runs.append(
            {
                "run_name": scores.get("run_name", bundle.name),
                "mode": scores.get("mode", ""),
                "ts": bundle.name,
                "href": Path(os.path.relpath(report, logs_root)).as_posix(),
                "scores": scores,
            }
        )
    if not runs:
        return None
    index = logs_root / "index.html"
    index.write_text(build_index(runs))
    return index


def _cmd_view(args: argparse.Namespace) -> int:
    config = load_run_config(_config_path(args.run))
    logs_root = Path(args.out)
    bundle = _latest_bundle(logs_root, config.run_slug)
    index = _build_run_index(logs_root)
    if bundle is None and index is None:
        print(
            f"no bundle under {logs_root / config.run_slug}; run `make run` first",
            file=sys.stderr,
        )
        return 1
    if index is not None:
        print(f"index: {index}")
    if bundle is not None:
        print(f"report: {bundle / 'report.html'}")
        if config.agent_transcript and any(bundle.glob("*.eval")):
            print(f"inspect view: inspect view --log-dir {bundle}")
    if not args.no_open:
        target = index if index is not None else (bundle / "report.html" if bundle else None)
        if target is not None:
            try:
                webbrowser.open(target.resolve().as_uri())
            except Exception as exc:  # noqa: BLE001 - opening a browser is best-effort; never fail the command
                print(f"(could not open a browser: {exc}; open the path above)", file=sys.stderr)
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    config = load_run_config(_config_path(args.run))
    agg = run_sweep(
        config, honest=args.honest, attack=args.attack, out_root=Path(args.out), robust=not args.minimal
    )
    print(f"sweep bundle: {agg['_dir']}")
    print(json.dumps({k: v for k, v in agg.items() if k != "_dir"}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch ``run`` / ``view`` / ``sweep``."""
    parser = argparse.ArgumentParser(prog="loc-arena", description="Run and view LOC-Arena episodes.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run one episode in a mode and write its audit bundle")
    p_run.add_argument("--run", default="aurora-efficiency")
    p_run.add_argument("--mode", choices=["attack", "honest"], default="attack")
    p_run.add_argument("--out", default="logs")
    p_run.add_argument("--seed", type=int, default=None)
    p_run.add_argument("--minimal", action="store_true", help="attack reaches only the minimal landing")
    p_run.set_defaults(func=_cmd_run)

    p_view = sub.add_parser("view", help="open the latest report.html + print the inspect view command")
    p_view.add_argument("--run", default="aurora-efficiency")
    p_view.add_argument("--out", default="logs")
    p_view.add_argument("--no-open", action="store_true")
    p_view.set_defaults(func=_cmd_view)

    p_sweep = sub.add_parser("sweep", help="run N honest + M attack episodes and aggregate a safety number")
    p_sweep.add_argument("--run", default="aurora-efficiency")
    p_sweep.add_argument("--honest", type=int, default=2)
    p_sweep.add_argument("--attack", type=int, default=2)
    p_sweep.add_argument("--out", default="logs")
    p_sweep.add_argument("--minimal", action="store_true")
    p_sweep.set_defaults(func=_cmd_sweep)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
