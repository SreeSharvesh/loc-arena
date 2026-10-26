"""The tickets service: Meridian's ticket tracker, with projects, tickets and the discussion on each.

``store`` holds them and their history in SQLite, ``seed`` what a tracker starts with, and ``service`` the
HTTP API and the MCP tools over a store. ``authority`` is what a deployment hands in to decide every call.
"""

from __future__ import annotations

from meridian_controlplane.tickets.authority import Authority
from meridian_controlplane.tickets.seed import TicketSeed, load_ticket_seed, open_seeded_store
from meridian_controlplane.tickets.service import SERVICE, build_tickets_server, create_tickets_api
from meridian_controlplane.tickets.store import (
    Comment,
    Project,
    Ticket,
    TicketError,
    TicketStore,
    TicketSummary,
)

__all__ = [
    "SERVICE",
    "Authority",
    "Comment",
    "Project",
    "Ticket",
    "TicketError",
    "TicketSeed",
    "TicketStore",
    "TicketSummary",
    "build_tickets_server",
    "create_tickets_api",
    "load_ticket_seed",
    "open_seeded_store",
]
