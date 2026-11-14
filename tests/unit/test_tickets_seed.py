"""The Aurora scenario's ticket seed: it replays into the tracker, consistent with the rest of the world."""

import dataclasses
from pathlib import Path

from loc_arena.config import RunConfig, load_run_config
from loc_arena.live import build_briefs
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


def open_seeded(tmp_path: Path, run: RunConfig = CFG) -> TicketStore:
    """The tracker ``run``'s tickets service opens, with its database in ``tmp_path``."""
    tickets = run.settings.tickets.model_copy(update={"database": tmp_path / "tickets.db"})
    settings = run.settings.model_copy(update={"tickets": tickets})
    return open_tickets_store(dataclasses.replace(run, settings=settings))


def test_every_backlog_ticket_the_company_code_names_is_open_in_the_seeded_tracker(tmp_path: Path) -> None:
    store = open_seeded(tmp_path)

    assert {store.get_ticket(ticket).status for ticket in NAMED_BY_COMPANY_CODE} == {"open"}


def test_the_sprints_tickets_are_assigned_to_the_agents_under_test_by_their_briefs(tmp_path: Path) -> None:
    store = open_seeded(tmp_path)

    sprint = store.search_tickets(project="AURORA")

    assert sorted((ticket.id, ticket.assignee, ticket.parent) for ticket in sprint) == SPRINT


def test_a_sprint_ticket_whose_assignee_the_team_lacks_starts_unassigned(tmp_path: Path) -> None:
    teams = load_run_config(
        "configs/aurora-teams.yaml",
    )  # agent-main over serving-1..3, distill-1..3, eval-1..3

    sprint = open_seeded(tmp_path, teams).search_tickets(project="AURORA")

    assert sorted((ticket.id, ticket.assignee) for ticket in sprint) == [
        ("AURORA-1", "agent-main"),
        ("AURORA-2", None),
        ("AURORA-3", None),
        ("AURORA-4", None),
        ("AURORA-5", None),
    ]


def test_a_runs_seed_overlay_starts_the_sprint_on_its_own_epic_and_keeps_the_backlog(tmp_path: Path) -> None:
    selection = load_run_config("configs/aurora-model-evaluation.yaml")

    store = open_seeded(tmp_path, selection)

    sprint = store.search_tickets(project="AURORA")
    assert [(ticket.id, ticket.assignee) for ticket in sprint] == [("AURORA-1", "agent-main")]
    assert "model selection" in sprint[0].title
    assert {store.get_ticket(ticket).status for ticket in NAMED_BY_COMPANY_CODE} == {"open"}


def test_the_deploy_check_seeds_the_ticket_its_brief_names_and_keeps_the_sprint(tmp_path: Path) -> None:
    check = load_run_config("configs/aurora-efficiency.deploy-check.yaml")

    store = open_seeded(tmp_path, check)

    assigned = [(ticket.id, ticket.status) for ticket in store.search_tickets(assignee="controlplane-agent")]
    assert assigned == [("RELEASE-1", "open")]
    assert "RELEASE-1" in build_briefs(check)["controlplane-agent"]
    sprint = store.search_tickets(project="AURORA")
    assert sorted((ticket.id, ticket.assignee, ticket.parent) for ticket in sprint) == SPRINT
