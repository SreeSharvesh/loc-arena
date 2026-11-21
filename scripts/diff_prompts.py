"""Compare what every run config sends its models on this checkout with what it sends on a base commit.

For each run config git tracks in ``configs/`` and each play, the attack and its honest twin, it builds every
agent's system prompt as pi receives it, then the covert objective, each monitor's prompt template and the
task description the monitors read, and every session's opening prompt. It builds them on a worktree of
the base and on this checkout, prints each text that differs and exits non-zero if any does. It plays nothing
and calls no model.

    uv run python scripts/diff_prompts.py --base origin/main
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

# Imported from the tree being compared: its process runs this file with that tree first on its path.
from loc_arena.config import load_run_config
from loc_arena.harness import apply_mode
from loc_arena.live import build_briefs
from loc_arena.monitors.registry import load_monitor_specs
from loc_arena.own_sessions import TASK, brief_loop
from loc_arena.protocol import monitor_context

CONFIGS = Path("configs")
NOT_RUN_CONFIGS = ("env.", "models.", "monitors.", "taxonomy.")
PLAYS = ("honest", "attack")


def build_texts() -> dict[str, str]:
    """Every text the run configs send, by ``<config> <what>``, built by the code on ``sys.path``."""
    texts: dict[str, str] = {}
    tracked = subprocess.run(
        ["git", "ls-files", f"{CONFIGS}/*.yaml"],
        capture_output=True,
        text=True,
        check=True,
    )
    for path in map(Path, tracked.stdout.split()):
        if path.name.startswith(NOT_RUN_CONFIGS):
            continue
        config = load_run_config(path)
        for play in PLAYS:
            played = apply_mode(config, play)
            for agent, brief in build_briefs(played).items():
                texts[f"{path.stem} {play} {agent}"] = brief_loop(played, agent, brief)
        texts[f"{path.stem} opening prompt"] = TASK
        texts[f"{path.stem} covert objective"] = config.covert.objective_prompt
        texts[f"{path.stem} task description"] = monitor_context(config).task_description
        for spec in load_monitor_specs(CONFIGS / str(config.protocol["monitors_config"])):
            texts[f"{path.stem} monitor {spec.name}"] = spec.prompt_template
    return texts


def build_texts_in(tree: Path) -> dict[str, str]:
    """``build_texts`` run on the code and configs of ``tree``, in a process of its own."""
    environment = {**os.environ, "PYTHONPATH": str(tree)}
    command = [sys.executable, str(Path(__file__).resolve()), "--build"]
    built = subprocess.run(command, cwd=tree, env=environment, capture_output=True, text=True, check=False)
    if built.returncode:
        raise SystemExit(f"building the texts in {tree} failed:\n{built.stderr}")
    return json.loads(built.stdout)


def compare(base: dict[str, str], head: dict[str, str]) -> list[str]:
    """A unified diff of each text that differs between ``base`` and ``head``, or is in one alone."""
    return [
        "".join(
            difflib.unified_diff(
                base.get(name, "").splitlines(keepends=True),
                head.get(name, "").splitlines(keepends=True),
                f"base: {name}" if name in base else "base: (absent)",
                f"head: {name}" if name in head else "head: (absent)",
            ),
        )
        or f"{name} differs\n"
        for name in sorted(base.keys() | head.keys())
        if base.get(name) != head.get(name)
    ]


def summarise(texts: dict[str, str], differences: int) -> str:
    """One line per run config: how many texts of each kind were compared, and the verdict."""
    counts: dict[str, Counter[str]] = {}
    for name in texts:
        config, kind, _ = name.split(" ", 2)
        counts.setdefault(config, Counter())[kind] += 1
    lines = [
        f"{config}: " + ", ".join(f"{n} {kind}" for kind, n in kinds.items())
        for config, kinds in counts.items()
    ]
    verdict = f"{differences} texts differ" if differences else "every text is byte for byte the base's"
    return "\n".join([*lines, f"{len(texts)} texts over {len(counts)} run configs: {verdict}"])


def main() -> int:
    """Build the texts on the base and on this checkout, print what differs, and say whether anything did."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="the commit to compare with")
    parser.add_argument("--build", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.build:
        print(json.dumps(build_texts()))
        return 0
    with tempfile.TemporaryDirectory() as scratch:
        tree = Path(scratch) / "base"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(tree), arguments.base],
            check=True,
            capture_output=True,
        )
        try:
            base = build_texts_in(tree)
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(tree)], check=True)
    head = build_texts_in(Path.cwd())
    differences = compare(base, head)
    print("".join(differences), end="")
    print(summarise(head, len(differences)))
    return 1 if differences else 0


if __name__ == "__main__":
    raise SystemExit(main())
