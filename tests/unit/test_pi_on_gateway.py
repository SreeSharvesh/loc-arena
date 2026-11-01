"""pi-on-gateway writes pi's home before it becomes pi: its provider, and its settings."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from sandbox_server import pi_on_gateway


class BecamePi(Exception):  # noqa: N818 - what os.execvp would do, as an exception the test can catch
    pass


def test_a_runs_pi_compacts_nothing_since_its_trace_would_miss_the_summarizing_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOC_ARENA_GATEWAY_API", "http://gateway/runner/api/v1")
    monkeypatch.setattr(sys, "argv", ["pi-on-gateway", "model", "Summarize.", "hi"])
    monkeypatch.setattr(sys, "stdin", (tmp_path / "stdin").open("w+"))  # pytest's own has no descriptor
    monkeypatch.setattr(os, "dup2", lambda *_: None)

    def become_pi(*_: object) -> None:
        raise BecamePi

    monkeypatch.setattr(os, "execvp", become_pi)

    with pytest.raises(BecamePi):
        pi_on_gateway.main()

    settings = json.loads((tmp_path / ".pi" / "agent" / "settings.json").read_text())
    assert settings == {"compaction": {"enabled": False}}
