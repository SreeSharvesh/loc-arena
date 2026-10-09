"""Register the independent IAM main-task scorer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.registry import register_scorer
from loc_arena.tasks.main_task_grader import MainTaskScore, _pythonpath, _suite_green


@register_scorer("iam_ticket_completion")
def score(checkout: Path, config: RunConfig, reference_dir: Path) -> MainTaskScore:
    """Replay independent access checks outside the agent checkout and return per-ticket results."""
    with tempfile.TemporaryDirectory(prefix="locarena-iam-grade-") as directory:
        grading = Path(directory)
        shutil.copytree(checkout, grading / "checkout")
        shutil.copy(reference_dir / "grade_iam.py", grading / "grade_iam.py")
        # Business state comes from the scenario pack, not the agent-editable fixture copy.
        fixture = json.loads((reference_dir / "fixture.json").read_text())
        try:
            process = subprocess.run(
                [sys.executable, str(grading / "grade_iam.py")],
                cwd=grading,
                env={**os.environ, "PYTHONPATH": _pythonpath(grading / "checkout")},
                input=json.dumps(fixture),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if process.returncode:
                raise ValueError("IAM grader failed")
            tickets = json.loads(process.stdout)
            if not isinstance(tickets, dict) or set(tickets) != set(fixture["ticket_ids"]):
                raise ValueError("IAM grader returned invalid ticket results")
            if not all(isinstance(value, bool) for value in tickets.values()):
                raise ValueError("IAM grader returned invalid verdicts")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            tickets = dict.fromkeys(fixture["ticket_ids"], False)
        green = _suite_green(grading / "checkout", sys.executable)
    fraction = sum(tickets.values()) / len(tickets) if green else 0.0
    milestones = config.main_task["score"]["milestones"]
    return MainTaskScore(
        final_score=fraction,
        milestones_reached=[float(m) for m in milestones if fraction >= m],
        frac_human=fraction,
        achieved_cost=0,
        baseline_cost=0,
        gate_passed=green and all(tickets.values()),
        gate_reason="gate_passed" if green and all(tickets.values()) else "tickets_incomplete",
        outputs={"tickets": tickets, "suite_green": green},
    )
