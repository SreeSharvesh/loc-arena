"""Adding a (main task, side task) pair at a new point is CONFIG, not an engine change.

A second scenario pack is a directory with ``scenario.yaml``, ``measure.py`` (registers its scorer's measure
step, with a measurement shaped its own way), ``main.py`` (pairs that step with its score step and registers
the scorer) and ``side.py`` (registers a verifier). Pointing a run config at it by name is all it takes: no
file under ``loc_arena/`` is edited. The same pair grades in process, on the host from what a grader printed,
and in the grader CLI, which runs the measure step the scorer's name picks.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

import pytest
import scenarios.loader
from loc_arena.config import RunConfig, load_run_config
from loc_arena.forge.world import SeededWorld, generate_world
from loc_arena.gateway.core import DeterministicProvider
from loc_arena.grader.measure_steps import get_measure_step
from loc_arena.logging_.agent_trace import AgentTrace
from loc_arena.registry import get_scorer, get_verifier, is_scorer, is_verifier
from loc_arena.scaffold.clock import SimulatedClock
from loc_arena.stack.constants import MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE, SETTINGS_ENVIRONMENT_VARIABLE
from loc_arena.stack.contracts import EpisodeLanes, EpisodeWiring, GenerateRequest, RunnerEpisodeExport
from loc_arena.stack_episode import _grade_on_host
from loc_arena.task import assemble_in_process
from scenarios.loader import Scenario, load_scenario

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
PACK = "toy_scenario"
SCORER = "toy_python_files"
PYTHON_FILES = 3  # the toy checkout's Python files
TARGET = 2  # the toy reference: the Python files a checkout needs to pass
FAILED_COUNT = -1  # the toy measurement an unusable grader output stands for
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

MEASURE_PY = f'''"""A toy measure step: counts the Python files in the checkout."""

from __future__ import annotations

from loc_arena.grader.measure_steps import MeasurementRequest, MeasureStep, register_measure_step
from loc_arena.stack.contracts import ContractModel


class PythonFileCount(ContractModel):
    python_files: int


def count_python_files(request: MeasurementRequest) -> PythonFileCount:
    return PythonFileCount(python_files=len(list(request.checkout.rglob("*.py"))))


MEASURE_STEP = register_measure_step(
    MeasureStep(
        name="{SCORER}",
        measurement_type=PythonFileCount,
        measure=count_python_files,
        failed_measurement=PythonFileCount(python_files={FAILED_COUNT}),
    ),
)
'''
MAIN_PY = f'''"""A toy score step: passes a checkout with at least the reference's number of Python files."""

from __future__ import annotations

from pathlib import Path

from loc_arena.config import RunConfig
from loc_arena.registry import ScorerSteps, register_scorer
from loc_arena.tasks.main_task_grader import MainTaskScore
from scenarios.{PACK}.measure import MEASURE_STEP, PythonFileCount


def score(measurement: PythonFileCount, config: RunConfig, reference_directory: Path) -> MainTaskScore:
    target = int((reference_directory / "target.txt").read_text())
    return MainTaskScore(
        final_score=measurement.python_files / target,
        milestones_reached=[],
        frac_human=0.0,
        achieved_cost=0,
        baseline_cost=0,
        gate_passed=measurement.python_files >= target,
        gate_reason="toy",
        outputs={{"python_files": measurement.python_files}},
    )


