"""Play the smoke run and store it as the tests' run fixture (``make fixture-run``).

Plays ``configs/aurora-efficiency.fast.yaml`` in attack mode, with its honest twin, in a stack, and packs the
run directory into ``tests/fixtures/run.tar.gz``: what grading and the tests of a run's records read. Each
play's checkout, its histories and the agents' session homes are left out; a test seeds the checkout from the
company's code. The run is kept in ``logs/`` like any other, and one in which a model call failed is not
stored. Needs Docker and ``OPENROUTER_API_KEY``, and spends a few cents.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from loc_arena.checkout_history import HISTORY_DIRECTORY
from loc_arena.episode_stack import REPOSITORY, run_in_stack
from loc_arena.merged_main import CHECKOUT, DEPLOYED_CHECKOUT, DEPLOYED_FILE, HISTORY
from loc_arena.own_sessions import LOOPS_DIRECTORY

RUN = "aurora-efficiency.fast"
FIXTURE = REPOSITORY / "tests" / "fixtures" / "run.tar.gz"
# Rebuilt from the rest, or too large to store: the reports, the call record's raw form, and each play's
# checkout, histories and homes.
LEFT_OUT = frozenset(
    {"report.html", "transcript.html", "transcript.txt", "gateway.calls.otlp.jsonl"}
    | {"forge", CHECKOUT, DEPLOYED_CHECKOUT, DEPLOYED_FILE, HISTORY, HISTORY_DIRECTORY, LOOPS_DIRECTORY},
)


def keep(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    """``member`` unless a part of its path is left out of the fixture."""
    return None if LEFT_OUT & set(Path(member.name).parts) else member


def main() -> None:
    """Play the run, then pack its directory; the fixture is replaced only by a whole new one."""
    run_directory = run_in_stack(RUN, mode="attack", seed=None, logs=REPOSITORY / "logs")
    failed = json.loads((run_directory / "scores.json").read_text())["failed_model_calls"]
    if any(failed.values()):
        raise SystemExit(f"model calls failed ({failed}), so {run_directory} is not stored as the fixture")
    packed = FIXTURE.with_suffix(".new")
    with tarfile.open(packed, "w:gz") as archive:
        archive.add(run_directory, arcname=run_directory.name, filter=keep)
    packed.replace(FIXTURE)
    print(f"stored {run_directory.name} in {FIXTURE.relative_to(REPOSITORY)}")


if __name__ == "__main__":
    main()
