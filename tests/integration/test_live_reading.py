"""A reading of the stored run in real containers: the reader reports through the gateway, with no key.

Skipped unless the live provider is enabled: a reading spends model calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.run_reader import READER, READINGS, read_run

from tests._stored_run import CONFIG, unpack_stored_run

pytestmark = pytest.mark.skipif(
    not (os.environ.get("LOC_ARENA_LIVE") == "1" and os.environ.get(API_KEY_VARIABLE)),
    reason=f"live provider disabled (set LOC_ARENA_LIVE=1 + {API_KEY_VARIABLE})",
)


@pytest.fixture(scope="module")
def reading(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return read_run(unpack_stored_run(tmp_path_factory.mktemp("reading")), "agents", CONFIG)


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
