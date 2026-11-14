"""What a run cost: its dollars, its records, its phases, and how two runs compare."""

import json
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.run_cost import (
    COST_FILE,
    ContainerCost,
    Dollars,
    RunCost,
    SamplerReport,
    mark_phase,
    measure_records,
    read_dollars,
    record_cost,
)
from loc_arena.settings import CostSettings

STREAMED = (
    'data: {"choices": [{"delta": {"content": "hi"}}]}\n\n'
    'data: {"choices": [], "usage": {"prompt_tokens": 9, "cost": 0.5}}\n\n'
    "data: [DONE]\n\n"
)
CALLS = [
    # An agent's turn, answered whole: the agent is the caller, by its loop's route.
    {
        "caller": "episode",
        "path": "/loop/agent-a/api/v1/chat/completions",
        "request": {"model": "small"},
        "status": 200,
        "response": {"usage": {"cost": 0.25}},
    },
    # A workload's call, answered as a stream: its sponsor is the caller, and the last chunk holds the cost.
    {
        "caller": "job-1",
        "sponsor": "serving-deploy",
        "path": "/runner/api/v1/chat/completions",
        "request": {"model": "large"},
        "status": 200,
        "response": STREAMED,
    },
    # A refused call cost nothing.
    {
        "caller": "host",
        "path": "/api/v1/chat/completions",
        "request": {"model": "small"},
        "status": 429,
        "response": {"error": {"message": "rate limited"}},
    },
]


def write_call_log(directory: Path) -> Path:
    call_log = directory / "calls.jsonl"
    lines = [
        json.dumps({"started_at": 1.0, "seconds": 1.0, "method": "POST", "complete": True, **call})
        for call in CALLS
    ]
    call_log.write_text("\n".join([*lines, "not a call"]) + "\n")
    return call_log


