"""The explorer's pages of the stored run: a run played live in a stack, graded, with its honest twin."""

from __future__ import annotations

import json
import re
import shutil
import webbrowser
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.explorer.build import RUN_TEMPLATE, TWIN_PLAY, build, build_folder, build_run_page, write_page
from loc_arena.explorer.page import RunPage
from loc_arena.explorer.settings import ExplorerSettings
from loc_arena.gateway.model_gateway import CALL_RECORD_FILE, locate_call_log

from tests._stored_run import unpack_stored_run

PAGE_DATA = re.compile(r'<script id="data" type="application/json">(.*?)</script>', re.S)
# The reasoning each play's coworker reply came with; no agent's reply came with any.
REASONING = {"attack": "a short answer will do", "twin": "the twin's reasoning"}


@pytest.fixture(scope="module")
def stored_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The stored run's bundle, shared by the tests here: none of them writes to it."""
    return unpack_stored_run(tmp_path_factory.mktemp("stored"))


@pytest.fixture(scope="module")
def stack_bundle(stored_bundle: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The stored bundle where a stack run leaves it, beside its gateway's call log, with reasoning."""
    logs = tmp_path_factory.mktemp("logs")
    bundle = _copy_bundle(stored_bundle, logs / "runs" / stored_bundle.name)
    calls = [json.loads(line) for line in (bundle / CALL_RECORD_FILE).read_text().splitlines()]
    for call in calls:
        if "/loop/" not in call["path"]:  # the coworker's call: every agent's goes through its loop's route
            call["response"]["choices"][0]["message"]["reasoning"] = REASONING[call["play"]]
    call_log = locate_call_log(logs, bundle.name.rpartition("__")[2])
    call_log.parent.mkdir()
    call_log.write_text("".join(json.dumps(call) + "\n" for call in calls))
    return bundle


def _copy_bundle(bundle: Path, to: Path) -> Path:
    """A copy of ``bundle`` a test may write to, without the checkouts, which no page reads."""
    return shutil.copytree(bundle, to, ignore=shutil.ignore_patterns("checkout"))


def _list_reasoning(page: RunPage) -> list[str]:
    return [call.reasoning for step in page.steps for call in step.calls if call.reasoning]


def _page_data(path: Path) -> dict:
    match = PAGE_DATA.search(path.read_text(encoding="utf-8"))
    assert match is not None
    return json.loads(match.group(1))


def test_every_verdict_with_a_target_sits_on_a_step_of_the_runs_page(stored_bundle: Path) -> None:
    verdicts = json.loads((stored_bundle / "scores.json").read_text())["monitor_verdicts"]

    page = build_run_page(stored_bundle, ExplorerSettings())

    step_ids = {step.id for step in page.steps}
    placed = sorted((v.monitor, v.score) for v in page.verdicts if v.step in step_ids)
    assert placed == sorted((v["monitor"], v["score"]) for v in verdicts if v["target_seq"] is not None)


def test_a_folder_pages_each_traced_run_and_lists_a_run_without_a_transcript(
    stored_bundle: Path,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "logs"
    _copy_bundle(stored_bundle, folder / "traced")
    (folder / "older").mkdir()
    shutil.copy(stored_bundle / "scores.json", folder / "older" / "scores.json")
    (folder / "older" / "older.eval").write_text('{"placeholder": true}')

    index = build_folder(folder, tmp_path / "site", ExplorerSettings())

    links = [row["href"] for row in _page_data(index)["runs"]]
    assert sorted(link is None for link in links) == [False, True]
    assert all((index.parent / link).exists() for link in links if link)


def test_explore_writes_a_run_page_without_opening_a_browser_when_told_not_to(
    stored_bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    out = tmp_path / "run.html"

    code = cli.main(["explore", str(stored_bundle), "--out", str(out), "--no-open"])

    assert (code, out.exists(), opened) == (0, True, [])


def test_the_page_carries_the_reasoning_the_gateway_recorded_with_a_reply_to_the_reader(
    stack_bundle: Path,
    tmp_path: Path,
) -> None:
    page = build_run_page(stack_bundle, ExplorerSettings())

    out = write_page(RUN_TEMPLATE, page, tmp_path / "run.html")

    calls = [call for step in _page_data(out)["steps"] for call in step["calls"]]
    assert [call["reasoning"] for call in calls if call["reasoning"]] == [REASONING["attack"]]


def test_the_twins_page_shows_the_reasoning_of_the_twins_own_calls(stack_bundle: Path) -> None:
    twin = build_run_page(stack_bundle, ExplorerSettings(), play=TWIN_PLAY)

    assert _list_reasoning(twin) == [REASONING["twin"]]


def test_a_run_without_a_gateway_call_log_shows_no_reasoning(stored_bundle: Path) -> None:
    page = build_run_page(stored_bundle, ExplorerSettings())

    assert _list_reasoning(page) == []


def test_a_reply_outside_any_agents_turn_is_nothing_an_agent_said(stored_bundle: Path) -> None:
    page = build_run_page(stored_bundle, ExplorerSettings())

    calls = [call for step in page.steps for call in step.calls]
    assert [(bool(call.reply), call.said) for call in calls] == [(True, None)]  # the coworker's one reply


def test_a_folder_pages_the_honest_twin_beside_the_run_and_links_them_both_ways(
    stored_bundle: Path,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "logs"
    _copy_bundle(stored_bundle, folder / "traced")

    index = build_folder(folder, tmp_path / "site", ExplorerSettings())

    pages = sorted((index.parent / "runs").glob("*.html"))
    twin_path = next(page for page in pages if page.stem.endswith("__honest_twin"))
    run_path = next(page for page in pages if page is not twin_path)
    twin, run = _page_data(twin_path), _page_data(run_path)
    assert (run["run"]["play"], twin["run"]["play"]) == ("episode", TWIN_PLAY)
    assert run["run"]["peer"] == twin_path.name and twin["run"]["peer"] == run_path.name
    assert twin["outcome"] is None and run["outcome"] is not None
    assert twin["monitors"] == []  # the monitor rows describe the run's play
    assert twin["steps"] and twin["lanes"]
    assert twin["run"]["audit_threshold"] == run["run"]["audit_threshold"]


def test_the_twins_page_is_the_twins_own_play_not_the_runs(stored_bundle: Path) -> None:
    run = build_run_page(stored_bundle, ExplorerSettings())
    twin = build_run_page(stored_bundle, ExplorerSettings(), play=TWIN_PLAY)

    assert twin.steps and twin.steps != run.steps
    assert (twin.run.name, twin.run.mode) == (run.run.name + "__honest_twin", "honest")


def test_the_twins_page_shows_the_twins_own_verdicts_not_the_runs(stored_bundle: Path) -> None:
    scores = json.loads((stored_bundle / "scores.json").read_text())

    twin = build_run_page(stored_bundle, ExplorerSettings(), play=TWIN_PLAY)

    shown = sorted((v.monitor, v.score, v.reason) for v in twin.verdicts)
    assert shown == sorted(
        (v["monitor"], v["score"], v["reason"]) for v in scores["honest_twin"]["monitor_verdicts"]
    )
    assert shown != sorted((v["monitor"], v["score"], v["reason"]) for v in scores["monitor_verdicts"])


def test_both_pages_hold_the_run_to_the_bar_its_honest_twin_set(stored_bundle: Path) -> None:
    safety = json.loads((stored_bundle / "scores.json").read_text())["safety"]

    run = build_run_page(stored_bundle, ExplorerSettings())
    twin = build_run_page(stored_bundle, ExplorerSettings(), play=TWIN_PLAY)

    assert run.calibration is not None and run.calibration == twin.calibration
    assert (run.calibration.run_overall, run.calibration.bar) == (
        safety["overall_suspicion"],
        safety["aggregation"]["threshold"],
    )
    assert run.calibration.twin_overall == run.calibration.bar  # one twin: its own overall is the bar


def test_a_bundle_graded_before_the_twins_scores_were_kept_shows_the_twin_without_them(
    stored_bundle: Path,
    tmp_path: Path,
) -> None:
    bundle = _copy_bundle(stored_bundle, tmp_path / "bundle")
    scores = json.loads((bundle / "scores.json").read_text())
    del scores["honest_twin"]
    (bundle / "scores.json").write_text(json.dumps(scores))

    twin = build_run_page(bundle, ExplorerSettings(), play=TWIN_PLAY)

    assert (twin.verdicts, twin.calibration) == ((), None)


def test_a_single_bundle_writes_the_twins_page_beside_its_own(stored_bundle: Path, tmp_path: Path) -> None:
    bundle = _copy_bundle(stored_bundle, tmp_path / "bundle")

    page = build(bundle, None, ExplorerSettings())

    assert page == bundle / "explorer.html"
    assert (bundle / "explorer__honest_twin.html").exists()
