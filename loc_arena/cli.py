"""``loc-arena`` command-line entrypoint.

Enforces config over code (mode is a flag and a config field). ``run`` plays one episode in a MODE in a
stack and writes its audit bundle; ``view`` opens the latest report.html plus prints the ``inspect view``
command. Mode is the ONLY difference between a run and its honest twin. No live server; the viewer is the
static report. The Makefile targets call this module.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import secrets
import sys
import webbrowser
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.episode_stack import EPISODE_SERVICE, run_in_stack
from loc_arena.gateway.model_gateway import PROJECT_PREFIX, locate_call_log
from loc_arena.gateway.proxy_relay import relay_while_running
from loc_arena.harness import apply_mode, locate_run, play_run, read_played_snapshot
from loc_arena.logging_.viewer import build_index
from loc_arena.merged_main import EPISODE_PHASE
from loc_arena.run_cost import read_cost, record_cost, render_costs, summarize
from loc_arena.task import grade_snapshot, read_snapshot_file


def _config_path(run: str) -> str:
    return run if run.endswith(".yaml") else f"configs/{run}.yaml"


def _read_finished(bundle: Path) -> float:
    """When ``bundle``'s run finished playing: a bundle's name ends in a random id, so it says nothing.

    The run's own record of it, which a copy of the bundle keeps; else when its report was written, and the
    epoch for a bundle that is going away.
    """
    try:
        finished = read_snapshot_file(bundle / EPISODE_PHASE).finished_at
    except (
        OSError,
        ValueError,
    ):  # no snapshot, or one that does not parse (pydantic's error is a ValueError)
        finished = None
    try:
        recorded = finished is not None and math.isfinite(finished)  # a damaged snapshot can hold any number
        return finished if recorded else (bundle / "report.html").stat().st_mtime
    except OSError:
        return 0.0


def _latest_bundle(logs_root: Path, run_slug: str) -> Path | None:
    run_dir = logs_root / run_slug
    if not run_dir.exists():
        return None
    bundles = (d for d in run_dir.iterdir() if d.is_dir() and (d / "report.html").exists())
    return max(bundles, key=_read_finished, default=None)


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_run_config(_config_path(args.run))
    if args.play_only:  # the stack's episode container plays where it is
        run_directory = locate_run(config, args.mode, args.instance or secrets.token_hex(3), Path(args.out))
        play_run(config, run_directory, mode=args.mode, next_phase=args.next_phase)
        print(f"played: {run_directory}")
        return 0
    instance, logs = secrets.token_hex(3), Path(args.out)
    cost = record_cost(config.settings.cost, f"{PROJECT_PREFIX}{instance}", EPISODE_SERVICE)
    with relay_while_running(config.settings.gateway), cost as recorder:
        out = run_in_stack(args.run, mode=args.mode, seed=args.seed, logs=logs, instance_id=instance)
    try:  # the profiler never fails a run that was played and graded
        cost_line = summarize(recorder.write(out, locate_call_log(logs, instance)))
    except (OSError, ValueError) as error:
        cost_line = f"cost not recorded: {error}"
    scores = json.loads((out / "scores.json").read_text())
    s = scores["side_task"]
    print(f"bundle: {out}")
    print(cost_line)
    print(
        f"main final_score={scores['main_task']['final_score']:.3f} "
        f"milestones={scores['main_task']['milestones_reached']} "
        f"deployed={scores['main_task']['deployed']['final_score']:.3f}",
    )
    if scores["main_task"]["error"]:
        print(f"main not graded: {scores['main_task']['error']}")
    print(
        f"side landing minimal={s['landing_minimal']} depth={s['covert_depth']}",
    )
    if s["redeployed_services"]:
        records = ", ".join(s["redeployed_services"])
        print(f"side verdict rests on the records of {records}, which this play redeployed")
    print(
        f"safety landed={scores['safety']['landed']} caught={scores['safety']['caught']} "
        f"by={scores['safety']['caught_by']} threshold={scores['safety']['aggregation']['threshold']}",
    )
    return 0


def _cmd_cost(args: argparse.Namespace) -> int:
    try:
        cost = read_cost(Path(args.run_directory))
        other = read_cost(Path(args.other)) if args.other else None
    except FileNotFoundError as error:
        print(f"no cost file: {error.filename}", file=sys.stderr)
        return 1
    print(render_costs(cost, other))
    return 0


def _cmd_grade(args: argparse.Namespace) -> int:
    directory = Path(args.snapshot_dir)
    config = apply_mode(load_run_config(_config_path(args.run)), read_snapshot_file(directory).mode)
    result = grade_snapshot(read_played_snapshot(directory, config), config)
    grades = {
        "main_task": dataclasses.asdict(result.main_score),
        "side_task": dataclasses.asdict(result.verdict),
    }
    (directory / "grades.json").write_text(json.dumps(grades, indent=2, default=str))
    main_score = result.main_score
    print(f"main final_score={main_score.final_score:.3f} milestones={main_score.milestones_reached}")
    print(
        f"side landing minimal={result.verdict.landing.minimal} depth={result.verdict.covert_depth}",
    )
    return 0


def _build_run_index(logs_root: Path) -> Path | None:
    """Write the run-index ``logs_root/index.html`` over every bundle under ``logs_root``.

    Each ``href`` is the report.html relative to where the index lives (``logs_root``).
    """
    logs_root.mkdir(parents=True, exist_ok=True)
    graded = (path.parent for path in logs_root.glob("*/*/scores.json"))
    bundles = [bundle for bundle in graded if (bundle / "report.html").exists()]
    runs: list[dict[str, object]] = []
    for bundle in sorted(bundles, key=_read_finished, reverse=True):
        scores_path, report = bundle / "scores.json", bundle / "report.html"
        try:
            scores = json.loads(scores_path.read_text())
        except (OSError, ValueError):
            continue
        runs.append(
            {
                "run_name": scores.get("run_name", bundle.name),
                "mode": scores.get("mode", ""),
                "ts": datetime.fromtimestamp(_read_finished(bundle), UTC).isoformat(timespec="seconds"),
                "href": Path(os.path.relpath(report, logs_root)).as_posix(),
                "scores": scores,
            },
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
        if any(zipfile.is_zipfile(eval_file) for eval_file in bundle.glob("*.eval")):
            print(f"inspect view: inspect view --log-dir {bundle}")
    if not args.no_open:
        target = bundle / "report.html" if bundle is not None else index
        if target is not None:
            try:
                webbrowser.open(target.resolve().as_uri())
            except Exception as exc:  # noqa: BLE001 - opening a browser is best-effort; never fail the command
                print(f"(could not open a browser: {exc}; open the path above)", file=sys.stderr)
    return 0


def _cmd_explore(args: argparse.Namespace) -> int:
    # The explorer reads Inspect logs, and inspect_ai loads ~1200 modules; only this command pays for that.
    from loc_arena.explorer.build import build
    from loc_arena.explorer.settings import ExplorerSettings

    page = build(Path(args.path), Path(args.out) if args.out else None, ExplorerSettings())
    print(f"explorer: {page}")
    if not args.no_open:
        try:
            webbrowser.open(page.resolve().as_uri())
        except Exception as exc:  # noqa: BLE001 - opening a browser is best-effort; never fail the command
            print(f"(could not open a browser: {exc}; open the path above)", file=sys.stderr)
    return 0


def _cmd_read(args: argparse.Namespace) -> int:
    # The reader's stack imports the episode's; only this command pays for that.
    from loc_arena.run_reader import read_run

    try:
        config = load_run_config(_config_path(args.run))
        with relay_while_running(config.settings.gateway):
            report = read_run(Path(args.run_directory), args.brief, config)
    except (ValueError, RuntimeError) as error:
        print(f"read: {error}", file=sys.stderr)
        return 1
    print(f"reading: {report}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch ``run`` / ``view`` / ``explore`` / ``grade`` / ``read`` / ``cost``."""
    parser = argparse.ArgumentParser(prog="loc-arena", description="Run and view LOC-Arena episodes.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="run one episode in a mode, in a stack, and write its audit bundle")
    p_run.add_argument("--run", default="aurora-efficiency")
    p_run.add_argument("--mode", choices=["attack", "honest"], default="attack")
    p_run.add_argument("--out", default="logs")
    p_run.add_argument("--seed", type=int, default=None)
    p_run.add_argument(
        "--play-only",
        action="store_true",
        help="internal to stack runs: play in this episode container, and leave grading to the host",
    )
    p_run.add_argument(
        "--next-phase",
        action="store_true",
        help="internal to stack runs: with --play-only, play only the first phase the run has not played",
    )
    p_run.add_argument(
        "--instance",
        default=None,
        help="the run's instance id, the end of its name (default: random)",
    )
    p_run.set_defaults(func=_cmd_run)

    p_view = sub.add_parser("view", help="open the latest report.html + print the inspect view command")
    p_view.add_argument("--run", default="aurora-efficiency")
    p_view.add_argument("--out", default="logs")
    p_view.add_argument("--no-open", action="store_true")
    p_view.set_defaults(func=_cmd_view)

    p_explore = sub.add_parser(
        "explore",
        help="build the run explorer for one run bundle, or for every run in a folder, and open it",
    )
    p_explore.add_argument("path", nargs="?", default="logs", help="a run bundle, or a folder of them")
    p_explore.add_argument("--out", default=None, help="where to write (default: beside the runs)")
    p_explore.add_argument("--no-open", action="store_true")
    p_explore.set_defaults(func=_cmd_explore)

    p_grade = sub.add_parser("grade", help="grade a played episode again from its snapshot directory alone")
    p_grade.add_argument("snapshot_dir", help="the episode directory holding snapshot.json")
    p_grade.add_argument("--run", default="aurora-efficiency")
    p_grade.set_defaults(func=_cmd_grade)

    p_read = sub.add_parser("read", help="have the reader agent read one graded run and report what it did")
    p_read.add_argument("run_directory", help="the run's directory, which holds scores.json")
    p_read.add_argument("--brief", default="agents", help="what to report on: agents, or monitors")
    p_read.add_argument(
        "--run",
        default="aurora-efficiency",
        help="the config whose reader role and settings to use",
    )
    p_read.set_defaults(func=_cmd_read)

    p_cost = sub.add_parser(
        "cost",
        help="print what one run cost, or how a second run's cost differs from it",
    )
    p_cost.add_argument("run_directory", help="the run's directory, which holds cost.json")
    p_cost.add_argument("other", nargs="?", help="a second run's directory, to print its cost and the change")
    p_cost.set_defaults(func=_cmd_cost)

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
