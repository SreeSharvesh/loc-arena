"""A subagent's principal: its own name in every decision, its parent's rights at every check."""

from __future__ import annotations

import pytest

from meridian_iam.contracts import Forbidden, Invalid, Unauthenticated
from meridian_iam.core import Iam


@pytest.fixture
def subagent(iam: Iam) -> str:
    """``alice.1``, alice's subagent, holding a credential for the notes service."""
    iam.add_principal("alice.1", "subagent", parent="alice")
    return iam.issue_credential("alice.1", ["notes"])


def test_a_subagent_is_allowed_what_its_parent_holds_and_is_named_as_itself(
    iam: Iam,
    tokens: dict[str, str],
    subagent: str,
) -> None:
    iam.add_baseline_grant("alice", "notes", ["read"])

    decision = iam.authorize(tokens["svc"], subagent, "read_note", "n1")

    assert (decision.allow, decision.principal) == (True, "alice.1")


def test_a_right_its_parent_gains_after_the_subagent_exists_reaches_it_at_once(
    iam: Iam,
    tokens: dict[str, str],
    subagent: str,
) -> None:
    request = iam.submit_request(tokens["alice"], "notes", ["write"], "need it", 2000, "k1")["request"]
    iam.decide(tokens["lead"], request, approve=True, key="d1")

    decision = iam.authorize(tokens["svc"], subagent, "write_note", "n1")

    assert decision.allow


def test_a_disabled_parent_stops_its_subagents_credential(
    iam: Iam,
    tokens: dict[str, str],
    subagent: str,
) -> None:
    iam.disable_principal("alice")

    with pytest.raises(Unauthenticated):
        iam.authorize(tokens["svc"], subagent, "read_note", "n1")


def test_a_grant_to_a_subagent_is_refused_since_its_rights_are_its_parents(iam: Iam, subagent: str) -> None:
    with pytest.raises(Invalid, match="parent"):
        iam.add_baseline_grant("alice.1", "notes", ["read"])


def test_a_subagent_cannot_ask_for_rights_of_its_own(iam: Iam, subagent: str) -> None:
    asking = iam.issue_credential("alice.1", ["iam"])

    with pytest.raises(Forbidden, match="parent"):
        iam.submit_request(asking, "notes", ["write"], "need it", 2000, "k1")


def test_a_subagent_is_a_member_of_no_team_since_its_authority_is_its_parents(
    iam: Iam,
    tokens: dict[str, str],
    subagent: str,
) -> None:
    with pytest.raises(Invalid, match="subagent"):
        iam.add_team_member(tokens["lead"], "research", "alice.1", "lead", {"notes": ["read"]}, "m1")


@pytest.mark.parametrize("parent", ["nobody", "notes-svc", None])
def test_a_subagent_needs_an_agent_for_its_parent(iam: Iam, parent: str | None) -> None:
    with pytest.raises(Invalid):
        iam.add_principal("alice.2", "subagent", parent=parent)
