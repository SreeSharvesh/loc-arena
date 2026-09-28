"""The host's side of a stack episode, against a stand-in for the docker CLI (docker cannot run here).

The stand-in is a real executable first on ``PATH``: it logs every call (its arguments, and the control key
file compose would read) and plays the containers' part: the runner writes its export into its bind-mounted
output directory, ``cp`` copies the golden logs out, the grader prints a measurement.
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from loc_arena.compose_stack import (
    EpisodeStack,
    HarnessError,
    run_in_runner,
    teardown,
    up,
    write_control_key_file,
)
from loc_arena.config import load_run_config
from loc_arena.stack.constants import RUNNER_EPISODE_EXPORT_FILE_NAME
from loc_arena.stack.contracts import (
    EpisodeLanes,
    GradeMeasurement,
    MonitorVerdictRecord,
    RunnerEpisodeExport,
)
from loc_arena.stack_episode import StackEpisode, _grade_on_host, _measure_checkout, run_stack_episode

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
GOLDEN = Path(__file__).parent / "golden" / "aurora-efficiency.deterministic" / "attack"
HANDLE = "0123456789abcdef"
MEASUREMENT = GradeMeasurement(suite_green=True, pipeline_completed=True, cost=3335, outputs={"metric": 1.0})

FAKE_DOCKER = """#!{python}
import json, os, pathlib, shutil, sys, time
arguments = sys.argv[1:]
scenario = json.loads(pathlib.Path(os.environ["FAKE_DOCKER_SCENARIO"]).read_text())
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    key_file = os.environ.get("LOC_ARENA_CONTROL_KEY_FILE")
    log.write(json.dumps({{"arguments": arguments, "control_key_file": key_file}}) + "\\n")
if "run" in arguments:
    time.sleep(scenario.get("run_seconds", 0))
    if "grader" in arguments:
        sys.stdout.write(scenario.get("grader_stdout", ""))
    if "--volume" in arguments:
        output = pathlib.Path(arguments[arguments.index("--volume") + 1].split(":")[0])
        (output / "{export_name}").write_text(scenario["export"])
if "cp" in arguments:
    source, destination = arguments[-2:]
    shutil.copytree(scenario["copies"][source.split(":/")[1].split("/")[0]], destination)
"""


@dataclasses.dataclass(frozen=True)
class DockerCall:
    """One call of the stand-in: its arguments, and the control key file compose would have read."""

    arguments: list[str]
    control_key_file: str | None


@dataclasses.dataclass(frozen=True)
class FakeDocker:
    """The stand-in's call log and the scenario it plays."""

    log: Path
    scenario: Path

    def play(self, **scenario: object) -> None:
        self.scenario.write_text(json.dumps(scenario))

    def calls(self) -> list[DockerCall]:
        return [DockerCall(**json.loads(line)) for line in self.log.read_text().splitlines()]


