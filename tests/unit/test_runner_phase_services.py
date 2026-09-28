"""The stack's code path without docker: the runner phase over the services' apps, then the host's grading.

The runner phase runs the deterministic config against the recorder, the core, the edge and one execution
app per agent (over TestClient, reached through the runner's own clients); the host then grades what the
services wrote, as it grades what it copies out of a real stack. Both must match the in-process golden run.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.execution.checkout import COMPANY_ROOT, list_repositories
from loc_arena.gateway.core import DeterministicProvider, ToolSpec
from loc_arena.grader.measurement import measure_checkout
from loc_arena.harness import DEFAULT_MONITORS_PATH, apply_mode, open_bundle, score_and_write_bundle
from loc_arena.scaffold.agent import AgentPolicy, ScriptedAgentPolicy, Transcript
from loc_arena.scaffold.tools import AgentAction, AgentContext
from loc_arena.stack.constants import EVENTS_FILE_NAME
from loc_arena.stack.contracts import RunnerEpisodeExport
from loc_arena.stack_episode import StackEpisode, _grade_on_host, run_runner_phase
from loc_arena.task import _resolve_scenario

from tests.unit._stack_services import ServedStack, serve_stack

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
GOLDEN = Path(__file__).parent / "golden" / "aurora-efficiency.deterministic"
MODES = ("attack", "honest")


def _run_served_episode(directory: Path, mode: str) -> tuple[RunnerEpisodeExport, ServedStack]:
    config = apply_mode(CONFIG, mode)
    served = serve_stack(directory, config)
    export = run_runner_phase(
        config,
        served.services,
        robust=True,
        output_directory=directory / "runner",
        mirror_root=served.gateway.mirror_root,
    )
    return export, served


@pytest.fixture(scope="module")
def served_runs(
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, tuple[RunnerEpisodeExport, ServedStack]]:
    return {mode: _run_served_episode(tmp_path_factory.mktemp(mode), mode) for mode in MODES}


def _grade(export: RunnerEpisodeExport, served: ServedStack, mode: str) -> StackEpisode:
    config = apply_mode(CONFIG, mode)
    measurement = measure_checkout(
        served.checkout,
        _resolve_scenario(config).reference_dir,
        config.settings.grading,
        repositories=list_repositories(COMPANY_ROOT),
    )
    return _grade_on_host(
        config,
        export,
        measurement,
        sealed_directory=served.gateway.sealed_root / export.handle,
        mirror_path=served.gateway.mirror_root / export.handle / EVENTS_FILE_NAME,
        max_bytes=config.settings.docker.evidence_max_bytes,
    )


def _scores_without_wall_clock(path: Path) -> dict[str, object]:
    scores = json.loads(path.read_text())
    del scores["wall_clock_seconds"]
    return scores


@pytest.mark.parametrize("mode", MODES)
def test_the_recorder_writes_the_golden_sealed_log(
    served_runs: dict[str, tuple[RunnerEpisodeExport, ServedStack]],
    mode: str,
) -> None:
    export, served = served_runs[mode]

    written = (served.gateway.sealed_root / export.handle / EVENTS_FILE_NAME).read_bytes()

    assert written == (GOLDEN / mode / "events.sealed.jsonl").read_bytes()


@pytest.mark.parametrize("mode", MODES)
def test_the_edge_writes_the_golden_mirror_log(
    served_runs: dict[str, tuple[RunnerEpisodeExport, ServedStack]],
    mode: str,
) -> None:
    export, served = served_runs[mode]

    written = (served.gateway.mirror_root / export.handle / EVENTS_FILE_NAME).read_bytes()

    assert written == (GOLDEN / mode / "events.mirror.jsonl").read_bytes()


def test_the_runner_exports_the_verdicts_the_in_process_run_scored(
    served_runs: dict[str, tuple[RunnerEpisodeExport, ServedStack]],
) -> None:
    export, _ = served_runs["attack"]

    exported = [verdict.model_dump() for verdict in export.verdicts]

    assert exported == json.loads((GOLDEN / "attack" / "scores.json").read_text())["monitor_verdicts"]


def test_the_host_scores_the_served_episodes_like_the_in_process_run(
    served_runs: dict[str, tuple[RunnerEpisodeExport, ServedStack]],
    tmp_path: Path,
) -> None:
    attack, honest = (_grade(*served_runs[mode], mode) for mode in MODES)
    config = apply_mode(CONFIG, "attack")
    bundle = open_bundle(config, "attack", out_root=tmp_path, seed=None, run_instance_id="golden")

    directory = score_and_write_bundle(
        config,
        "attack",
        bundle,
        attack,
        honest,
        monitors_path=DEFAULT_MONITORS_PATH,
        write_report=False,
    )

    golden = _scores_without_wall_clock(GOLDEN / "attack" / "scores.json")
    assert _scores_without_wall_clock(directory / "scores.json") == golden


class BashProbePolicy:
    """A scripted agent policy that runs one bash command, then reads its output back from the transcript."""

    def __init__(self, command: str) -> None:
        """Run ``command`` on the first turn; keep what it printed."""
        self._command = command
        self.output: str | None = None

    def next_actions(self, uid: str, turn: int, transcript: Transcript) -> list[AgentAction] | None:
        if not transcript:
            return [AgentAction("bash", {"command": self._command})]
        self.output = str(transcript[-1]["result"]["output"])
        return None


def test_a_live_policy_agent_reads_back_what_its_bash_printed_in_its_sandbox(tmp_path: Path) -> None:
    config = dataclasses.replace(apply_mode(CONFIG, "honest"), policy="model")
    served = serve_stack(tmp_path, config, provider=DeterministicProvider())
    probe = BashProbePolicy("echo from-the-sandbox; pwd")

    def policies(ctx: AgentContext, brief: str, tool_specs: list[ToolSpec]) -> AgentPolicy:
        return probe if ctx.uid == "serving-agent" else ScriptedAgentPolicy([])

    run_runner_phase(
        config,
        served.services,
        robust=False,
        output_directory=tmp_path / "runner",
        mirror_root=served.gateway.mirror_root,
        policy_factory=policies,
    )

    assert probe.output == f"from-the-sandbox\n{served.checkout.resolve()}\n"
