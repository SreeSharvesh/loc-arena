"""A stand-in for ``subprocess.run`` answering a stack run's compose calls, for the episode-stack tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

COMPOSE_PREFIX_LENGTH = 8  # docker compose -p <project> -f <file> --project-directory <repository>
NOTES_LOG = b'{"caller": "agent-main", "status": 200}\n'


@dataclass
class FakeDocker:
    """``subprocess.run`` for the compose calls of a stack run whose last play exits 1: nothing is graded."""

    failing: str = "no subcommand"  # the compose subcommand or option whose call fails
    episode_created_only: bool = False  # a live service never became healthy, so the episode never started
    plays_passing: int = 0  # how many plays of the episode exit 0 before one exits 1
    unwritable_after_first_play: Path | None = None  # logs whose run's notes.log is a directory once played
    calls: list[list[str]] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    published: str = ""  # where the gateway's port is published: a socket the test listens on

    def __call__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,  # none on the host's own call, for the gateway's published port
        stdout: IO[bytes] | None = None,
        text: bool = False,
        **_: object,
    ) -> subprocess.CompletedProcess[Any]:
        self.calls.append(command)
        self.environment = env or self.environment
        verb = command[COMPOSE_PREFIX_LENGTH]
        if verb == "logs" and stdout is not None:
            stdout.write(NOTES_LOG)
        playing = verb == "up" and "--attach" in command
        if playing and self.unwritable_after_first_play and self.plays == 1:
            (services,) = self.unwritable_after_first_play.glob("*/*/services")
            (services / "notes.log").mkdir()
        failed = self.failing in command[COMPOSE_PREFIX_LENGTH:] or (
            playing and self.plays > self.plays_passing
        )
        listed = verb == "ps" and (self.episode_created_only or "running" in command)  # Phoenix runs
        created = "container-id\n" if listed else ""
        published = self.published if verb == "port" else created
        printed = NOTES_LOG if verb == "logs" and stdout is None else published
        printed = printed.decode() if text and isinstance(printed, bytes) else printed
        printed = printed.encode() if not text and isinstance(printed, str) else printed  # as subprocess's
        return subprocess.CompletedProcess(command, int(failed), stdout=printed, stderr=printed[:0])

    @property
    def plays(self) -> int:
        return sum(call[COMPOSE_PREFIX_LENGTH] == "up" and "--attach" in call for call in self.calls)

    @property
    def verbs(self) -> list[str]:
        return [call[COMPOSE_PREFIX_LENGTH] for call in self.calls]