@pytest.fixture
def docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    executable = bin_directory / "docker"
    executable.write_text(
        FAKE_DOCKER.format(python=sys.executable, export_name=RUNNER_EPISODE_EXPORT_FILE_NAME),
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    fake = FakeDocker(log=tmp_path / "docker.log", scenario=tmp_path / "scenario.json")
    fake.log.touch()
    fake.play()
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_DOCKER_SCENARIO", str(fake.scenario))
    return fake


@pytest.fixture
def stack(docker: FakeDocker, tmp_path: Path) -> EpisodeStack:
    return up(CONFIG, project="locarena-unit", workdir=tmp_path / "stack")


def _golden_export() -> RunnerEpisodeExport:
    scores = json.loads((GOLDEN / "scores.json").read_text())
    first = json.loads((GOLDEN / "events.sealed.jsonl").read_text().splitlines()[0])
    return RunnerEpisodeExport(
        handle=HANDLE,
        episode_id=first["episode_id"],
        deadline_ts=CONFIG.settings.clock.deadline_ts,
        last_sealed_seq=33,
        verdicts=tuple(MonitorVerdictRecord(**verdict) for verdict in scores["monitor_verdicts"]),
        turns=(),
        lanes=EpisodeLanes(sealed={}, mirror={}),
        phases={},
        mirror_to_sealed={},
    )


def _evidence(tmp_path: Path) -> dict[str, str]:
    """The golden logs laid out as the evidence reader holds one handle's: sealed/ and mirror/."""
    sealed, mirror = tmp_path / "golden-sealed", tmp_path / "golden-mirror"
    sealed.mkdir()
    mirror.mkdir()
    (sealed / "events.jsonl").write_bytes((GOLDEN / "events.sealed.jsonl").read_bytes())
    (mirror / "events.jsonl").write_bytes((GOLDEN / "events.mirror.jsonl").read_bytes())
    return {"sealed": str(sealed), "mirror": str(mirror)}


@pytest.fixture
def stack_episode(docker: FakeDocker, tmp_path: Path) -> StackEpisode:
    docker.play(
        export=_golden_export().model_dump_json(),
        copies=_evidence(tmp_path),
        grader_stdout=MEASUREMENT.model_dump_json() + "\n",
    )
    config = dataclasses.replace(CONFIG, agent_transcript=False)
    return run_stack_episode(
        "aurora-efficiency.deterministic",
        config,
        "attack",
        tmp_path / "episode",
        robust=True,
        provider_key=None,
    )


def _subcommands(docker: FakeDocker) -> list[str]:
    """Each compose call's subcommand (the argument after the project directory and any profile)."""
    subcommands: list[str] = []
    for call in docker.calls():
        arguments = call.arguments
        if arguments[:1] == ["compose"]:
            rest = arguments[arguments.index("--project-directory") + 2 :]
            subcommands.append(rest[2] if rest[:1] == ["--profile"] else rest[0])
    return subcommands


def test_every_compose_command_of_a_stack_names_its_control_key_file(
    stack: EpisodeStack,
    docker: FakeDocker,
) -> None:
    teardown(stack)

    assert {call.control_key_file for call in docker.calls()} == {str(stack.control_key_file)}


def test_the_stack_images_are_built_for_every_profile(stack: EpisodeStack, docker: FakeDocker) -> None:
    first = docker.calls()[0]

    assert first.arguments[-3:] == ["--profile", "*", "build"]


def test_teardown_deletes_the_control_key_file(stack: EpisodeStack) -> None:
    teardown(stack)

    assert not stack.control_key_file.exists()


def test_the_control_key_file_is_readable_by_every_container_user() -> None:
    key_file = write_control_key_file()

    assert stat.S_IMODE(key_file.stat().st_mode) == 0o444


def test_the_control_key_directory_is_open_to_its_owner_only() -> None:
    key_file = write_control_key_file()

    assert stat.S_IMODE(key_file.parent.stat().st_mode) == 0o700


def test_a_runner_past_its_wall_clock_limit_is_removed(stack: EpisodeStack, docker: FakeDocker) -> None:
    docker.play(run_seconds=5)

    with pytest.raises(HarnessError, match="wall-clock limit"):
        run_in_runner(stack, ["python", "-c", "pass"], timeout_seconds=0.5)

    run, removal = docker.calls()[-2:]
    assert removal.arguments == ["rm", "--force", run.arguments[run.arguments.index("--name") + 1]]


def test_the_graders_last_line_is_its_measurement(stack: EpisodeStack, docker: FakeDocker) -> None:
    docker.play(grader_stdout="a suite printed this\n" + MEASUREMENT.model_dump_json() + "\n")

    measurement = _measure_checkout(stack, CONFIG)

    assert measurement == MEASUREMENT


def test_a_grader_line_that_is_no_measurement_is_graded_as_a_broken_pipeline(
    stack: EpisodeStack,
    docker: FakeDocker,
) -> None:
    docker.play(grader_stdout='{"suite_green": true}\n')

    measurement = _measure_checkout(stack, CONFIG)

    assert measurement.pipeline_completed is False


def test_a_grader_printing_past_its_output_cap_is_graded_as_a_broken_pipeline(
    stack: EpisodeStack,
    docker: FakeDocker,
) -> None:
    cap = CONFIG.settings.grading.max_output_bytes
    docker.play(grader_stdout="x" * cap + "\n" + MEASUREMENT.model_dump_json() + "\n")

    measurement = _measure_checkout(stack, CONFIG)

    assert measurement.pipeline_completed is False


def test_the_host_freezes_the_sandboxes_and_the_edge_before_it_copies_the_logs(
    stack_episode: StackEpisode,
    docker: FakeDocker,
) -> None:
    subcommands = _subcommands(docker)

    assert subcommands == ["build", "up", "run", "stop", "cp", "cp", "run", "down"]


def test_the_host_stops_every_agents_sandbox_and_the_edge(
    stack_episode: StackEpisode,
    docker: FakeDocker,
) -> None:
    (stop,) = (call.arguments for call in docker.calls() if "stop" in call.arguments)

    stopped = set(stop[stop.index("stop") + 1 :])

    assert stopped == {f"sandbox-{agent.id}" for agent in CONFIG.agents} | {"gateway_edge"}


def test_the_host_copies_the_logs_without_following_links(
    stack_episode: StackEpisode,
    docker: FakeDocker,
) -> None:
    copies = [call.arguments for call in docker.calls() if "cp" in call.arguments]

    assert not any("-L" in arguments or "--follow-link" in arguments for arguments in copies)


def test_the_host_verifies_the_side_task_on_the_copied_sealed_log(stack_episode: StackEpisode) -> None:
    landing = stack_episode.verdict.landing

    assert (landing.minimal, landing.robust) == (True, True)


def test_the_host_scores_the_graders_measurement_against_the_sealed_reference(
    stack_episode: StackEpisode,
) -> None:
    main_score = stack_episode.main_score

    assert (main_score.gate_reason, main_score.achieved_cost) == ("outputs_out_of_tolerance", 3335)


def test_the_host_refuses_a_sealed_log_holding_another_episodes_events(tmp_path: Path) -> None:
    evidence = _evidence(tmp_path)
    export = _golden_export().model_copy(update={"episode_id": "another-episode"})

    with pytest.raises(HarnessError, match="holds events of"):
        _grade_on_host(
            CONFIG,
            export,
            MEASUREMENT,
            sealed_directory=Path(evidence["sealed"]),
            mirror_path=Path(evidence["mirror"]) / "events.jsonl",
            max_bytes=CONFIG.settings.docker.evidence_max_bytes,
        )
