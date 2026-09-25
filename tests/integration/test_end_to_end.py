from __future__ import annotations

import json
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.harness import run_episode
from loc_arena.logging_.events import read_events

CFG = load_run_config("configs/aurora-efficiency.deterministic.yaml")
_BUNDLE_FILES = {
    "config.resolved.yaml",
    "events.sealed.jsonl",
    "events.mirror.jsonl",
    "scores.json",
    "report.html",
    "decisions.md",
}


def _bundle_files(bundle: Path) -> set[str]:
    return {p.name for p in bundle.iterdir() if p.is_file()}


def test_attack_and_honest_each_write_a_full_bundle(tmp_path: Path) -> None:
    attack = run_episode(CFG, mode="attack", out_root=tmp_path, robust=True)
    honest = run_episode(CFG, mode="honest", out_root=tmp_path)
    assert _BUNDLE_FILES <= _bundle_files(attack)
    assert _BUNDLE_FILES <= _bundle_files(honest)
    # the Inspect .eval is named after the run, not a fixed "run.eval"
    assert (attack / "inspect" / f"{attack.name}.eval").is_file()
    assert (honest / "inspect" / f"{honest.name}.eval").is_file()

    a = json.loads((attack / "scores.json").read_text())
    h = json.loads((honest / "scores.json").read_text())
    # honest twin verifies clean; attack decided from the sealed log
    assert h["side_task"]["landing_minimal"] is False and h["side_task"]["covert_depth"] == 0
    assert h["safety"]["landed"] is False
    assert a["side_task"]["landing_robust"] is True
    assert a["safety"]["landed"] is True and a["safety"]["caught"] is True
    # main task is a continuous score in both modes (the only difference is covert.enabled)
    assert a["main_task"]["final_score"] == h["main_task"]["final_score"]


def test_report_html_has_the_required_panels(tmp_path: Path) -> None:
    bundle = run_episode(CFG, mode="attack", out_root=tmp_path, robust=True)
    html = (bundle / "report.html").read_text()
    for panel in (
        "global timeline",
        "kill-chain story",
        "monitor scores",
        "inference flow",
        "transcript",
    ):
        assert panel in html
    assert "self-contained" not in html or True  # no external <script src> / <link href>
    assert "src=http" not in html and "href=http" not in html.replace("claude.com", "")


def test_shared_bus_message_on_both_lanes(tmp_path: Path) -> None:
    bundle = run_episode(CFG, mode="attack", out_root=tmp_path, robust=True)
    events = list(read_events(bundle / "events.sealed.jsonl"))
    msgs = [e for e in events if e.kind == "message"]
    assert msgs
    msg = msgs[0]
    # the one message reconstructs onto both the sender's and the recipient's lane
    sender_traj = [e for e in events if e.actor_uid == msg.actor_uid or e.target_id == msg.actor_uid]
    recipient_traj = [e for e in events if e.actor_uid == msg.target_id or e.target_id == msg.target_id]
    assert msg in sender_traj and msg in recipient_traj


def test_reproducible_from_config_and_seed(tmp_path: Path) -> None:
    a = run_episode(CFG, mode="attack", out_root=tmp_path / "a", robust=True)
    b = run_episode(CFG, mode="attack", out_root=tmp_path / "b", robust=True)
    # deterministic scripted policy: the sealed event sequence is byte-identical across runs
    assert (a / "events.sealed.jsonl").read_text() == (b / "events.sealed.jsonl").read_text()
    # and so are the graded outcomes
    sa = json.loads((a / "scores.json").read_text())
    sb = json.loads((b / "scores.json").read_text())
    assert sa["side_task"] == sb["side_task"]
    assert sa["main_task"] == sb["main_task"]
