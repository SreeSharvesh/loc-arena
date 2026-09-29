"""The host's side of a stack episode, against a stand-in docker CLI first on ``PATH``."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from loc_arena.compose_stack import (
    EpisodeStack,
    HarnessError,
    name_compose_project,
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
from loc_arena.stack_episode import StackEpisode, _grade_on_host, run_stack_episode

from tests.unit._golden import GOLDEN

CONFIG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
HANDLE = "0123456789abcdef"
MEASUREMENT = GradeMeasurement(suite_green=True, pipeline_completed=True, cost=3335, outputs={"metric": 1.0})
# The tag grammar of pkg.go.dev/github.com/distribution/reference.
IMAGE_TAG = re.compile(r"[\w][\w.-]{0,127}")
# `loc-arena run --stack` in a process of its own, reading no .env: argv[1] names the run, argv[2] the output.
RUN_STACK_CLI = (
    "import functools, sys\n"
    "from loc_arena import cli, stack_episode\n"
    "cli.run_in_stack = functools.partial(stack_episode.run_in_stack, dotenv_path=None)\n"
    "sys.exit(cli.main(['run', '--run', sys.argv[1], '--stack', '--out', sys.argv[2]]))\n"
)
RUNNER_SECONDS = 60  # the stand-in's runner outlasts the test, so only the signal ends the run
CALL_WAIT_SECONDS = 30
POLL_SECONDS = 0.1

FAKE_DOCKER = """#!{python}
import json, os, pathlib, shutil, sys, time
arguments = sys.argv[1:]
scenario = json.loads(pathlib.Path(os.environ["FAKE_DOCKER_SCENARIO"]).read_text())
with open(os.environ["FAKE_DOCKER_LOG"], "a") as log:
    call = {{
        "arguments": arguments,
        "control_key_file": os.environ.get("LOC_ARENA_CONTROL_KEY_FILE"),
        "image_tag": os.environ.get("LOC_ARENA_IMAGE_TAG"),
    }}
    log.write(json.dumps(call) + "\\n")
if scenario.get("fail") in arguments:
    sys.stderr.write("Cannot connect to the Docker daemon\\n")
    sys.exit(1)
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
    arguments: list[str]
    control_key_file: str | None
    image_tag: str | None


@dataclasses.dataclass(frozen=True)
class FakeDocker:
    log: Path
    scenario: Path

    def play(self, **scenario: object) -> None:
        self.scenario.write_text(json.dumps(scenario))

    def calls(self) -> list[DockerCall]:
        return [DockerCall(**json.loads(line)) for line in self.log.read_text().splitlines()]


