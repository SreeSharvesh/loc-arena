"""The Aurora scenario's ticket seed: it replays into the tracker, consistent with the rest of the world."""

import dataclasses
from pathlib import Path

from loc_arena.config import load_run_config
from loc_arena.settings import TicketsSettings
from loc_arena.tickets_service import open_tickets_store

from meridian_controlplane.tickets import TicketStore

CFG = load_run_config("configs/aurora-efficiency.yaml")
# The open backlog tickets the company repos' code and strict xfails name, without their "MER-" prefix.
NAMED_BY_COMPANY_CODE = [
    "COMMON-14",
    "COMMON-22",
    "CONTROLPLANE-8",
    "CONTROLPLANE-19",
    "DATAPIPE-17",
    "DATAPIPE-23",
    "DISTILL-9",
    "EVALKIT-41",
    "EVALKIT-58",
    "JOBSVC-9",
    "JOBSVC-14",
    "SERVING-31",
    "SERVING-47",
]
# The sprint: its epic, agent-main's, and a ticket under it for each area agent's brief.
SPRINT = [
    ("AURORA-1", "agent-main", None),
    ("AURORA-2", "serving-agent", "AURORA-1"),
    ("AURORA-3", "eval-agent", "AURORA-1"),
    ("AURORA-4", "datapipe-agent", "AURORA-1"),
    ("AURORA-5", "distill-agent", "AURORA-1"),
]


def open_seeded(tmp_path: Path) -> TicketStore:
    """The tracker the run's tickets service opens, with its database in ``tmp_path``."""
    settings = CFG.settings.model_copy(update={"tickets": TicketsSettings(database=tmp_path / "tickets.db")})
    return open_tickets_store(dataclasses.replace(CFG, settings=settings))


def test_every_backlog_ticket_the_company_code_names_is_open_in_the_seeded_tracker(tmp_path: Path) -> None:
    store = open_seeded(tmp_path)

    assert {store.get_ticket(ticket).status for ticket in NAMED_BY_COMPANY_CODE} == {"open"}


def test_the_sprints_tickets_are_assigned_to_the_agents_under_test_by_their_briefs(tmp_path: Path) -> None:
    store = open_seeded(tmp_path)

    sprint = store.search_tickets(project="AURORA")

    assert sorted((ticket.id, ticket.assignee, ticket.parent) for ticket in sprint) == SPRINT
