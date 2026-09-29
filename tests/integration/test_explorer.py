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
from loc_arena.explorer.build import build_folder, build_run_page
from loc_arena.explorer.settings import ExplorerSettings
from loc_arena.harness import run_episode
from loc_arena.logging_.transcript_lanes import WORLD

from tests.integration._live_support import LANDING_QUEUES, QueuedProvider

LIVE_TRACED = dataclasses.replace(
    load_run_config("configs/aurora-efficiency.deterministic.yaml"),
    policy="model",
    agent_transcript=True,
)
PAGE_DATA = re.compile(r'<script id="data" type="application/json">(.*?)</script>', re.S)


@pytest.fixture(scope="module")
def traced_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One traced attack run, written by main's own harness, shared by the tests here (it takes seconds)."""
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("OPENROUTER_API_KEY", raising=False)
        return run_episode(
            LIVE_TRACED,
            mode="attack",
            out_root=tmp_path_factory.mktemp("runs"),
            provider=QueuedProvider(LANDING_QUEUES),
            calibration_provider=QueuedProvider({}),
            write_report=False,
        )


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
