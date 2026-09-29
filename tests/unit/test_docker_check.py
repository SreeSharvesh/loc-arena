"""``docker_available``, the first thing ``make run`` checks, against a stand-in docker CLI on PATH."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from loc_arena.compose_stack import docker_available
from loc_arena.stack.settings import DockerSettings

CHECK_TIMEOUT_SECONDS = 0.5
HUNG_SECONDS = 20  # far past the check's timeout: the stand-in daemon never answers in time
HUNG_DOCKER = f"#!/bin/sh\nexec sleep {HUNG_SECONDS}\n"
ANSWERING_DOCKER = "#!/bin/sh\nexit 0\n"


def _put_docker_on_path(script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = tmp_path / "docker"
    docker.write_text(script)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")


def test_a_daemon_that_never_answers_counts_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _put_docker_on_path(HUNG_DOCKER, tmp_path, monkeypatch)

    available = docker_available(DockerSettings(daemon_check_timeout_seconds=CHECK_TIMEOUT_SECONDS))

    assert available is False


def test_a_daemon_that_answers_counts_as_available(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _put_docker_on_path(ANSWERING_DOCKER, tmp_path, monkeypatch)

    available = docker_available(DockerSettings(daemon_check_timeout_seconds=CHECK_TIMEOUT_SECONDS))

    assert available is True
