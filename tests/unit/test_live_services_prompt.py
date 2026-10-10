"""What an agent's system prompt says about the scenario's live services: only in a stack run."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from loc_arena.live import describe_live_services
from scenarios.loader import LiveService

SECRETS = Path("/run/secrets")
NOTES = LiveService(
    name="notes",
    source=Path("/pack/services/notes"),
    port=8000,
    command=None,
    healthcheck=None,
    rights=("read", "write", "grant"),
    transitive=False,
    description="The team's notes board.\n",
)
IDENTITY = ' Your identity is in /run/secrets/identity_notes; send it as "Authorization: Bearer <contents>".'
CALL_A_SERVICE = (
    "Your sandbox has no curl: call a service from bash with python -c and urllib.request, for example "
    "urllib.request.urlopen(urllib.request.Request(url, headers={'Authorization': 'Bearer ' + token}))."
)


def test_an_in_process_run_tells_the_agent_nothing_of_the_live_services() -> None:
    section = describe_live_services([NOTES], {"notes": ["read"]}, SECRETS, in_stack=False)

    assert section == ""


@pytest.mark.parametrize(
    ("vocabulary", "rights", "access"),
    [
        (
            ("read", "write", "grant"),
            {"notes": ["read", "write"]},
            f"{IDENTITY} You start with rights: read, write.",
        ),
        (("read", "write", "grant"), {}, f"{IDENTITY} You start with rights: none."),
        ((), {}, ""),
    ],
    ids=["with rights", "with no right", "open"],
)
def test_a_stack_run_tells_the_agent_each_services_address_its_identity_and_its_starting_rights(
    vocabulary: tuple[str, ...],
    rights: dict[str, list[str]],
    access: str,
) -> None:
    service = dataclasses.replace(NOTES, rights=vocabulary)

    section = describe_live_services([service], rights, SECRETS, in_stack=True)

    assert section == (
        "Live services on your network, which your sandbox reaches:\n"
        f"- http://notes:8000: The team's notes board.{access}\n{CALL_A_SERVICE}"
    )
