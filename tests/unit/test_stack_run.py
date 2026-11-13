"""A stack run's flow over compose: each play, what is copied out and when, and the twin's renewal.

Compose is a fake that records each call; the run directory, the logs and the merged main are real files.
"""

from __future__ import annotations

import json
import socket
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from loc_arena import episode_stack
from loc_arena.config import load_run_config
from loc_arena.episode_stack import StackError, run_in_stack
from loc_arena.gateway import model_gateway
from loc_arena.observability import EXPORT_SPANS
from loc_arena.stack_play import DEPLOY, renew_services
from loc_arena.tasks.main_task_grader import COMPANY_REPOS

from tests.unit._reasoning_support import read_shown_reasoning, write_bundle, write_call_log
from tests.unit._stack_support import COMPOSE_PREFIX_LENGTH, NOTES_LOG, FakeDocker, start_as

SCRIPTED_CONFIG = load_run_config("configs/aurora-efficiency.yaml")
IAM_DB = str(SCRIPTED_CONFIG.settings.iam.database)
AURORA_AGENTS = ["AGENT_MAIN", "CONTROLPLANE_AGENT", "DATAPIPE_AGENT", "DISTILL_AGENT", "EVAL_AGENT"]
AURORA_AGENTS += ["JOBSVC_AGENT", "SERVING_AGENT"]
# The services agents hold an identity on, in the order a stack run's compose file declares them.
SERVICES = tuple("notes forge chat jobsvc agents evalkit tickets modelstore deploy iam".split())
# The containers whose log a stack run copies, in that order: those, Phoenix, the fleet and the tools gateway.
LOGGED = (*SERVICES[:-1], "phoenix", "iam", "node-1", "node-2", "node-3", "node-4", "agentgateway")


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeDocker]:
    with socket.create_server(("127.0.0.1", 0)) as gateway:
        fake = FakeDocker(published=f"127.0.0.1:{gateway.getsockname()[1]}")
        monkeypatch.setattr(subprocess, "run", fake)
        monkeypatch.setattr(subprocess, "Popen", start_as(fake))  # the reads of the forge, which are streamed
        yield fake


def run_aurora_in_stack(logs: Path, mode: str = "honest") -> None:
    run_in_stack("aurora-efficiency", mode=mode, seed=None, logs=logs)


def read_copied_log(logs: Path, service: str) -> bytes:
    """The log of ``service`` a stack run copied into its run directory under ``logs``."""
    (copied,) = logs.glob(f"*/*/services/{service}.log")
    return copied.read_bytes()


def test_a_stack_run_hands_compose_a_distinct_identity_per_agent_and_service_through_its_environment_alone(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    identities = {name: value for name, value in docker.environment.items() if "_IDENTITY_" in name}
    written = "".join(path.read_text() for path in (tmp_path / "compose").iterdir()) + str(docker.calls)
    assert (
        sorted(identities),
        len(set(identities.values())),
        [v for v in identities.values() if v in written],
    ) == (
        sorted(f"LOC_ARENA_IDENTITY_{s.upper()}_{a}" for s in SERVICES for a in AURORA_AGENTS),
        len(SERVICES) * len(AURORA_AGENTS),
        [],
    )


def test_a_stack_run_copies_each_live_services_log_out_before_it_removes_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "notes")
    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    assert (copied, ["logs", "--no-color", "--no-log-prefix", "notes"] in commands, docker.verbs[-1]) == (
        NOTES_LOG,
        True,
        "down",
    )


@pytest.mark.parametrize(
    ("failing", "mode", "plays_passing"),
    [("notes", "honest", 0), ("python3.13", "honest", 0), ("python3.13", "attack", 1)],
    ids=["a-live-services-log", "phoenixs-spans-at-the-end-of-play", "phoenixs-spans-before-the-twin"],
)
def test_a_record_that_cannot_be_copied_out_keeps_the_project(
    docker: FakeDocker,
    tmp_path: Path,
    failing: str,
    mode: str,
    plays_passing: int,
) -> None:
    docker.failing = failing  # a live service's log, not the gateway's, or the export in a running Phoenix
    docker.plays_passing = plays_passing

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path, mode)

    assert (docker.verbs[-1], "down" in docker.verbs) == ("stop", False)


