"""Request, approval, direct administration and revocation semantics."""

from __future__ import annotations

import pytest
from iam_support import Clock

from meridian_iam.contracts import Conflict, Forbidden, Invalid, Unauthenticated
from meridian_iam.core import Iam
from meridian_iam.core.lifecycle import OPERATOR_ROLE, OPERATOR_TEAM


def submit(
    iam: Iam,
    token: str,
    perms: list[str] | None = None,
    until: int | None = 2000,
    key: str = "k",
) -> str:
    return iam.submit_request(token, "notes", perms or ["write"], "because", until, key)["request"]


def allowed(iam: Iam, t: dict[str, str], who: str, op: str = "write_note") -> bool:
    return iam.authorize(t["svc"], t[who], op, "n").allow


def test_pending_and_denied_grant_nothing(iam: Iam, tokens: dict[str, str]) -> None:
    pending = submit(iam, tokens["alice"])
    assert not allowed(iam, tokens, "alice")
    iam.decide(tokens["lead"], pending, approve=False, key="d", reason="no")
    assert not allowed(iam, tokens, "alice")
    assert iam.my_requests(tokens["alice"])[0]["state"] == "denied"


def test_a_decision_names_who_asked_so_it_joins_to_the_request(iam: Iam, tokens: dict[str, str]) -> None:
    request = submit(iam, tokens["alice"])

    decided = iam.decide(tokens["lead"], request, approve=True, key="d", reason="ok")

    assert decided == {"request": request, "state": "approved", "requester": "alice"}


def test_a_filed_request_says_whom_to_ask_and_what_happens_next(iam: Iam, tokens: dict[str, str]) -> None:
    filed = iam.submit_request(tokens["alice"], "notes", ["write"], "because", 2000, "k")

    request = filed["request"]
    assert filed["approvers"] == ["lead"]
    assert filed["next"] == (
        f"Request {request} waits for lead, who decide it from their own inbox: you need not ask. "
        f"GET /requests/{request} shows its state, and your inbox brings the outcome."
    )


