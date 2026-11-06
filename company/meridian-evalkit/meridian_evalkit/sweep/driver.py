"""Submit a sweep's trials to the cluster as jobs, each sized to the steps the pruner keeps it running.

In a live run the sweep runs on the cluster: each trial is a job that advances its learning curve for a number
of steps. The early-stopping rule decides how many steps each trial gets -- the eventual winner runs the full
budget, the losers stop early and their jobs are correspondingly short -- so a better rule submits less total
compute while still running the winner to completion. :func:`submit_sweep` plans the sweep with the pruner,
then submits one right-sized job per trial through the shared :class:`~meridian_common.jobclient.JobClient`.
"""

from __future__ import annotations

from dataclasses import dataclass

from meridian_common.jobclient import JobClient, JobSpec
from meridian_evalkit.sweep.pruning import Pruner
from meridian_evalkit.sweep.sweep import SweepResult, SweepSpec, run_sweep
from meridian_evalkit.sweep.trial import TrialConfig

TRIAL_MODULE = "meridian_evalkit.sweep"  # ``python -m meridian_evalkit.sweep`` runs one trial


@dataclass(frozen=True)
class SweepSubmission:
    """What a submitted sweep left on the cluster: its planned winner and cost, and each trial's job id."""

    result: SweepResult
    jobs: dict[str, str]


def trial_command(config: TrialConfig, steps: int) -> list[str]:
    """The argv that runs ``config`` for ``steps`` steps as a cluster job."""
    return [
        "python",
        "-m",
        TRIAL_MODULE,
        "--id",
        config.trial_id,
        "--target",
        str(config.target),
        "--rate",
        str(config.rate),
        "--steps",
        str(steps),
    ]


def submit_sweep(
    spec: SweepSpec,
    client: JobClient,
    pruner: Pruner | None = None,
    *,
    cpu: float = 1.0,
    memory_mb: int = 512,
    priority: int = 0,
    labels: dict[str, str] | None = None,
) -> SweepSubmission:
    """Plan ``spec`` under ``pruner``, then submit one job per trial sized to the steps it survived.

    Returns the planned :class:`SweepResult` and the ``{trial_id: job_id}`` the cluster assigned. The winner's
    job runs the full budget; a stopped trial's job runs only the steps it survived.
    """
    result = run_sweep(spec, pruner)
    base_labels = {"kind": "sweep-trial", **(labels or {})}
    jobs: dict[str, str] = {}
    for config in spec.trials:
        steps = result.final[config.trial_id].step
        job = JobSpec(
            name=f"sweep-{config.trial_id}",
            command=trial_command(config, steps),
            cpu=cpu,
            memory_mb=memory_mb,
            priority=priority,
            labels={**base_labels, "trial": config.trial_id, "steps": str(steps)},
        )
        jobs[config.trial_id] = client.submit(job)
    return SweepSubmission(result=result, jobs=jobs)
