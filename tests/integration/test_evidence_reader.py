"""The networkless evidence reader: the host's only way to the sealed and mirror logs, read-only."""

from __future__ import annotations

from pathlib import Path

import pytest
from loc_arena.compose_stack import EpisodeStack, run_compose

from tests.integration._docker_support import service_networks

pytestmark = pytest.mark.integration  # every test here brings the stack up


def test_the_evidence_reader_is_networkless(stack: EpisodeStack) -> None:
    networks = service_networks(stack, "evidence_reader")

    assert networks <= {"none"}


def test_the_evidence_reader_cannot_write_the_sealed_volume(stack: EpisodeStack) -> None:
    write = stack.exec(
        "evidence_reader",
        ["sh", "-c", "echo x > /sealed/planted 2>&1 || echo WRITE_DENIED"],
        check=False,
    )

    assert "WRITE_DENIED" in write.stdout


def test_the_host_copies_the_sealed_volume_out_through_the_evidence_reader(
    stack: EpisodeStack,
    tmp_path: Path,
) -> None:
    copy = run_compose(stack, ["cp", "evidence_reader:/sealed", str(tmp_path / "sealed")], check=False)

    assert (copy.returncode, (tmp_path / "sealed").is_dir()) == (0, True)
