"""The stored run: a graded attack run of the smoke config with its honest twin, played once and committed.

``make fixture-run`` records it again. Tests that read a run's records, or grade one again, work on a copy.
"""

from __future__ import annotations

import tarfile
from pathlib import Path

from loc_arena.config import RunConfig, load_run_config
from loc_arena.merged_main import CHECKOUT, EPISODE_PHASE, TWIN_PHASE
from loc_arena.tasks.main_task_grader import seed_checkout

ARCHIVE = Path(__file__).parent / "fixtures" / "run.tar.gz"
CONFIG: RunConfig = load_run_config("configs/aurora-efficiency.fast.yaml")  # the config the run played


def unpack_stored_run(into: Path) -> Path:
    """A copy of the stored run's directory under ``into``, each play's checkout the company's code.

    The run's own checkouts are not stored, so its main task grades as work that changed nothing.
    """
    with tarfile.open(ARCHIVE) as archive:
        archive.extractall(into, filter="data")
    (run_directory,) = (path for path in into.iterdir() if path.is_dir())
    for phase in (EPISODE_PHASE, TWIN_PHASE):
        seed_checkout(run_directory / phase / CHECKOUT)
    return run_directory
