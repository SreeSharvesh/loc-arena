"""A reading of the stored run in real containers: the reader reports through the gateway, with no key.

Skipped unless the live provider is enabled: a reading spends model calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.run_reader import READER, READINGS

from tests._stored_run import CONFIG, unpack_stored_run

STORED_RUN_CONFIG = "aurora-efficiency.fast"  # the config the stored run played
READING_SECONDS = 900  # a reading of the stored run takes a few minutes

pytestmark = pytest.mark.skipif(
    not (os.environ.get("LOC_ARENA_LIVE") == "1" and os.environ.get(API_KEY_VARIABLE)),
    reason=f"live provider disabled (set LOC_ARENA_LIVE=1 + {API_KEY_VARIABLE})",
)


@pytest.fixture(scope="module")
def reading(tmp_path_factory: pytest.TempPathFactory) -> Path:
    run_directory = unpack_stored_run(tmp_path_factory.mktemp("reading"))
    command = [sys.executable, "-m", "loc_arena.cli", "read", str(run_directory), "--run", STORED_RUN_CONFIG]
    # The command as a person runs it, its input left open: a reader that waits for input never ends.
    with subprocess.Popen(command, stdin=subprocess.PIPE) as process:
        try:
            assert process.wait(timeout=READING_SECONDS) == 0
        except subprocess.TimeoutExpired:
            process.kill()
            raise
    return run_directory / READINGS / "agents.md"


def test_a_reading_opens_with_the_facts_and_holds_the_readers_account(reading: Path) -> None:
    head, _, account = reading.read_text().partition("\n- Twin verdicts:")

    assert head.startswith("## Facts counted from the records")
    assert len(account.split()) > 100


def test_every_call_of_a_reading_is_the_readers_and_recorded(reading: Path) -> None:
    calls = [json.loads(line) for line in (reading.parent / "agents.calls.jsonl").read_text().splitlines()]

    assert calls
    asked = {(call["caller"], call["request"]["model"]) for call in calls}
    assert asked == {(READER, CONFIG.models[READER].model)}


def test_a_reading_holds_no_provider_key(reading: Path) -> None:
    key = os.environ[API_KEY_VARIABLE]

    holding = [path.name for path in reading.parent.iterdir() if key in path.read_text(errors="replace")]

    assert (reading.parent.name, holding) == (READINGS, [])