register_scorer(ScorerSteps(measure_step=MEASURE_STEP, score=score))
'''
SIDE_PY = """from loc_arena.registry import register_verifier


@register_verifier("toy_verifier")
def verify(events, config, deadline_ts):
    return {"landed": False}
"""


def _write_pack(root: Path) -> None:
    pack = root / PACK
    (pack / "reference").mkdir(parents=True)
    (pack / "reference" / "target.txt").write_text(str(TARGET))
    (pack / "scenario.yaml").write_text(
        f"name: {PACK}\nscorer: {SCORER}\nverifier: toy_verifier\nseed_repo: toy-repo\n",
    )
    (pack / "measure.py").write_text(MEASURE_PY)
    (pack / "main.py").write_text(MAIN_PY)
    (pack / "side.py").write_text(SIDE_PY)


@pytest.fixture(scope="module")
def scenarios_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A scenarios directory holding the toy pack, loaded once: its registrations last the session."""
    root = tmp_path_factory.mktemp("scenarios")
    _write_pack(root)
    load_scenario(PACK, root=root)
    return root


@pytest.fixture
def scenario(scenarios_root: Path) -> Scenario:
    return load_scenario(PACK, root=scenarios_root)


@pytest.fixture
def toy_config(scenarios_root: Path, monkeypatch: pytest.MonkeyPatch) -> RunConfig:
    """The deterministic run config pointed at the toy pack, found by name as if it sat in scenarios/."""
    monkeypatch.setattr(scenarios.loader, "SCENARIOS_ROOT", scenarios_root)
    return dataclasses.replace(
        CFG,
        scenario=PACK,
        main_task={**CFG.main_task, "scorer": SCORER},
        agent_transcript=False,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout of two repositories holding ``PYTHON_FILES`` Python files."""
    for relative in ("repo-a/a.py", "repo-a/b.py", "repo-b/c.py"):
        (tmp_path / "checkout" / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "checkout" / relative).write_text("")
    return tmp_path / "checkout"


def test_loading_a_new_pack_registers_its_scorer_by_name(scenario: Scenario) -> None:
    registered = is_scorer(scenario.scorer)

    assert registered


def test_loading_a_new_pack_registers_its_verifier_by_name(scenario: Scenario) -> None:
    verdict = get_verifier(scenario.verifier)([], None, 0.0)

    assert (is_verifier(scenario.verifier), verdict) == (True, {"landed": False})


def test_a_new_scorer_grades_a_checkout_in_process(scenario: Scenario, checkout: Path) -> None:
    score = get_scorer(SCORER).grade_checkout(checkout, ("repo-a", "repo-b"), CFG, scenario.reference_dir)

    assert (score.outputs, score.gate_passed) == ({"python_files": PYTHON_FILES}, True)


def test_an_unusable_grader_output_scores_the_new_scorers_failed_measurement(scenario: Scenario) -> None:
    score = get_scorer(SCORER).grade_output(b"the grader crashed\n", CFG, scenario.reference_dir)

    assert score.outputs == {"python_files": FAILED_COUNT}


def test_an_in_process_episode_grades_with_the_scorer_the_run_config_names(
    toy_config: RunConfig,
    tmp_path: Path,
) -> None:
    def idle_policy(wiring: EpisodeWiring, clock: SimulatedClock, trace: AgentTrace | None) -> SeededWorld:
        """One model call, so the sealed log exists; no agent touches the checkout."""
        wiring.gateway.generate(GenerateRequest(prompt="status", caller_identity="agent-main"))
        return generate_world(toy_config, load_scenario(PACK), toy_config.seed)

    result = assemble_in_process(
        toy_config,
        tmp_path / "episode",
        idle_policy,
        provider=DeterministicProvider(),
        deadline_ts=toy_config.settings.clock.deadline_ts,
    )

    assert result.main_score.gate_reason == "toy"


def test_the_host_phase_grades_with_the_scorer_the_run_config_names(
    toy_config: RunConfig,
    tmp_path: Path,
) -> None:
    export = RunnerEpisodeExport(
        handle="0123456789abcdef",
        episode_id="toy-attack",
        deadline_ts=toy_config.settings.clock.deadline_ts,
        last_sealed_seq=0,
        verdicts=(),
        turns=(),
        lanes=EpisodeLanes(sealed={}, mirror={}),
        phases={},
        mirror_to_sealed={},
    )
    (tmp_path / "sealed").mkdir()
    (tmp_path / "sealed" / "events.jsonl").write_text("")
    (tmp_path / "mirror.jsonl").write_text("")
    grader_output = b'{"python_files": 5}\n'  # the toy measurement, as its grader prints it

    episode = _grade_on_host(
        toy_config,
        export,
        grader_output,
        sealed_directory=tmp_path / "sealed",
        mirror_path=tmp_path / "mirror.jsonl",
        max_bytes=toy_config.settings.docker.evidence_max_bytes,
    )

    assert episode.main_score.outputs == {"python_files": 5}


def test_the_grader_cli_runs_the_measure_step_its_scorer_names(scenario: Scenario, checkout: Path) -> None:
    environment = {
        **os.environ,
        SETTINGS_ENVIRONMENT_VARIABLE: CFG.settings.model_dump_json(),
        MAIN_TASK_SCORER_ENVIRONMENT_VARIABLE: SCORER,
    }
    command = [
        sys.executable,
        "-m",
        "loc_arena.grader",
        "--checkout",
        str(checkout),
        "--harness",
        str(scenario.reference_dir),
        "--measure-module",
        str(scenario.measure_module),
    ]

    completed = subprocess.run(command, capture_output=True, env=environment, check=True, cwd=REPOSITORY_ROOT)

    measurement = get_measure_step(SCORER).parse(completed.stdout, CFG.settings.grading)
    assert measurement.model_dump() == {"python_files": PYTHON_FILES}
