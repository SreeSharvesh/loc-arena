"""Each agent's checkout: its own clone of every company repo on the forge, where it works and its code runs.

An agent's checkout is in its own volume, mounted at ``checkouts_directory/<agent id>`` in the episode and in
that agent's sandbox alone, and is cloned there from the forge over HTTP with the agent's own credential on
it; the sandbox is reset to keep only the new checkout first. Each clone commits as its agent and pushes back
to the forge. The workspace service's tools read and write the checkout, and its commands run in the agent's
sandbox. A checkout lasts the play: what an agent leaves in it and never pushes is graded nowhere.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sandbox_server.wire import CommandRequest, CommandResult

from loc_arena.compose_document import ComposeVolume
from loc_arena.config import RunConfig
from loc_arena.forge_service import SERVICE as FORGE
from loc_arena.sandbox import IDENTITY_PREFIX, SandboxClient
from loc_arena.settings import StackSettings
from loc_arena.tasks.main_task_grader import COMPANY_REPOS
from meridian_forge.repositories import EMAIL_DOMAIN

IMAGE_USER_ID = 65534  # nobody, whom the episode's and the sandboxes' images run as
# Clones each of $REPOS from $FORGE into the working directory, all at once, each clone committing as $AGENT
# and sending the credential in $TOKEN_FILE, if any, as a bearer token on every request to the forge.
CLONE_SCRIPT = """set -eu
options=(-c "user.name=$AGENT" -c "user.email=$AGENT@$DOMAIN")
if [ -n "$TOKEN_FILE" ]; then options+=(-c "http.extraHeader=Authorization: Bearer $(cat "$TOKEN_FILE")"); fi
clones=()
for repo in $REPOS; do git clone --quiet "${options[@]}" "$FORGE/$repo" "$repo" & clones+=($!); done
for clone in "${clones[@]}"; do wait "$clone"; done
"""


class CheckoutError(RuntimeError):
    """An agent's checkout could not be cloned, so the episode cannot play."""


class CommandRunner(Protocol):
    """Where an agent's commands run: its sandbox."""

    def run(self, request: CommandRequest) -> CommandResult:
        """Run ``request`` and return what it did."""
        ...


@dataclass(frozen=True)
class Checkout:
    """An agent's checkout: the directory holding its clones, and where its commands run."""

    directory: Path
    runner: CommandRunner


def checkout_home(stack: StackSettings, agent_id: str) -> Path:
    """Where ``agent_id``'s checkout volume is mounted in a stack run, in the episode and in its sandbox."""
    return stack.checkouts_directory / agent_id


def checkout_mount(stack: StackSettings, agent_id: str) -> str:
    """The compose mount of ``agent_id``'s checkout volume, the same in the episode and in its sandbox.

    It is labelled for containers (``z``): unlabelled, a host enforcing SELinux lets neither container write
    the volume's tmpfs. Elsewhere the label does nothing.
    """
    return f"checkout-{agent_id}:{checkout_home(stack, agent_id)}:z"


def render_checkout_volumes(stack: StackSettings, agent_ids: Iterable[str]) -> dict[str, ComposeVolume]:
    """Each agent's checkout volume: in memory, so it can be owned by the images' user, never by root."""
    options = f"size={stack.checkout_volume_size},uid={IMAGE_USER_ID},mode=0700"
    volume: ComposeVolume = {"driver_opts": {"type": "tmpfs", "device": "tmpfs", "o": options}}
    return {f"checkout-{agent_id}": volume for agent_id in agent_ids}


def open_checkouts(config: RunConfig, sandboxes: Mapping[str, SandboxClient]) -> dict[str, Checkout]:
    """Clone each agent's checkout from the forge, in its sandbox of ``sandboxes``.

    Raises ``CheckoutError`` if a clone fails.
    """
    stack = config.settings.stack
    port = next(service.port for service in config.live_services if service.name == FORGE)
    root, forge = stack.checkouts_directory, f"http://{FORGE}:{port}/git"
    token_file = str(config.settings.gateway.secrets_dir / f"{IDENTITY_PREFIX}{FORGE}")
    checkouts = {}
    for agent in config.agents:
        (root / agent.id).mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(dir=root / agent.id)).resolve()
        sandboxes[agent.id].reset(keep=directory)  # what an earlier episode left there goes before the clone
        environment = {
            "FORGE": forge,
            "REPOS": " ".join(COMPANY_REPOS),
            "AGENT": agent.id,
            "DOMAIN": EMAIL_DOMAIN,
            "TOKEN_FILE": token_file,
        }
        request = CommandRequest(
            argv=["bash", "-c", CLONE_SCRIPT],
            directory=directory,
            timeout_seconds=stack.shell_timeout_seconds,
            environment=environment,
        )
        cloned = sandboxes[agent.id].run(request)
        if cloned.returncode != 0:
            raise CheckoutError(f"{agent.id} could not clone the forge's repos: {cloned.stderr.strip()}")
        checkouts[agent.id] = Checkout(directory, sandboxes[agent.id])
    return checkouts


def remove_checkouts(checkouts: Mapping[str, Checkout]) -> None:
    """Remove each agent's checkout once its play is over; what agent code made undeletable stays."""
    for checkout in checkouts.values():
        shutil.rmtree(checkout.directory, ignore_errors=True)
