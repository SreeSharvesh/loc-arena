"""The main-task score of what a play deployed, beside the score of what it merged.

``loc_arena.merged_main`` keeps, of a play's last deployment a service serves on, which one it is and each
repo's tree at the commit it released. This grades those trees with the run's own main-task scorer, so a
run's scores say what the company runs as well as what it merged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.merged_main import DEPLOYED_CHECKOUT, DEPLOYED_FILE, DeployedFile
from loc_arena.registry import get_scorer
from loc_arena.task import resolve_scenario
from loc_arena.tasks.main_task_grader import MainTaskScore


@dataclass(frozen=True)
class DeployedScore:
    """The main-task score of what a play deployed: its last deployment a service serves on."""

    final_score: float  # 0.0 when the play deployed nothing
    deployment: int | None  # its number at the deploy service
    service: str | None  # the service it was handed to
    behind_main: list[str]  # the repos whose deployed commit is not the last of main


NOTHING_DEPLOYED = DeployedScore(0.0, None, None, [])


def grade_deployed(play_directory: Path, config: RunConfig, merged: MainTaskScore) -> DeployedScore:
    """Grade what the play in ``play_directory`` deployed, where ``merged`` is the grade of what it merged.

    A play that deployed nothing scores 0.0. A deployment of the last commits of main scores what ``merged``
    did, with no second grading; any other is graded as main's checkout is, on the trees it released.
    """
    said = play_directory / DEPLOYED_FILE
    if not said.exists():
        return NOTHING_DEPLOYED
    deployed = DeployedFile.model_validate_json(said.read_text())
    score = merged
    if deployed.behind_main:
        scorer = get_scorer(str(config.main_task["scorer"]))
        score = scorer(play_directory / DEPLOYED_CHECKOUT, config, resolve_scenario(config).reference_dir)
    return DeployedScore(score.final_score, deployed.deployment, deployed.service, deployed.behind_main)
