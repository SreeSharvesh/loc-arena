"""Monitors run again over the stored run: each play's score, the cost, and nothing of the run changed."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from loc_arena.cli import main
from loc_arena.config import ConfigError, ModelSpec
from loc_arena.gateway.core import API_KEY_VARIABLE, GATEWAY_URL_VARIABLE, Message, ProviderResult, ToolSpec
from loc_arena.monitor_rerun import SCORES_FILE, VERDICTS_FILE, Rerun, rerun_monitors

from tests._stored_run import CONFIG, unpack_stored_run

MONITORS_PATH = Path("configs/monitors.yaml")
PER_AGENT, TEAM = "agent_trajectory_monitor", "team_summary_monitor"
WHOLE_TEAM = "team_trajectory_monitor"
AGENTS = [  # the stored run's team
    "agent-main",
    "controlplane-agent",
    "datapipe-agent",
    "distill-agent",
    "eval-agent",
    "jobsvc-agent",
    "serving-agent",
]
PLAYS = ["attack", "twin"]
SAMPLES = 3  # ensemble_n of each trajectory monitor in configs/monitors.yaml


class PricedVerdictProvider:
    """A provider whose every reply is a verdict of 0.9, at 100 prompt and 10 completion tokens and $0.001.

    With ``unpriced_first``, its first reply reports no cost.
    """

    def __init__(self, *, unpriced_first: bool = False) -> None:
        """Record the model of every call."""
        self.models: set[str] = set()
        self._unpriced = unpriced_first

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
        *,
        spec: ModelSpec | None = None,
    ) -> ProviderResult:
        self.models.add(model)
        reply = '{"suspicion": 0.9, "reason": "stub verdict"}'
        cost, self._unpriced = (None if self._unpriced else 0.001), False
        return ProviderResult(text=reply, prompt_tokens=100, completion_tokens=10, cost=cost)


def _digest(directory: Path) -> dict[str, str]:
    return {
        str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def stored_run(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return unpack_stored_run(tmp_path_factory.mktemp("stored"))


@pytest.fixture(scope="module")
def rerun(stored_run: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Rerun, PricedVerdictProvider]:
    """The trajectory monitors run over the stored run, read back from the scores file the re-run wrote."""
    out, provider = tmp_path_factory.mktemp("rerun"), PricedVerdictProvider()
    monitors = [PER_AGENT, TEAM, WHOLE_TEAM]
    rerun_monitors(CONFIG, stored_run, monitors, out, monitors_path=MONITORS_PATH, provider=provider)
    return Rerun.model_validate_json((out / SCORES_FILE).read_text()), provider


def test_a_rerun_scores_each_agent_of_each_play(rerun: tuple[Rerun, PricedVerdictProvider]) -> None:
    plays = rerun[0].monitors[PER_AGENT].plays

    assert list(plays) == PLAYS
    assert all(play.score == 0.9 and play.by_actor == dict.fromkeys(AGENTS, 0.9) for play in plays.values())


@pytest.mark.parametrize(
    ("monitor", "calls_per_play"),
    [(PER_AGENT, len(AGENTS) * SAMPLES), (TEAM, len(AGENTS) + SAMPLES), (WHOLE_TEAM, SAMPLES)],
    ids=["each agent sampled", "each agent summarized, then the team sampled", "the team sampled"],
)
def test_a_rerun_reports_what_each_monitors_calls_came_to(
    rerun: tuple[Rerun, PricedVerdictProvider],
    monitor: str,
    calls_per_play: int,
) -> None:
    usage = rerun[0].monitors[monitor].usage

    calls = calls_per_play * len(PLAYS)
    assert (usage.calls, usage.prompt_tokens, usage.completion_tokens) == (calls, calls * 100, calls * 10)
    assert usage.dollars == pytest.approx(calls * 0.001)


def test_a_rerun_reports_no_dollars_when_a_call_reported_none(stored_run: Path, tmp_path: Path) -> None:
    provider = PricedVerdictProvider(unpriced_first=True)

    rerun = rerun_monitors(
        CONFIG,
        stored_run,
        [WHOLE_TEAM],
        tmp_path / "out",
        monitors_path=MONITORS_PATH,
        provider=provider,
    )

    usage = rerun.monitors[WHOLE_TEAM].usage
    assert usage.calls == SAMPLES * len(PLAYS)
    assert usage.dollars is None  # five calls reported $0.001 each: their sum is not the total


def test_a_rerun_calls_the_model_its_monitors_declare(rerun: tuple[Rerun, PricedVerdictProvider]) -> None:
    assert rerun[1].models == {"deepseek/deepseek-v4.1-flash"}  # the trajectory_monitor role's, not Llama


def test_a_rerun_changes_nothing_of_the_run(stored_run: Path, tmp_path: Path) -> None:
    before = _digest(stored_run)

    rerun_monitors(
        CONFIG,
        stored_run,
        [PER_AGENT],
        tmp_path / "out",
        monitors_path=MONITORS_PATH,
        provider=PricedVerdictProvider(),
    )

    assert _digest(stored_run) == before


@pytest.mark.parametrize("earlier_rerun", [False, True], ids=["the run's own folder", "an earlier re-run's"])
def test_a_rerun_refuses_a_folder_that_holds_something(
    stored_run: Path,
    tmp_path: Path,
    earlier_rerun: bool,
) -> None:
    out, provider = (tmp_path if earlier_rerun else stored_run), PricedVerdictProvider()
    if earlier_rerun:
        rerun_monitors(CONFIG, stored_run, [PER_AGENT], out, monitors_path=MONITORS_PATH, provider=provider)
    before = _digest(out)

    with pytest.raises(FileExistsError):
        rerun_monitors(CONFIG, stored_run, [PER_AGENT], out, monitors_path=MONITORS_PATH, provider=provider)

    assert _digest(out) == before  # the run's scores.json, or the earlier re-run's records, as they were


def test_a_rerun_refuses_a_monitor_the_config_does_not_declare(stored_run: Path, tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="no_such_monitor"):
        rerun_monitors(CONFIG, stored_run, ["no_such_monitor"], tmp_path, monitors_path=MONITORS_PATH)


def test_the_monitor_command_writes_as_many_verdicts_as_the_run_recorded(
    stored_run: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable in (API_KEY_VARIABLE, GATEWAY_URL_VARIABLE):  # no model: the action monitor's rule scores
        monkeypatch.delenv(variable, raising=False)
    out = tmp_path / "out"
    command = ["monitor", str(stored_run), "--monitors", "action_monitor", "--out", str(out)]

    status = main([*command, "--run", "aurora-efficiency.fast"])

    lines = [json.loads(line) for line in (out / VERDICTS_FILE).read_text().splitlines()]
    recorded = json.loads((stored_run / "scores.json").read_text())["monitors"][0]
    assert status == 0
    assert recorded["name"] == "action_monitor"
    assert sum(line["play"] == "attack" for line in lines) == recorded["n_verdicts"]
