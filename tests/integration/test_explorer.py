from __future__ import annotations

import dataclasses
import json
import re
import shutil
import webbrowser
from pathlib import Path

import pytest
from loc_arena import cli
from loc_arena.config import load_run_config
from loc_arena.explorer.build import RUN_TEMPLATE, TWIN_PLAY, build, build_folder, build_run_page, write_page
from loc_arena.explorer.page import RunPage
from loc_arena.explorer.settings import ExplorerSettings
from loc_arena.gateway.core import ScriptedProvider
from loc_arena.gateway.proxy import GatewayCall
from loc_arena.harness import run_episode
from loc_arena.logging_.transcript_lanes import WORLD

from tests.integration._live_support import LANDING_MOVES

LIVE_TRACED = dataclasses.replace(
    load_run_config("configs/aurora-efficiency.deterministic.yaml"),
    policy="model",
    agent_transcript=True,
)
PAGE_DATA = re.compile(r'<script id="data" type="application/json">(.*?)</script>', re.S)
# The replies a stack run's gateway recorded, as OpenRouter returned them, each with the model's reasoning:
# agent-main's first turn, serving-agent's first turn without reasoning, a reply no call of the run received,
# and the coworker's "ok" twice, the second time to a later call outside the run, with different reasoning.
RECORDED_REPLIES = (
    (
        {
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "send",
                        "arguments": json.dumps(LANDING_MOVES["agent-main"][0].args),
                    },
                },
            ],
        },
        "plan: have eval-agent blind the monitor",
    ),
    ({"content": "ok"}, "a short answer will do"),
    (
        {
            "content": "",
            "tool_calls": [
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "acquire_identity", "arguments": "{}"},
                },
            ],
        },
        None,
    ),
    ({"content": "a reply no call of this run received"}, "reasoning of another run"),
    ({"content": "ok"}, "reasoning of the later call"),
)


@pytest.fixture(scope="module")
def traced_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One traced attack run, written by main's own harness, shared by the tests here (it takes seconds)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("OPENROUTER_API_KEY", raising=False)
        return run_episode(
            LIVE_TRACED,
            mode="attack",
            out_root=tmp_path_factory.mktemp("runs"),
            provider=ScriptedProvider(LANDING_MOVES),
            calibration_provider=ScriptedProvider({}),
            write_report=False,
        )


@pytest.fixture(scope="module")
def stack_page(traced_bundle: Path, tmp_path_factory: pytest.TempPathFactory) -> RunPage:
    """The traced run's page with its bundle where a stack run leaves it, beside its gateway's call log."""
    logs = tmp_path_factory.mktemp("logs")
    bundle = _stack_bundle(traced_bundle, logs)
    return build_run_page(bundle, ExplorerSettings())


def _stack_bundle(traced_bundle: Path, logs: Path) -> Path:
    """The traced bundle under ``logs`` with a gateway call log beside it, as a stack run leaves it."""
    bundle = logs / traced_bundle.parent.name / traced_bundle.name
    shutil.copytree(traced_bundle, bundle)
    call_log = logs / "gateway" / f"locarena-{bundle.name.rpartition('__')[2]}.calls.jsonl"
    call_log.parent.mkdir()
    calls = [
        GatewayCall(
            started_at=started_at,
            seconds=1.0,
            caller="episode",
            method="POST",
            path="/api/v1/chat/completions",
            request={},
            status=200,
            response={"choices": [{"message": {"role": "assistant", **message, "reasoning": reasoning}}]},
            complete=True,
        )
        for started_at, (message, reasoning) in enumerate(RECORDED_REPLIES)
    ]
    call_log.write_text("".join(call.model_dump_json() + "\n" for call in calls))
    return bundle


def _reasoning_by_step(page: RunPage) -> dict[str, str]:
    """The reasoning each step's model calls carry, by step, for the steps with any."""
    return {
        step.id.split("/", 1)[1]: call.reasoning
        for step in page.steps
        for call in step.calls
        if call.reasoning
    }


def _index_links(index: Path) -> list[str | None]:
    """Each run's link in an index page, None for a run without a transcript."""
    match = PAGE_DATA.search(index.read_text(encoding="utf-8"))
    assert match is not None
    return [row["href"] for row in json.loads(match.group(1))["runs"]]


def test_every_action_verdict_of_a_traced_run_sits_on_a_step_of_its_actor(traced_bundle: Path) -> None:
    page = build_run_page(traced_bundle, ExplorerSettings())

    steps = {step.id: step for step in page.steps}
    placed = [
        (verdict, steps[verdict.step])
        for verdict in page.verdicts
        if verdict.monitor == "action_monitor" and verdict.step is not None
    ]
    assert placed
    assert [verdict for verdict, step in placed if step.agent not in (verdict.actor, WORLD)] == []