def test_a_stack_run_whose_episode_never_started_copies_each_live_services_log_before_it_removes_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.episode_created_only = True

    with pytest.raises(StackError, match="never started"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "notes")
    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    assert (copied, ["logs", "--no-color", "--no-log-prefix", "notes"] in commands, docker.verbs[-1]) == (
        NOTES_LOG,
        True,
        "down",
    )


def test_a_stack_run_whose_episode_never_started_says_a_live_service_may_have_failed_to_become_healthy(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.episode_created_only = True

    with pytest.raises(StackError) as raised:
        run_aurora_in_stack(tmp_path)

    (services,) = tmp_path.glob("*/*/services")
    assert "live service" in str(raised.value) and str(services) in str(raised.value)


def test_a_live_services_log_that_cannot_be_copied_keeps_the_project_though_the_episode_never_started(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.episode_created_only = True
    docker.failing = "logs"

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    assert (docker.verbs[-1], "down" in docker.verbs) == ("stop", False)


def test_a_stack_run_hands_the_tools_gateway_its_config_through_its_environment_alone_without_a_token(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    config = docker.environment["LOC_ARENA_AGENTGATEWAY_CONFIG"]
    tokens = [
        value for name, value in docker.environment.items() if name.startswith("LOC_ARENA_SANDBOX_TOKEN_")
    ]
    written = "".join(path.read_text() for path in (tmp_path / "compose").iterdir()) + str(docker.calls)
    assert (len(tokens), [token for token in tokens if token in config], config in written) == (7, [], False)


def test_a_stack_run_copies_the_tools_gateways_log_out_before_it_removes_the_project(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "agentgateway")
    copy_command = ["logs", "--no-color", "--no-log-prefix", "agentgateway"]
    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    assert (copied, copy_command in commands, docker.verbs[-1]) == (NOTES_LOG, True, "down")


@pytest.fixture
def copied_at_grading(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The run directory's logs and call record as its events are built and graded, in place of both."""
    seen: list[list[str]] = []

    def list_copied_logs(config: object, run_directory: Path, **_: object) -> Path:
        logs = [*(run_directory / "services").iterdir(), *run_directory.glob("gateway.calls.jsonl")]
        seen.append(sorted(path.name for path in logs))
        return run_directory

    monkeypatch.setattr(episode_stack, "build_recorded_run_events", list_copied_logs)
    monkeypatch.setattr(episode_stack, "grade_run", list_copied_logs)
    return seen


def test_a_stack_run_copies_each_services_log_and_the_call_record_into_its_run_directory_before_grading(
    docker: FakeDocker,
    tmp_path: Path,
    copied_at_grading: list[list[str]],
) -> None:
    docker.plays_passing = 1

    run_aurora_in_stack(tmp_path)

    copied = [*(f"{service}.log" for service in LOGGED), "phoenix.spans.jsonl", "gateway.calls.jsonl"]
    assert copied_at_grading == [sorted(copied)] * 2


def test_a_stack_run_grades_its_play_on_what_it_merged_on_the_forges_main(
    docker: FakeDocker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker.plays_passing = 1
    graded: dict[str, list[str]] = {}

    def list_graded_checkout(config: object, run_directory: Path, **_: object) -> Path:
        checkout = run_directory / "episode" / "checkout"
        graded.update(
            {repo.name: sorted(path.name for path in repo.iterdir()) for repo in checkout.iterdir()},
        )
        return run_directory

    monkeypatch.setattr(episode_stack, "build_recorded_run_events", lambda *_, **__: None)
    monkeypatch.setattr(episode_stack, "grade_run", list_graded_checkout)

    run_aurora_in_stack(tmp_path)

    assert graded == dict.fromkeys(COMPANY_REPOS, ["MERGED.md"])


def test_a_graded_stack_run_copies_its_run_directory_out_once(
    docker: FakeDocker,
    tmp_path: Path,
    copied_at_grading: list[list[str]],
) -> None:
    docker.plays_passing = 1

    run_aurora_in_stack(tmp_path)

    outputs = [
        call for call in docker.calls if call[COMPOSE_PREFIX_LENGTH] == "cp" and call[-1] == str(tmp_path)
    ]
    assert len(outputs) == 1


def test_a_graded_stack_runs_inspect_log_gets_the_reasoning_its_gateway_recorded_once_the_record_is_copied(
    docker: FakeDocker,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def grade(config: object, run_directory: Path, **_: object) -> Path:
        write_bundle(run_directory, {"episode": "the reply"})
        return run_directory

    def copy_call_log(run: object, compose: object, settings: object, call_log: Path) -> bool:
        write_call_log(call_log, [("the reply", "I should not do that.")])
        return True

    docker.plays_passing = 1
    monkeypatch.setattr(episode_stack, "build_recorded_run_events", lambda config, run_directory: None)
    monkeypatch.setattr(episode_stack, "grade_run", grade)
    monkeypatch.setattr(model_gateway, "copy_call_log", copy_call_log)

    run_aurora_in_stack(tmp_path)

    (bundle,) = {path.parent for path in tmp_path.glob("*/*/*.eval")}
    assert read_shown_reasoning(bundle) == {"episode": (["I should not do that."], "the reply")}


def test_an_attack_stack_run_drains_the_record_then_renews_every_container_but_the_episode_for_the_twin(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    plays = [index for index, command in enumerate(commands) if command[0] == "up" and "--attach" in command]
    between = [  # up to the company's server's deploy, which comes right before each play
        command[:2] if command[0] == "cp" else command for command in commands[plays[0] + 1 : plays[1] - 2]
    ]
    renewed = ["sandbox-agent-main", "sandbox-serving-agent", "sandbox-distill-agent", "sandbox-eval-agent"]
    renewed += ["sandbox-datapipe-agent", "sandbox-jobsvc-agent", "sandbox-controlplane-agent"]
    renewed += ["runner-1", "runner-2", "runner-3", *LOGGED]
    read = (
        [
            "exec",
            "-T",
            "forge",
            *("timeout", "--signal=KILL", "30.0"),  # forge.git_timeout_seconds, kept in the container too
            "git",
            "--no-replace-objects",
            "--git-dir",
            f"/var/lib/forge/{repo}.git",
            *command,
        ]
        for repo in COMPANY_REPOS
        for command in (
            ["archive", "--format=tar", "refs/heads/main"],
            ["bundle", "create", "--quiet", "-", "--all"],
        )
    )
    assert between == [
        *read,  # what the episode merged, and the forge's history, read before the forge is renewed
        ["logs", "--no-color", "--no-log-prefix", "deploy"],  # then the episode's last deployment, if any
        ["stop", *(service for service in renewed if service != "phoenix")],  # Phoenix runs on to export
        ["logs", "--no-color", "--no-log-prefix", "gateway"],
        ["cp", "collector:/sealed/calls.jsonl"],
        *(["logs", "--no-color", "--no-log-prefix", service] for service in LOGGED),
        ["ps", "--status", "running", "--quiet", "phoenix"],
        ["exec", "-T", "phoenix", "python3.13", "-c", EXPORT_SPANS, "http://localhost:6006"],
        [
            "up",
            "--detach",
            "--wait",
            "--force-recreate",
            "--renew-anon-volumes",
            "--no-deps",
            "gateway",
            *renewed,
        ],
        ["up", "--detach", "--wait", "gateway"],
        ["port", "gateway", "8080"],
    ]


def test_the_company_server_is_submitted_by_its_deployer_from_iams_container_right_before_each_play(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    commands = [call[COMPOSE_PREFIX_LENGTH:] for call in docker.calls]
    plays = [index for index, command in enumerate(commands) if command[0] == "up" and "--attach" in command]
    deploys = [(commands[play - 2], commands[play - 1]) for play in plays]
    assert [(started[:4], submitted[:7]) for started, submitted in deploys] == [
        (
            ["up", "--detach", "--wait", "--no-recreate"],
            ["exec", "-T", "iam", "python", "-c", DEPLOY, IAM_DB],
        ),
    ] * 2
    assert {(*submitted[7:10], *json.loads(submitted[10])["command"][:5]) for _, submitted in deploys} == {
        (
            "serving-deploy",
            "jobsvc",
            "http://jobsvc:8000",
            "python",
            "-m",
            "meridian_serving.http",
            "--model",
            "aurora-7b-v3",
        ),
    }


def test_a_company_server_that_does_not_load_stops_the_run_before_any_play(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.failing = DEPLOY  # the deploy's exec in IAM's container exits non-zero

    with pytest.raises(StackError, match="serving-deploy's server did not load"):
        run_aurora_in_stack(tmp_path, mode="attack")

    assert docker.plays == 0


def test_a_stack_runs_service_log_holds_the_lines_of_the_episodes_container_and_then_the_honest_twins(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    assert read_copied_log(tmp_path, "notes") == NOTES_LOG * 2


def test_a_log_that_cannot_be_copied_before_the_honest_twin_keeps_the_project_and_renews_nothing(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1
    docker.failing = "node-1"  # the compose service named by its log's copy alone

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path, mode="attack")

    renewed = [call for call in docker.calls if "--force-recreate" in call]
    assert (renewed, docker.plays, docker.verbs[-1]) == ([], 1, "stop")


def test_containers_that_do_not_come_back_for_the_honest_twin_stop_the_run_with_every_log_copied_out(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1
    docker.failing = "--force-recreate"

    with pytest.raises(StackError, match="did not come back for the honest twin"):
        run_aurora_in_stack(tmp_path, mode="attack")

    copied = read_copied_log(tmp_path, "notes")
    assert (copied, docker.plays, docker.verbs[-1]) == (NOTES_LOG * 2, 1, "down")


def test_the_honest_twin_plays_in_the_episodes_container_though_its_image_tag_moved(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1

    with pytest.raises(StackError, match="exited with code 1"):
        run_aurora_in_stack(tmp_path, mode="attack")

    plays = [call for call in docker.calls if "--attach" in call]
    assert ["--no-recreate" in play for play in plays] == [True, True]


def test_a_log_file_that_cannot_be_written_before_the_honest_twin_renews_nothing_and_writes_no_log(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.log").mkdir()
    compose = ["docker", "compose", "-p", "locarena-test", "-f", "compose.yaml", "--project-directory", "."]

    with pytest.raises(StackError, match="nothing was renewed"):
        renew_services(SCRIPTED_CONFIG, compose, tmp_path, {}, {})

    renewed = [call for call in docker.calls if "--force-recreate" in call]
    assert (renewed, sorted(path.name for path in tmp_path.iterdir())) == ([], ["notes.log"])


def test_a_log_file_that_cannot_be_written_at_teardown_keeps_the_project_after_every_other_copy(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.unwritable_after_first_play = tmp_path

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    copied = read_copied_log(tmp_path, "agentgateway")
    assert (copied, docker.verbs[-1], "down" in docker.verbs) == (NOTES_LOG, "stop", False)


def test_a_service_log_copied_before_a_failed_grading_is_not_added_again_at_teardown(
    docker: FakeDocker,
    tmp_path: Path,
) -> None:
    docker.plays_passing = 1
    docker.failing = "agentgateway"  # its copy fails, so the run stops before grading

    with pytest.raises(StackError, match="kept compose project"):
        run_aurora_in_stack(tmp_path)

    assert read_copied_log(tmp_path, "notes") == NOTES_LOG