def test_only_approvers_holding_what_is_asked_are_named_never_the_requester(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    iam.add_member("bob", "research", "lead", authority={"notes": ["read"]})

    for_write = iam.submit_request(tokens["alice"], "notes", ["write"], "because", 2000, "a")
    for_read = iam.submit_request(tokens["lead"], "notes", ["read"], "because", 2000, "l")

    assert (for_write["approvers"], for_read["approvers"]) == (["lead"], ["bob"])


def test_a_request_nobody_can_approve_says_so(iam: Iam, tokens: dict[str, str]) -> None:
    filed = iam.submit_request(tokens["lead"], "notes", ["read"], "because", 2000, "k")

    assert filed["approvers"] == []
    assert "nobody can approve it now" in filed["next"]


@pytest.mark.parametrize(
    ("perms", "until"),
    [
        (["manage"], 2000),
        (["nope"], 2000),
        ([], 2000),
        (["write"], 1000),
        (["write"], 99999),
        (["write"], None),
    ],
)
def test_invalid_requests_rejected_without_mutation(
    iam: Iam,
    tokens: dict[str, str],
    perms: list[str],
    until: int | None,
) -> None:
    with pytest.raises(Invalid):
        iam.submit_request(tokens["alice"], "notes", perms, "why", until, "k")
    assert iam.my_requests(tokens["alice"]) == []


def test_unknown_resource_and_blank_reason(iam: Iam, tokens: dict[str, str]) -> None:
    with pytest.raises(Invalid):
        iam.submit_request(tokens["alice"], "ghost", ["read"], "why", 2000, "k")
    with pytest.raises(Invalid):
        iam.submit_request(tokens["alice"], "notes", ["read"], "  ", 2000, "k")


def test_only_eligible_owner_with_authority_decides(iam: Iam, tokens: dict[str, str], clock: Clock) -> None:
    req = submit(iam, tokens["alice"])
    with pytest.raises(Forbidden):
        iam.decide(tokens["bob"], req, approve=True, key="d")  # not an owner
    iam.add_principal("peer", "agent")
    iam.add_member("peer", "research", "member", {"notes": ["write"]})  # membership alone confers nothing
    peer = iam.issue_credential("peer", ["iam"])
    with pytest.raises(Forbidden):
        iam.decide(peer, req, approve=True, key="d")
    iam.add_member("lead", "research", "lead", {"notes": ["read"]})  # standing authority lacks write
    with pytest.raises(Forbidden):
        iam.decide(tokens["lead"], req, approve=True, key="d2")
    iam.add_member("lead", "research", "lead", {"notes": ["read", "write"]})
    iam.remove_member("lead", "research")  # recently removed owner
    with pytest.raises(Forbidden):
        iam.decide(tokens["lead"], req, approve=True, key="d3")
    assert not allowed(iam, tokens, "alice")


def test_a_decision_reason_is_stripped_and_bounded_like_a_requests(iam: Iam, tokens: dict[str, str]) -> None:
    request = submit(iam, tokens["alice"])
    with pytest.raises(Invalid):
        iam.decide(tokens["lead"], request, approve=False, key="long", reason="x" * 501)

    iam.decide(tokens["lead"], request, approve=False, key="ok", reason=" " * 450 + "x" * 100)

    assert iam.my_requests(tokens["alice"])[0]["decision_reason"] == "x" * 100


@pytest.mark.parametrize(("permissions", "until"), [(["write"], None), (None, 1500)])
def test_a_denial_grants_nothing_so_it_names_no_permission_or_deadline(
    iam: Iam,
    tokens: dict[str, str],
    permissions: list[str] | None,
    until: int | None,
) -> None:
    request = submit(iam, tokens["alice"])

    with pytest.raises(Invalid):
        iam.decide(
            tokens["lead"],
            request,
            approve=False,
            key="d",
            reason="no",
            permissions=permissions,
            until=until,
        )


def test_no_self_approval_and_no_repeat_decision(iam: Iam, tokens: dict[str, str]) -> None:
    req = submit(iam, tokens["lead"])
    with pytest.raises(Forbidden):
        iam.decide(tokens["lead"], req, approve=True, key="d")
    other = submit(iam, tokens["alice"], key="k2")
    iam.decide(tokens["lead"], other, approve=True, key="d2")
    with pytest.raises(Conflict):
        iam.decide(tokens["lead"], other, approve=False, key="d3")


def test_approval_may_narrow_but_never_widen(iam: Iam, tokens: dict[str, str]) -> None:
    req = submit(iam, tokens["alice"], ["read", "write"])
    with pytest.raises(Invalid):
        iam.decide(tokens["lead"], req, approve=True, permissions=["manage"], key="a")
    with pytest.raises(Invalid):
        iam.decide(tokens["lead"], req, approve=True, until=2500, key="b")
    iam.decide(tokens["lead"], req, approve=True, permissions=["read"], until=1500, key="d")
    assert iam.effective_access(tokens["alice"]) == {"notes": ["read"]}
    row = iam.my_requests(tokens["alice"])[0]
    assert (row["approved"], row["approved_until"], row["permissions"]) == (["read"], 1500, ["read", "write"])


def test_queue_privacy(iam: Iam, tokens: dict[str, str]) -> None:
    submit(iam, tokens["alice"])
    assert len(iam.owner_queue(tokens["lead"], "notes")) == 1
    assert iam.my_requests(tokens["bob"]) == []
    for resource in ("notes", "ghost"):
        with pytest.raises(Forbidden):
            iam.owner_queue(tokens["bob"], resource)  # same answer whether or not it exists


def test_pending_requests_are_bounded(iam: Iam, tokens: dict[str, str]) -> None:
    for n in range(20):
        submit(iam, tokens["alice"], key=f"k{n}")
    with pytest.raises(Invalid):
        submit(iam, tokens["alice"], key="k20")
    assert len(iam.my_requests(tokens["alice"])) == 20  # nothing deleted to make room


def test_idempotency(iam: Iam, tokens: dict[str, str]) -> None:
    first = iam.submit_request(tokens["alice"], "notes", ["write"], "r", 2000, "same")
    assert iam.submit_request(tokens["alice"], "notes", ["write"], "r", 2000, "same") == first
    assert len(iam.my_requests(tokens["alice"])) == 1
    with pytest.raises(Conflict):
        iam.submit_request(tokens["alice"], "notes", ["read"], "r", 2000, "same")
    again = iam.decide(tokens["lead"], first["request"], approve=True, key="dk")
    assert iam.decide(tokens["lead"], first["request"], approve=True, key="dk") == again


def test_revoke_hits_every_overlapping_grant_and_spares_the_rest(iam: Iam, tokens: dict[str, str]) -> None:
    approved = submit(iam, tokens["alice"], ["read", "write"])
    iam.decide(tokens["lead"], approved, approve=True, key="d")
    iam.grant(tokens["lead"], "alice", "notes", ["write"], 2500, "g")  # overlapping direct grant
    assert iam.revoke(tokens["lead"], "alice", "notes", ["write"], "r") == {"revoked": 2}
    assert iam.effective_access(tokens["alice"]) == {"notes": ["read"]}
    assert not allowed(iam, tokens, "alice")
    assert allowed(iam, tokens, "alice", "read_note")


def test_direct_grant_needs_standing_authority_and_active_target(iam: Iam, tokens: dict[str, str]) -> None:
    with pytest.raises(Forbidden):
        iam.grant(tokens["alice"], "bob", "notes", ["read"], 2000, "g")
    with pytest.raises(Forbidden):
        iam.grant(tokens["lead"], "bob", "notes", ["manage"], 2000, "g2")  # lead lacks 'manage' authority
    iam.disable_principal("bob")
    with pytest.raises(Invalid):
        iam.grant(tokens["lead"], "bob", "notes", ["read"], 2000, "g3")
    assert iam.effective_access(tokens["alice"]) == {}


def test_rejected_mutation_changes_nothing_but_is_audited(iam: Iam, tokens: dict[str, str]) -> None:
    before = len(iam.my_requests(tokens["alice"]))
    with pytest.raises(Invalid):
        iam.submit_request(tokens["alice"], "notes", ["manage"], "r", 2000, "k")
    assert len(iam.my_requests(tokens["alice"])) == before
    rejected = [e for e in iam.audit_log() if e["kind"] == "rejected"]
    assert [(e["actor"], e["operation"], e["code"], e["resource"], e["permissions"]) for e in rejected] == [
        ("alice", "access_requested", "invalid", "notes", ["manage"]),
    ]


def test_a_refused_calls_long_field_is_cut_and_the_rest_kept(iam: Iam, tokens: dict[str, str]) -> None:
    with pytest.raises(Invalid):
        iam.submit_request(tokens["alice"], "notes", ["write"], "x" * 5_000, 2000, "k")

    (refused,) = [e for e in iam.audit_log() if e["kind"] == "rejected"]

    assert (refused["resource"], refused["permissions"]) == ("notes", ["write"])
    assert refused["reason"] == "x" * 500 + "… (5000 characters)"


def test_a_caller_that_never_authenticated_audits_nothing_it_sent(iam: Iam) -> None:
    with pytest.raises(Unauthenticated):
        iam.submit_request("not a credential", "notes", ["write"], "anything at all", 2000, "k")

    (refused,) = [e for e in iam.audit_log() if e["kind"] == "rejected"]

    assert refused == {
        "at": refused["at"],
        "kind": "rejected",
        "actor": None,
        "operation": "access_requested",
        "code": "unauthenticated",
        "via": None,
    }


def test_baseline_grants_are_ongoing_revocable_and_listed(iam: Iam, tokens: dict[str, str]) -> None:
    iam.add_baseline_grant("alice", "notes", ["read"])
    assert allowed(iam, tokens, "alice", "read_note")
    assert iam.revoke(tokens["lead"], "alice", "notes", ["read"], "r") == {"revoked": 1}
    assert not allowed(iam, tokens, "alice", "read_note")
    assert not iam.bootstrapped()  # principals and grants added one by one are not a completed seed


def test_standing_authority_is_per_resource(iam: Iam, tokens: dict[str, str]) -> None:
    import json  # noqa: PLC0415

    from iam_support import NOTES  # noqa: PLC0415

    from meridian_iam.contracts import load_manifest  # noqa: PLC0415

    other = json.loads(json.dumps(NOTES))
    other["resource"] = "wiki"
    iam.register_resource(load_manifest(other), owner_team="research", approver_role="lead", max_seconds=3600)
    ask = iam.submit_request(tokens["alice"], "wiki", ["write"], "r", 1500, "k")["request"]
    with pytest.raises(
        Forbidden,
    ):  # lead's authority is over notes, not wiki, though both belong to team research
        iam.decide(tokens["lead"], ask, approve=True, key="d")
    with pytest.raises(Forbidden):
        iam.grant(tokens["lead"], "alice", "wiki", ["write"], 1500, "g")


def two_resources(iam: Iam) -> None:
    """Register ``wiki`` beside ``notes`` under the same owner team, and give the lead authority over both."""
    import json  # noqa: PLC0415

    from iam_support import NOTES  # noqa: PLC0415

    from meridian_iam.contracts import load_manifest  # noqa: PLC0415

    other = json.loads(json.dumps(NOTES))
    other["resource"] = "wiki"
    iam.register_resource(load_manifest(other), owner_team="research", approver_role="lead", max_seconds=3600)
    iam.add_member("lead", "research", "lead", {"notes": ["read", "write"], "wiki": ["read", "write"]})


def test_a_request_outside_the_allowed_lifetime_is_told_the_window_to_ask_within(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    two_resources(iam)  # wiki allows no ongoing access and an hour at most

    for until in (None, 10**12):
        with pytest.raises(Invalid, match="send until as a unix time within 3600 seconds from now"):
            iam.submit_request(tokens["alice"], "wiki", ["write"], "r", until, f"k-{until}")


def test_a_resource_bound_credential_manages_its_own_resource_and_no_other(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    from meridian_iam.contracts import Unauthenticated  # noqa: PLC0415

    two_resources(iam)
    notes_only = iam.issue_credential(
        "lead",
        ["notes", "iam:notes"],
    )  # what the notes service holds of its callers
    wiki_ask = iam.submit_request(tokens["alice"], "wiki", ["write"], "r", 1500, "w")["request"]
    notes_ask = iam.submit_request(tokens["alice"], "notes", ["write"], "r", 1500, "n")["request"]
    # A compromised notes service replays the lead's notes credential against IAM: it reaches notes only.
    iam.decide(notes_only, notes_ask, approve=True, key="d1")
    with pytest.raises(Unauthenticated):
        iam.decide(notes_only, wiki_ask, approve=True, key="d2")
    with pytest.raises(Unauthenticated):
        iam.grant(notes_only, "bob", "wiki", ["read"], 1500, "g")
    with pytest.raises(Unauthenticated):
        iam.revoke(notes_only, "alice", "wiki", ["write"], "r")
    with pytest.raises(Unauthenticated):
        iam.owner_queue(notes_only, "wiki")
    with pytest.raises(Unauthenticated):
        iam.grants_for(notes_only, "wiki")
    with pytest.raises(Unauthenticated):
        iam.submit_request(notes_only, "wiki", ["read"], "r", 1500, "s")
    with pytest.raises(Unauthenticated):  # and none of the identity administration
        iam.add_team_member(notes_only, "research", "bob", "lead", {"notes": ["read"]}, "a")
    with pytest.raises(Unauthenticated):
        iam.remove_team_member(notes_only, "research", "alice", "r")
    iam.add_member("lead", OPERATOR_TEAM, OPERATOR_ROLE)
    with pytest.raises(Unauthenticated):
        iam.disable_principal_as(notes_only, "bob", "x")
    assert iam.owner_queue(notes_only, "notes") and iam.grant(
        notes_only,
        "bob",
        "notes",
        ["read"],
        1500,
        "g2",
    )
    iam.decide(tokens["lead"], wiki_ask, approve=True, key="d3")  # the lead's own full credential still can


def test_a_resource_bound_credential_sees_only_its_resource(iam: Iam, tokens: dict[str, str]) -> None:
    two_resources(iam)
    iam.add_baseline_grant("alice", "notes", ["read"])
    iam.add_baseline_grant("alice", "wiki", ["read"])
    iam.submit_request(tokens["alice"], "wiki", ["write"], "r", 1500, "w")
    scoped = iam.issue_credential("alice", ["notes", "iam:notes"])
    assert iam.effective_access(tokens["alice"]) == {"notes": ["read"], "wiki": ["read"]}
    assert iam.effective_access(scoped) == {"notes": ["read"]}
    assert iam.my_requests(scoped) == []  # the wiki request is not visible through the notes credential
    assert [r["resource"] for r in iam.my_requests(tokens["alice"])] == ["wiki"]


def test_audit_says_via_which_resource_a_forwarded_call_came_and_nothing_for_a_direct_one(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    scoped = iam.issue_credential("alice", ["notes", "iam:notes"])
    iam.submit_request(scoped, "notes", ["read"], "forwarded", 1500, "k1")
    iam.submit_request(tokens["alice"], "notes", ["write"], "direct", 1500, "k2")
    events = [e for e in iam.audit_log() if e["kind"] == "access_requested"]
    assert [(e["reason"], e["via"]) for e in events] == [("forwarded", "notes"), ("direct", None)]


def test_the_queue_shows_why_access_was_asked_and_why_it_was_decided(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    ask = iam.submit_request(
        tokens["alice"],
        "notes",
        ["write"],
        "need to append the weekly report",
        1500,
        "k",
    )
    queued = iam.owner_queue(tokens["lead"], "notes")[0]
    assert queued["reason"] == "need to append the weekly report" and queued["decision_reason"] is None
    iam.decide(tokens["lead"], ask["request"], approve=False, key="d", reason="use the board instead")
    decided = iam.my_requests(tokens["alice"])[0]
    assert (decided["reason"], decided["decision_reason"]) == (
        "need to append the weekly report",
        "use the board instead",
    )


def test_an_approver_cannot_grant_itself_what_it_could_not_approve_for_itself(
    iam: Iam,
    tokens: dict[str, str],
) -> None:
    mine = iam.submit_request(tokens["lead"], "notes", ["write"], "for me", 1500, "k")["request"]
    with pytest.raises(Forbidden):
        iam.decide(tokens["lead"], mine, approve=True, key="d")  # self-approval is refused...
    with pytest.raises(Forbidden):
        iam.grant(
            tokens["lead"],
            "lead",
            "notes",
            ["write"],
            1500,
            "g",
        )  # ...and so is the same thing done directly
    assert iam.effective_access(tokens["lead"]) == {}
    iam.grant(tokens["lead"], "alice", "notes", ["write"], 1500, "g2")  # granting someone else is the job


def test_one_request_is_fetched_by_id_for_its_requester_or_an_approver_whatever_the_queue_limit(
    iam: Iam,
    tokens: dict[str, str],
    clock: Clock,
) -> None:
    first = iam.submit_request(tokens["alice"], "notes", ["read"], "oldest", 1500, "a")["request"]
    clock.now += 1
    iam.submit_request(tokens["alice"], "notes", ["write"], "newer", 1500, "b")
    assert [r["reason"] for r in iam.owner_queue(tokens["lead"], "notes", limit=1)] == [
        "newer",
    ]  # the oldest is cut off
    for token in (tokens["alice"], tokens["lead"]):  # but is found by id: by its requester and by an approver
        assert iam.get_request(token, first)["reason"] == "oldest"
    with pytest.raises(Forbidden):
        iam.get_request(tokens["bob"], first)  # anyone else is refused, as for a request that does not exist
    with pytest.raises(Forbidden):
        iam.get_request(tokens["lead"], "no-such-request")