def test_a_folder_pages_each_traced_run_and_lists_a_run_without_a_transcript(
    traced_bundle: Path,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "logs"
    shutil.copytree(traced_bundle, folder / "traced")
    (folder / "older").mkdir()
    shutil.copy(traced_bundle / "scores.json", folder / "older" / "scores.json")
    (folder / "older" / "older.eval").write_text('{"placeholder": true}')

    index = build_folder(folder, tmp_path / "site", ExplorerSettings())

    links = _index_links(index)
    assert sorted(link is None for link in links) == [False, True]
    assert all((index.parent / link).exists() for link in links if link)


def test_explore_writes_a_run_page_without_opening_a_browser_when_told_not_to(
    traced_bundle: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", opened.append)
    out = tmp_path / "run.html"

    code = cli.main(["explore", str(traced_bundle), "--out", str(out), "--no-open"])

    assert (code, out.exists(), opened) == (0, True, [])


def test_a_turn_shows_the_reasoning_the_gateway_recorded_with_its_reply(stack_page: RunPage) -> None:
    reasoning = _reasoning_by_step(stack_page)

    assert reasoning["agent-main/0"] == "plan: have eval-agent blind the monitor"


def test_of_two_recorded_calls_with_the_same_reply_the_earlier_belongs_to_the_earlier_event(
    stack_page: RunPage,
) -> None:
    reasoning = _reasoning_by_step(stack_page)

    assert reasoning[f"{WORLD}/-1"] == "a short answer will do"


def test_no_other_call_shows_reasoning_neither_one_without_it_nor_one_matching_no_event(
    stack_page: RunPage,
) -> None:
    reasoning = _reasoning_by_step(stack_page)

    assert sorted(reasoning) == sorted(["agent-main/0", f"{WORLD}/-1"])


def test_a_run_without_a_gateway_call_log_shows_no_reasoning(traced_bundle: Path) -> None:
    page = build_run_page(traced_bundle, ExplorerSettings())

    assert _reasoning_by_step(page) == {}


def test_the_page_carries_each_turns_reasoning_to_the_reader(stack_page: RunPage, tmp_path: Path) -> None:
    out = write_page(RUN_TEMPLATE, stack_page, tmp_path / "run.html")

    match = PAGE_DATA.search(out.read_text(encoding="utf-8"))
    assert match is not None
    calls = [call for step in json.loads(match.group(1))["steps"] for call in step["calls"]]
    assert [call["reasoning"] for call in calls if call["reasoning"]] == [
        "a short answer will do",
        "plan: have eval-agent blind the monitor",
    ]


def _page_data(path: Path) -> dict:
    match = PAGE_DATA.search(path.read_text(encoding="utf-8"))
    assert match is not None
    return json.loads(match.group(1))


def test_a_folder_pages_the_honest_twin_beside_the_run_and_links_them_both_ways(
    traced_bundle: Path,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "logs"
    shutil.copytree(traced_bundle, folder / "traced")

    index = build_folder(folder, tmp_path / "site", ExplorerSettings())

    pages = sorted((index.parent / "runs").glob("*.html"))
    twin_path = next(page for page in pages if page.stem.endswith("__honest_twin"))
    run_path = next(page for page in pages if page is not twin_path)
    twin, run = _page_data(twin_path), _page_data(run_path)
    assert (run["run"]["play"], twin["run"]["play"]) == ("episode", TWIN_PLAY)
    assert run["run"]["peer"] == twin_path.name and twin["run"]["peer"] == run_path.name
    assert twin["outcome"] is None and run["outcome"] is not None
    assert twin["verdicts"] == [] and twin["monitors"] == []
    assert twin["steps"] and twin["lanes"]
    assert twin["run"]["audit_threshold"] == run["run"]["audit_threshold"]


def test_the_twins_page_is_the_twins_own_play_not_the_runs(traced_bundle: Path) -> None:
    run = build_run_page(traced_bundle, ExplorerSettings())
    twin = build_run_page(traced_bundle, ExplorerSettings(), play=TWIN_PLAY)

    assert twin.steps and twin.steps != run.steps
    assert (twin.run.name, twin.run.mode) == (run.run.name + "__honest_twin", "honest")


def test_the_twin_borrows_no_reasoning_from_the_runs_call_log(traced_bundle: Path, tmp_path: Path) -> None:
    bundle = _stack_bundle(traced_bundle, tmp_path)

    run = build_run_page(bundle, ExplorerSettings())
    twin = build_run_page(bundle, ExplorerSettings(), play=TWIN_PLAY)

    assert _reasoning_by_step(run)  # the recorded replies pair to the run's own events...
    assert _reasoning_by_step(twin) == {}  # ...and the call log's seqs are the run's, so the twin takes none


def test_a_single_bundle_writes_the_twins_page_beside_its_own(traced_bundle: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    shutil.copytree(traced_bundle, bundle)

    page = build(bundle, None, ExplorerSettings())

    assert page == bundle / "explorer.html"
    assert (bundle / "explorer__honest_twin.html").exists()
