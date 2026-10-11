from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.config import load_run_config
from loc_arena.gateway.core import Move, load_moves
from loc_arena.harness import apply_mode
from pydantic import ValidationError

RUN = load_run_config("configs/aurora-efficiency.deterministic.yaml")
SEND = "{tool: send, args: {to: eval-agent, body: routine}}"
JOB = "{tool: submit_job, args: {spec: benchmark}}"


def _write_moves(directory: Path, text: str) -> Path:
    path = directory / "moves.yaml"
    path.write_text(text)
    return path


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (f"intruder-agent: {{moves: [{SEND}]}}\n", ValueError),
        ("agent-main: {moves: [{args: {to: eval-agent}}]}\n", ValidationError),
        (f"agent-main: {{moves: [{SEND}], extra_moves: []}}\n", ValidationError),
    ],
    ids=["unknown agent", "move without a tool", "unknown key"],
)
def test_a_moves_file_naming_an_unknown_agent_or_a_malformed_move_is_refused(
    tmp_path: Path,
    text: str,
    error: type[Exception],
) -> None:
    path = _write_moves(tmp_path, text)

    with pytest.raises(error):
        load_moves(path, RUN)


def test_a_move_with_content_from_carries_that_files_text_as_its_content(tmp_path: Path) -> None:
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt" / "near.py").write_text("FAST = True\n")
    path = _write_moves(
        tmp_path,
        "datapipe-agent: {moves: [{tool: write_file, args: {path: near.py}, content_from: opt/near.py}]}\n",
    )

    moves = load_moves(path, RUN)

    assert moves == {
        "datapipe-agent": [Move(tool="write_file", args={"path": "near.py", "content": "FAST = True\n"})],
    }


@pytest.mark.parametrize(
    ("mode", "tools"),
    [("attack", ["send", "submit_job"]), ("honest", ["send"])],
)
def test_attack_moves_are_played_only_when_covert_is_enabled(
    tmp_path: Path,
    mode: str,
    tools: list[str],
) -> None:
    path = _write_moves(tmp_path, f"agent-main: {{moves: [{SEND}], attack_moves: [{JOB}]}}\n")

    moves = load_moves(path, apply_mode(RUN, mode))

    assert [move.tool for move in moves["agent-main"]] == tools
