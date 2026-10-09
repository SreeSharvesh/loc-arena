"""What the episode and the sandbox's command server say to each other: two paths and their bodies."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat

RUN_PATH = "/run"
RESET_PATH = "/reset"


class CommandRequest(BaseModel):
    """A command an agent's tool runs over the checkout: what, where, for how long."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    argv: list[str] = Field(min_length=1, description="The program and its arguments.")
    directory: Path = Field(description="The working directory.")
    timeout_seconds: PositiveFloat = Field(description="Seconds before its whole session is killed.")
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="Variables set on top of the runner's environment, which never holds the provider key.",
    )


class CommandResult(BaseModel):
    """What a command did: its exit code (none when it was killed at the timeout) and its output's tails."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    returncode: int | None = Field(description="The exit code; none when the timeout killed the command.")
    stdout: str = Field(description="The end of its stdout.")
    stderr: str = Field(description="The end of its stderr.")


class ResetRequest(BaseModel):
    """Make the sandbox ready for a new episode, whose checkout is ``keep``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    keep: Path = Field(description="The new episode's checkout, under the checkouts directory.")
