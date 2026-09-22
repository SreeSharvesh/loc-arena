from __future__ import annotations

from loc_arena.harness import EpisodeStack

from tests.integration._docker_support import path_exists, service_networks


def test_host_reads_sealed_log_through_evidence_reader(stack: EpisodeStack) -> None:
    # the host grader reads the sealed log through the networkless, read-only evidence-reader
    read = stack.exec("evidence_reader", ["cat", "/sealed/events.jsonl"], check=False)
    assert read.returncode == 0
    lines = [line for line in read.stdout.splitlines() if line.strip()]
    assert len(lines) >= 1  # at least the recorder boot marker on the sealed volume

    # the evidence-reader is networkless (only docker's "none" network) and its sealed mount is read-only
    assert service_networks(stack, "evidence_reader") <= {"none"}
    assert "sealed-net" not in service_networks(stack, "evidence_reader")
    write = stack.exec(
        "evidence_reader",
        ["sh", "-c", "echo x >> /sealed/events.jsonl 2>&1 || echo WRITE_DENIED"],
        check=False,
    )
    assert "WRITE_DENIED" in write.stdout

    # the agent (execution) has no route to the sealed log at all
    assert path_exists(stack, "execution", "/sealed/events.jsonl") is False