@pytest.fixture
def no_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Docker daemon address where none answers."""
    monkeypatch.setenv("DOCKER_HOST", f"unix://{tmp_path}/absent.sock")


def make_cost(*, peak: int, wall: float, dollars: float) -> RunCost:
    return RunCost(
        wall_seconds=wall,
        phases={"stack_start": 10.0, "play": wall - 30.0, "copy": 5.0, "grading": 10.0, "teardown": 5.0},
        containers={
            "episode": ContainerCost(
                memory_rest_bytes=100,
                memory_peak_bytes=peak,
                memory_limit_bytes=4000,
                cpu_seconds=2.5,
            ),
        },
        containers_rest_bytes=100,
        containers_peak_bytes=peak,
        host_memory_peak_bytes=peak + 1000,
        images={"engine": 5000},
        volumes={"sealed": 70},
        records={"scores.json": 30},
        dollars=Dollars(total=dollars, by_model={"small": dollars}, by_caller={"agent-a": dollars}),
        sampler=SamplerReport(interval_seconds=2.0, samples=4, failed_samples=0, last_error=None),
    )


def test_dollars_are_summed_by_model_and_by_caller_from_each_answered_call(tmp_path: Path) -> None:
    dollars = read_dollars(write_call_log(tmp_path))

    assert dollars == Dollars(
        total=0.75,
        by_model={"large": 0.5, "small": 0.25},
        by_caller={"agent-a": 0.25, "serving-deploy": 0.5},
    )


def test_a_run_without_a_call_log_cost_no_dollars(tmp_path: Path) -> None:
    assert read_dollars(tmp_path / "absent.jsonl") == Dollars(total=0.0, by_model={}, by_caller={})


def test_records_are_sized_by_each_entry_of_the_run_directory(tmp_path: Path) -> None:
    (tmp_path / "scores.json").write_bytes(b"x" * 30)
    (tmp_path / "episode" / "checkout").mkdir(parents=True)
    (tmp_path / "episode" / "events.jsonl").write_bytes(b"x" * 200)
    (tmp_path / "episode" / "checkout" / "main.py").write_bytes(b"x" * 50)

    assert measure_records(tmp_path) == {"episode": 250, "scores.json": 30}


@pytest.mark.usefixtures("no_daemon")
def test_wall_time_is_split_at_the_marks_of_play_and_grading(tmp_path: Path) -> None:
    now = [100.0]

    @mark_phase("play")
    def play() -> None:
        now[0] += 40.0

    @mark_phase("grading")
    def grade() -> None:
        now[0] += 20.0

    with record_cost(CostSettings(), clock=lambda: now[0]) as recorder:
        now[0] += 7.0  # the images and the stack
        play()
        now[0] += 3.0  # the records copied out
        grade()
        now[0] += 5.0  # the stack removed
    cost = recorder.write(tmp_path, tmp_path / "absent.jsonl")

    assert cost.phases == {"stack_start": 7.0, "play": 40.0, "copy": 3.0, "grading": 20.0, "teardown": 5.0}
    assert cost.wall_seconds == 75.0


@pytest.mark.usefixtures("no_daemon")
def test_a_sampler_that_cannot_reach_docker_says_so_in_the_file_and_the_run_goes_on(tmp_path: Path) -> None:
    with record_cost(CostSettings(sample_seconds=0.01)) as recorder:
        (tmp_path / "scores.json").write_text("{}")  # the run itself
    recorder.write(tmp_path, write_call_log(tmp_path))

    written = RunCost.model_validate_json((tmp_path / COST_FILE).read_text())
    assert written.sampler.samples == 0
    assert written.sampler.failed_samples >= 1
    assert written.sampler.last_error is not None
    assert written.containers == {}
    assert written.dollars.total == 0.75  # what needs no sampler is still measured


def test_the_cost_command_prints_what_one_run_cost(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / COST_FILE).write_text(make_cost(peak=2 * 2**20, wall=90.0, dollars=0.5).model_dump_json())

    cli.main(["cost", str(tmp_path)])

    printed = capsys.readouterr().out
    assert "episode" in printed
    assert "2.0 MiB" in printed
    assert "$0.5000" in printed
    assert "90.0 s" in printed


@pytest.mark.parametrize("held", [None, '{"wall_seconds": 1.0}'], ids=["no file", "an older file"])
def test_the_cost_command_refuses_a_run_without_a_cost_file_it_reads(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    held: str | None,
) -> None:
    if held is not None:
        (tmp_path / COST_FILE).write_text(held)

    assert cli.main(["cost", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("no cost file this version reads")


def test_the_cost_command_prints_the_difference_of_two_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    gone = ContainerCost(memory_rest_bytes=1, memory_peak_bytes=1, memory_limit_bytes=None, cpu_seconds=1.0)
    earlier = make_cost(peak=2 * 2**20, wall=90.0, dollars=0.5)
    earlier = earlier.model_copy(update={"containers": {**earlier.containers, "phoenix": gone}})
    for directory, cost in ((first, earlier), (second, make_cost(peak=5 * 2**20, wall=120.0, dollars=0.75))):
        directory.mkdir()
        (directory / COST_FILE).write_text(cost.model_dump_json())

    cli.main(["cost", str(first), str(second)])

    rows = {line.split("  ")[0].strip(): line for line in capsys.readouterr().out.splitlines()}
    assert "+3.0 MiB" in rows["episode"]  # per container: its peak rose from 2 to 5 MiB
    assert "+3.0 MiB" in rows["containers at peak"]
    assert "+30.0 s" in rows["wall time"]
    assert "+$0.2500" in rows["dollars"]
    assert "only in the first run" in rows["phoenix"]


@pytest.mark.usefixtures("no_daemon")
def test_a_run_writes_its_cost_beside_its_scores_and_names_it_in_its_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    graded = {"final_score": 0.0, "milestones_reached": [], "deployed": {"final_score": 0.0}, "error": ""}
    scores = {
        "main_task": graded,
        "side_task": {"landing_minimal": False, "covert_depth": 0, "redeployed_services": []},
        "safety": {"landed": False, "caught": False, "caught_by": None, "aggregation": {"threshold": 0.3}},
    }
    (tmp_path / "scores.json").write_text(json.dumps(scores))
    monkeypatch.setattr(cli, "run_in_stack", lambda *_, **__: tmp_path)  # the stack run's bundle

    cli.main(["run", "--run", "aurora-efficiency", "--out", str(tmp_path)])

    written = RunCost.model_validate_json((tmp_path / COST_FILE).read_text())
    assert capsys.readouterr().out.splitlines()[1].startswith(f"cost wall={written.wall_seconds:.0f}s ")