@pytest.fixture(autouse=True)
def _control_keys_in_tmp_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write each test's control key files under its own ``tmp_path``, which pytest cleans up.

    ``up`` writes one per stack and only ``teardown`` deletes it: in the temp directory, a test that never
    tears its stack down would leave a key file behind on every run.
    """
    keys = tmp_path / "keys"
    keys.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(keys))


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
    scores = json.loads((GOLDEN / "attack" / "scores.json").read_text())
    first = json.loads((GOLDEN / "attack" / "events.sealed.jsonl").read_text().splitlines()[0])
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
    sealed, mirror = tmp_path / "golden-sealed", tmp_path / "golden-mirror"
    sealed.mkdir()
    mirror.mkdir()
    (sealed / "events.jsonl").write_bytes((GOLDEN / "attack" / "events.sealed.jsonl").read_bytes())
    (mirror / "events.jsonl").write_bytes((GOLDEN / "attack" / "events.mirror.jsonl").read_bytes())
    return {"sealed": str(sealed), "mirror": str(mirror)}


def _run_stack_episode(docker: FakeDocker, tmp_path: Path, grader_stdout: str) -> StackEpisode:
    docker.play(
        export=_golden_export().model_dump_json(),
        copies=_evidence(tmp_path),
        grader_stdout=grader_stdout,
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


@pytest.fixture
def stack_episode(docker: FakeDocker, tmp_path: Path) -> StackEpisode:
    return _run_stack_episode(docker, tmp_path, MEASUREMENT.model_dump_json() + "\n")


def _subcommands(docker: FakeDocker) -> list[str]:
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


def test_every_compose_command_of_a_stack_names_its_image_tag(
    stack: EpisodeStack,
    docker: FakeDocker,
) -> None:
    teardown(stack)

    assert {call.image_tag for call in docker.calls()} == {stack.project}


def test_two_episodes_of_a_run_build_their_images_under_different_tags(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    for episode in ("first", "second"):
        up(CONFIG, project=name_compose_project("aurora-efficiency"), workdir=tmp_path / episode)

    build_tags = [call.image_tag for call in docker.calls() if call.arguments[-1:] == ["build"]]
    assert len(set(build_tags)) == len(build_tags) == 2


def test_a_run_name_past_the_tag_length_still_names_a_valid_image_tag() -> None:
    project = name_compose_project("a" * 300)

    assert IMAGE_TAG.fullmatch(project)


def test_the_stack_images_are_built_for_every_profile(stack: EpisodeStack, docker: FakeDocker) -> None:
    first = docker.calls()[0]

    assert first.arguments[-3:] == ["--profile", "*", "build"]


def test_teardown_deletes_the_control_key_file(stack: EpisodeStack) -> None:
    teardown(stack)

    assert not stack.control_key_file.exists()


def test_a_teardown_docker_refuses_names_the_project_it_left_behind(
    stack: EpisodeStack,
    docker: FakeDocker,
    capsys: pytest.CaptureFixture[str],
) -> None:
    docker.play(fail="down")

    teardown(stack)

    assert stack.project in capsys.readouterr().err


def test_a_teardown_docker_completes_prints_nothing(
    stack: EpisodeStack,
    capsys: pytest.CaptureFixture[str],
) -> None:
    teardown(stack)

    assert capsys.readouterr().err == ""


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


def test_the_host_scores_the_graders_last_line(docker: FakeDocker, tmp_path: Path) -> None:
    grader_stdout = "a suite printed this\n" + MEASUREMENT.model_dump_json() + "\n"

    episode = _run_stack_episode(docker, tmp_path, grader_stdout)

    assert episode.main_score.achieved_cost == MEASUREMENT.cost


def test_a_grader_line_that_is_no_measurement_is_graded_as_a_broken_pipeline(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    episode = _run_stack_episode(docker, tmp_path, '{"suite_green": true}\n')

    assert episode.main_score.gate_reason == "grade_error"


def test_a_grader_printing_past_its_output_cap_is_graded_as_a_broken_pipeline(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    cap = CONFIG.settings.grading.max_output_bytes
    grader_stdout = "x" * cap + "\n" + MEASUREMENT.model_dump_json() + "\n"

    episode = _run_stack_episode(docker, tmp_path, grader_stdout)

    assert episode.main_score.gate_reason == "grade_error"


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
            MEASUREMENT.model_dump_json().encode(),
            sealed_directory=Path(evidence["sealed"]),
            mirror_path=Path(evidence["mirror"]) / "events.jsonl",
        )


def _wait_for_subcommand(docker: FakeDocker, subcommand: str) -> None:
    deadline = time.monotonic() + CALL_WAIT_SECONDS
    while subcommand not in _subcommands(docker):
        assert time.monotonic() < deadline, f"no compose {subcommand} within {CALL_WAIT_SECONDS} s"
        time.sleep(POLL_SECONDS)


def _end_a_stack_run_by_signal(docker: FakeDocker, tmp_path: Path, signal_number: signal.Signals) -> str:
    """Start ``loc-arena run --stack``, send it ``signal_number`` while its runner runs; return its stderr."""
    docker.play(run_seconds=RUNNER_SECONDS)
    command = [sys.executable, "-c", RUN_STACK_CLI, "aurora-efficiency.deterministic", str(tmp_path / "runs")]
    with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) as run:
        _wait_for_subcommand(docker, "run")
        run.send_signal(signal_number)
        _, stderr = run.communicate(timeout=CALL_WAIT_SECONDS)
    return stderr


ENDING_SIGNALS = pytest.mark.parametrize(
    "signal_number",
    [signal.SIGINT, signal.SIGTERM, signal.SIGHUP],
    ids=lambda number: number.name,
)


@ENDING_SIGNALS
def test_a_stack_run_ended_by_a_signal_tears_its_stack_down(
    docker: FakeDocker,
    tmp_path: Path,
    signal_number: signal.Signals,
) -> None:
    _end_a_stack_run_by_signal(docker, tmp_path, signal_number)

    assert _subcommands(docker)[-1] == "down"


@ENDING_SIGNALS
def test_a_stack_run_ended_by_a_signal_reports_it_in_one_line(
    docker: FakeDocker,
    tmp_path: Path,
    signal_number: signal.Signals,
) -> None:
    stderr = _end_a_stack_run_by_signal(docker, tmp_path, signal_number)

    assert stderr == f"error: stopped by {signal_number.name}\n"
