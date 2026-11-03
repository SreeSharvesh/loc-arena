"""The forge as a stack run serves it: real git over HTTP and its tools over MCP, with a real IAM deciding."""

from __future__ import annotations

import dataclasses
import gzip
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from loc_arena.config import load_run_config
from loc_arena.forge_service import SERVICE, build_forge_app, load_seed, open_forge
from loc_arena.iam_seed import REPOSITORY
from loc_arena.merged_main import read_local_forge, write_merged_checkout
from loc_arena.recorded_events import ServiceRequestRecord, build_events, build_events_from_service_request
from loc_arena.settings import ForgeSettings
from loc_arena.tasks.main_task_grader import COMPANY_REPOS
from loc_arena.tool_records import ToolRecord
from loc_arena.tools_gateway import MCP_PATH
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from pydantic import TypeAdapter

from meridian_forge.git_http import FetchedRef, GitBounds, read_upload_request
from meridian_forge.repositories import (
    branch_diff,
    branch_exists,
    repository_path,
)
from meridian_iam.client import IamClient
from meridian_iam.contracts import load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app
from tests.unit._jobsvc_support import serve

CFG = load_run_config("configs/aurora-efficiency.iam.yaml")
MANIFEST = json.loads(
    (REPOSITORY / "scenarios/aurora_efficiency/services/iam-manifests/forge.json").read_text(),
)
# DEVELOPER may read, push, open pull requests and merge, TEAMMATE read and merge, READER only read, and
# OUTSIDER holds nothing on the forge.
DEVELOPER, TEAMMATE, READER, OUTSIDER = "serving-agent", "jobsvc-agent", "datapipe-agent", "eval-agent"
REPO = "meridian-serving"
REVIEWED_REPO = "meridian-controlplane"  # a repo whose merges need the reviewer's approval
# A client's git, with neither the user's nor the system's configuration, and no prompt for a password.
CLIENT_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
COMMITTER = ["-c", "user.name=Serving Agent", "-c", "user.email=serving-agent@meridian.example"]
NO_COMMIT = "0" * 40
_FETCHED_REFS = TypeAdapter(list[FetchedRef])  # git's ID for no commit: where a new ref moves from


@dataclass
class World:
    """Central IAM on a local port, credentials of its principals, and the forge a stack run serves."""

    tokens: dict[str, str]
    url: str
    repositories: Path
    lines: list[str]

    def git(self, agent: str, *arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Run git as ``agent``'s client, its credential on the forge sent as a bearer token."""
        header = f"http.extraHeader=Authorization: Bearer {self.tokens[agent]}"
        return subprocess.run(
            ["git", "-c", header, *arguments],
            capture_output=True,
            text=True,
            check=False,
            cwd=cwd,
            env={"PATH": os.environ["PATH"], **CLIENT_GIT},
        )

    def clone(self, agent: str, destination: Path, repo: str = REPO) -> subprocess.CompletedProcess[str]:
        return self.git(agent, "clone", f"{self.url}/git/{repo}", str(destination))

    def call_tool(self, agent: str, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call the forge's MCP ``tool`` as ``agent``, its credential on the forge as a bearer token."""

        async def call() -> dict[str, Any]:
            # By the forge's service name, as on agent-net: the server admits no other host.
            headers = {"Authorization": f"Bearer {self.tokens[agent]}", "Host": SERVICE}
            async with (
                httpx2.AsyncClient(headers=headers) as http,
                Client(streamable_http_client(f"{self.url}{MCP_PATH}", http_client=http)) as client,
            ):
                return (await client.call_tool(tool, arguments)).structured_content or {}

        return anyio.run(call)

    def records(self) -> list[ServiceRequestRecord]:
        return [
            ServiceRequestRecord.model_validate_json(line) for line in self.lines if '"operation"' in line
        ]


StartForge = Callable[..., World]


@pytest.fixture
def start_forge(tmp_path: Path) -> Iterator[StartForge]:
    """Start the forge under a real IAM: its forge settings the run config's but for those given."""
    iam = Iam(tmp_path / "iam.db", lambda: 1_000_000)
    for principal in (DEVELOPER, TEAMMATE, READER, OUTSIDER, "lead"):
        iam.add_principal(principal, "agent")
    iam.add_principal("forge-svc", "service")
    iam.add_member("lead", "platform", "owner", {SERVICE: ["read", "push", "open_pr", "merge"]})
    iam.register_resource(
        load_manifest(MANIFEST),
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
        allow_ongoing=True,
    )
    iam.bind_service("forge-svc", SERVICE, SERVICE)
    agents = (DEVELOPER, TEAMMATE, READER, OUTSIDER)
    tokens = {agent: iam.issue_credential(agent, [SERVICE, f"iam:{SERVICE}"]) for agent in agents}
    admin = iam.issue_credential("lead", ["iam"])
    iam.grant(admin, DEVELOPER, SERVICE, ["read", "push", "open_pr", "merge"], None, "developer")
    iam.grant(admin, TEAMMATE, SERVICE, ["read", "merge"], None, "teammate")
    iam.grant(admin, READER, SERVICE, ["read"], None, "reader")
    iam_server, iam_url = serve(create_app(iam))
    guard = Guard(
        load_manifest(MANIFEST),
        IamClient(iam_url, iam.issue_credential("forge-svc", ["iam"]), timeout=1),
    )
    servers = [iam_server]

    def start(**forge: Any) -> World:  # noqa: ANN401 - ForgeSettings fields
        settings = ForgeSettings(repositories=tmp_path / "repositories", **forge)
        lines: list[str] = []
        config = dataclasses.replace(CFG, settings=CFG.settings.model_copy(update={"forge": settings}))
        forge_server, url = serve(build_forge_app(config, guard, lines.append))
        servers.append(forge_server)
        return World({**tokens, "stranger": "not-a-credential"}, url, settings.repositories, lines)

    yield start
    for server in servers:
        server.should_exit = True


@pytest.fixture
def world(start_forge: StartForge) -> World:
    return start_forge()


def main_commit(repositories: Path, repo: str = REPO) -> str:
    bare = repository_path(repositories, repo)
    return subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_a_clone_holds_the_company_code_on_main(world: World, tmp_path: Path) -> None:
    cloned = world.clone(READER, tmp_path / "clone")

    assert cloned.returncode == 0, cloned.stderr
    assert world.git(READER, "branch", "--show-current", cwd=tmp_path / "clone").stdout.strip() == "main"
    company = REPOSITORY / "company" / REPO
    assert (tmp_path / "clone/pyproject.toml").read_text() == (company / "pyproject.toml").read_text()


@pytest.mark.parametrize(
    ("caller", "status"),
    [(OUTSIDER, HTTPStatus.FORBIDDEN), ("stranger", HTTPStatus.UNAUTHORIZED)],
    ids=["without read", "unknown credential"],
)
def test_a_clone_is_refused_without_read_and_recorded_as_a_blocked_action(
    world: World,
    tmp_path: Path,
    caller: str,
    status: HTTPStatus,
) -> None:
    cloned = world.clone(caller, tmp_path / "clone")

    assert cloned.returncode != 0
    [record] = world.records()
    assert (record.operation, record.status, record.arguments) == ("info_refs", status, {"repo": REPO})
    events = build_events_from_service_request(record)
    assert [event.payload["blocked"] for event in events] == ([True] if caller == OUTSIDER else [])


def test_a_clone_is_recorded_under_its_caller_and_its_iam_decisions_and_makes_no_event(
    world: World,
    tmp_path: Path,
) -> None:
    world.clone(READER, tmp_path / "clone")

    records = world.records()

    assert {record.operation for record in records} == {"info_refs", "upload_pack"}
    assert all(record.caller == READER and record.decision for record in records)
    assert [event for record in records for event in build_events_from_service_request(record)] == []


def commit_notes(world: World, agent: str, clone: Path, branch: str, repo: str = REPO) -> None:
    """Clone ``repo`` as ``agent`` into ``clone`` and commit notes there on ``branch``, main included."""
    world.clone(agent, clone, repo)
    world.git(agent, "checkout", "--quiet", "-B", branch, cwd=clone)
    (clone / "NOTES.md").write_text("notes\n")
    world.git(agent, "add", "NOTES.md", cwd=clone)
    world.git(agent, *COMMITTER, "commit", "--quiet", "-m", "Add notes", cwd=clone)


def test_a_change_one_agent_pushes_reaches_a_teammate_through_review_and_merge_and_is_graded(
    start_forge: StartForge,
    tmp_path: Path,
) -> None:
    world = start_forge(ci_min_seconds=0, ci_max_seconds=0)
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    world.clone(TEAMMATE, theirs, REVIEWED_REPO)
    commit_notes(world, DEVELOPER, mine, "docs/notes", REVIEWED_REPO)

    world.git(DEVELOPER, "push", "--quiet", "origin", "docs/notes", cwd=mine)
    opened = world.call_tool(
        DEVELOPER,
        "open_pr",
        {"repo": REVIEWED_REPO, "branch": "docs/notes", "title": "Notes"},
    )
    merging = {"repo": REVIEWED_REPO, "number": opened["number"]}
    by_author, by_teammate = (world.call_tool(agent, "merge", merging) for agent in (DEVELOPER, TEAMMATE))
    world.git(TEAMMATE, "pull", "--quiet", cwd=theirs)
    # The merge and the branch it merged, named: their commits may share a second, which git log orders.
    merged = ("show", "-s", "--format=%an|%s", "HEAD", "HEAD^2")
    log = world.git(TEAMMATE, *merged, cwd=theirs).stdout.splitlines()
    write_merged_checkout(tmp_path / "graded", read_local_forge(world.repositories).mains)

    assert opened["number"] == 3  # after the seed's pull request on the repo and the coworkers' benign one
    assert (by_author["merged"], by_teammate["merged"], by_teammate["approved"]) == (False, True, True)
    assert log == [f"{TEAMMATE}|Merge pull request #3 from docs/notes", "Serving Agent|Add notes"]
    assert (theirs / "NOTES.md").read_text() == (tmp_path / "graded" / REVIEWED_REPO / "NOTES.md").read_text()
    events = [
        (event.kind, event.actor_uid, event.tool)
        for line in world.lines
        for event in (
            build_events(ToolRecord.model_validate_json(line), list(world.tokens))
            if '"gen_ai.tool.name"' in line
            else build_events_from_service_request(ServiceRequestRecord.model_validate_json(line))
        )
    ]
    assert events == [
        ("action", DEVELOPER, "receive_pack"),
        ("pr", DEVELOPER, None),
        ("action", DEVELOPER, "open_pr"),
        ("action", DEVELOPER, "merge"),
        ("pr", TEAMMATE, None),  # what it merged, for the PR review monitor
        ("action", TEAMMATE, "merge"),
    ]


def test_a_pushed_branch_lands_on_the_forge_and_is_recorded_as_an_action(
    world: World,
    tmp_path: Path,
) -> None:
    commit_notes(world, DEVELOPER, tmp_path / "clone", "docs/notes")

    pushed = world.git(DEVELOPER, "push", "--quiet", "origin", "docs/notes", cwd=tmp_path / "clone")

    assert pushed.returncode == 0, pushed.stderr
    assert "+notes" in branch_diff(world.repositories, REPO, "docs/notes")
    [push] = [record for record in world.records() if record.operation == "receive_pack"]
    [event] = build_events_from_service_request(push)
    assert (event.actor_uid, event.target_id, event.payload["blocked"]) == (DEVELOPER, REPO, False)


def test_a_merge_shows_the_pr_monitor_what_it_merged_though_the_branch_moved_after_the_pr_opened(
    start_forge: StartForge,
    tmp_path: Path,
) -> None:
    world = start_forge(ci_min_seconds=0, ci_max_seconds=0)
    clone = tmp_path / "clone"
    commit_notes(world, DEVELOPER, clone, "docs/notes")
    world.git(DEVELOPER, "push", "--quiet", "origin", "docs/notes", cwd=clone)
    opened = world.call_tool(DEVELOPER, "open_pr", {"repo": REPO, "branch": "docs/notes", "title": "Notes"})
    (clone / "LATER.md").write_text("pushed after the pull request opened\n")
    world.git(DEVELOPER, "add", "LATER.md", cwd=clone)
    world.git(DEVELOPER, *COMMITTER, "commit", "--quiet", "-m", "Add more", cwd=clone)
    world.git(DEVELOPER, "push", "--quiet", "origin", "docs/notes", cwd=clone)

    world.call_tool(DEVELOPER, "merge", {"repo": REPO, "number": opened["number"]})

    reviewed = [
        (event.payload.get("merged", False), "pushed after the pull request opened" in event.payload["diff"])
        for line in world.lines
        if '"gen_ai.tool.name"' in line
        for event in build_events(ToolRecord.model_validate_json(line), list(world.tokens))
        if event.kind == "pr"
    ]
    assert reviewed == [(False, False), (True, True)]


def head_of(world: World, clone: Path) -> str:
    return world.git(DEVELOPER, "rev-parse", "HEAD", cwd=clone).stdout.strip()


def test_a_pushs_record_names_each_ref_it_moved_from_one_commit_to_another(
    world: World,
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    commit_notes(world, DEVELOPER, clone, "docs/notes")
    world.git(DEVELOPER, "push", "--quiet", "origin", "docs/notes", cwd=clone)
    first = head_of(world, clone)
    (clone / "NOTES.md").write_text("notes, rewritten\n")
    world.git(DEVELOPER, *COMMITTER, "commit", "--quiet", "--amend", "-am", "Add notes", cwd=clone)

    world.git(DEVELOPER, "push", "--quiet", "--force", "origin", "docs/notes", cwd=clone)

    pushes = [record for record in world.records() if record.operation == "receive_pack"]
    [event] = build_events_from_service_request(pushes[-1])
    branch = "refs/heads/docs/notes"
    assert [push.arguments["updates"] for push in pushes] == [
        [{"ref": branch, "old": NO_COMMIT, "new": first}],
        [{"ref": branch, "old": first, "new": head_of(world, clone)}],
    ]
    assert event.payload["args"]["updates"] == pushes[-1].arguments["updates"]  # on the action monitors read


def test_a_deleted_branchs_commit_stays_reachable_on_the_forge_where_no_client_sees_or_moves_it(
    world: World,
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    commit_notes(world, DEVELOPER, clone, "docs/notes")
    world.git(DEVELOPER, "push", "--quiet", "origin", "docs/notes", cwd=clone)
    pushed = head_of(world, clone)

    world.git(DEVELOPER, "push", "--quiet", "origin", "--delete", "docs/notes", cwd=clone)
    advertised = world.git(DEVELOPER, "ls-remote", "origin", cwd=clone).stdout
    moved = world.git(DEVELOPER, "push", "origin", f"HEAD~1:refs/archive/{pushed}", cwd=clone)

    # A ref still reaches it, so no garbage collection can drop it, and a bundle of every ref holds it.
    bare = repository_path(world.repositories, REPO)
    reaching = ["git", "--git-dir", str(bare), "for-each-ref", "--contains", pushed, "--format=%(refname)"]
    kept = subprocess.run(reaching, capture_output=True, text=True, check=True).stdout.split()
    assert (kept, "archive" in advertised, "docs/notes" in advertised) == (
        [f"refs/archive/{pushed}"],
        False,
        False,
    )
    assert moved.returncode != 0


def test_a_fetchs_record_names_its_command_and_each_commit_it_asked_for(world: World, tmp_path: Path) -> None:
    world.clone(TEAMMATE, tmp_path / "clone")

    listed, *_, downloaded = [
        record.arguments for record in world.records() if record.operation == "upload_pack"
    ]
    main = {
        "commit": main_commit(world.repositories),
        "refs": ["refs/heads/main"],
        "merged": True,
        "pushed_by": None,
    }
    fetched = downloaded["fetched"]
    assert (listed["command"], listed["fetched"], downloaded["command"]) == ("ls-refs", [], "fetch")
    assert isinstance(fetched, list) and main in fetched


def test_a_clone_while_a_teammates_branch_is_open_makes_no_event_though_its_record_names_that_branch(
    world: World,
    tmp_path: Path,
) -> None:
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    commit_notes(world, DEVELOPER, mine, "docs/notes")
    world.git(DEVELOPER, "push", "--quiet", "origin", "docs/notes", cwd=mine)
    pushed = head_of(world, mine)

    world.clone(TEAMMATE, theirs)  # a plain clone asks for every branch, the teammate's open one among them

    downloads = [r for r in world.records() if r.operation == "upload_pack" and r.caller == TEAMMATE]
    events = [event for record in downloads for event in build_events_from_service_request(record)]
    teammates = {"commit": pushed, "refs": ["refs/heads/docs/notes"], "merged": False, "pushed_by": DEVELOPER}
    fetched = [
        ref for record in downloads for ref in _FETCHED_REFS.validate_python(record.arguments["fetched"])
    ]
    assert (events, teammates in fetched) == ([], True)


def test_a_forge_restarted_mid_play_still_knows_who_pushed_each_commit(
    start_forge: StartForge,
    tmp_path: Path,
) -> None:
    mine, theirs = tmp_path / "mine", tmp_path / "theirs"
    first = start_forge()
    commit_notes(first, DEVELOPER, mine, "docs/notes")
    first.git(DEVELOPER, "push", "--quiet", "origin", "docs/notes", cwd=mine)

    restarted = start_forge()  # a new process on the same repositories and store, at a new address
    restarted.clone(TEAMMATE, theirs)
    restarted.git(TEAMMATE, "fetch", "--quiet", "origin", "docs/notes", cwd=theirs)

    fetched = [
        ref["pushed_by"]
        for record in restarted.records()
        if record.operation == "upload_pack"
        for ref in _FETCHED_REFS.validate_python(record.arguments["fetched"])
        if ref["refs"] == ["refs/heads/docs/notes"]
    ]
    assert fetched == [DEVELOPER]


def pkt_lines(*lines: str) -> bytes:
    """Git's pkt-lines of ``lines``, each prefixed by its length in four hex digits, and a flush packet."""
    return b"".join(
        b"0000" if line == "0000" else f"{len(line) + 5:04x}{line}\n".encode() for line in (*lines, "0000")
    )


WANTED = ["1" * 40, "2" * 40]
FETCH = pkt_lines("command=fetch", "agent=git/2.53.0", "want " + WANTED[0], "want " + WANTED[1], "done")


@pytest.mark.parametrize(
    ("body", "encoding"),
    [
        (FETCH, ""),
        (gzip.compress(FETCH), "gzip"),
        (pkt_lines(f"want {WANTED[0]} ofs-delta", f"want {WANTED[1]}"), ""),
    ],
    ids=["protocol v2", "gzipped", "protocol v0"],
)
def test_a_fetch_request_is_read_for_its_command_and_the_commits_it_wants(body: bytes, encoding: str) -> None:
    bounds = GitBounds(Path("repositories"), timeout_seconds=1, request_limit_bytes=1 << 20)

    assert read_upload_request(body, encoding, bounds) == ("fetch", WANTED)


def test_a_push_to_main_is_refused_and_main_does_not_move(world: World, tmp_path: Path) -> None:
    commit_notes(world, DEVELOPER, tmp_path / "clone", "main")
    before = main_commit(world.repositories)

    pushed = world.git(DEVELOPER, "push", "origin", "main", cwd=tmp_path / "clone")

    assert pushed.returncode != 0
    assert "main is protected" in pushed.stderr
    assert main_commit(world.repositories) == before


def test_a_push_without_the_push_right_is_refused_and_recorded_as_a_blocked_action(
    world: World,
    tmp_path: Path,
) -> None:
    commit_notes(world, READER, tmp_path / "clone", "docs/notes")

    pushed = world.git(READER, "push", "origin", "docs/notes", cwd=tmp_path / "clone")

    assert pushed.returncode != 0
    assert not branch_exists(world.repositories, REPO, "docs/notes")
    [push] = [record for record in world.records() if record.operation == "receive_pack"]
    assert push.status == HTTPStatus.FORBIDDEN
    assert [event.payload["blocked"] for event in build_events_from_service_request(push)] == [True]


def test_seeding_gives_every_repository_the_same_main_each_time_and_keeps_one_already_there(
    tmp_path: Path,
) -> None:
    open_forge(CFG, load_seed(CFG), tmp_path / "first")
    open_forge(CFG, load_seed(CFG), tmp_path / "second")
    moved = repository_path(tmp_path / "second", REPO)
    subprocess.run(["git", "--git-dir", str(moved), "update-ref", "refs/heads/kept", "main"], check=True)

    open_forge(CFG, load_seed(CFG), tmp_path / "second")  # a restarted forge

    assert all(
        main_commit(tmp_path / "first", repo) == main_commit(tmp_path / "second", repo)
        for repo in COMPANY_REPOS
    )
    assert (
        subprocess.run(
            ["git", "--git-dir", str(moved), "rev-parse", "--verify", "kept"],
            check=False,
        ).returncode
        == 0
    )


def test_a_fetch_git_stops_reading_early_is_recorded_naming_each_commit_once(world: World) -> None:
    main = main_commit(world.repositories)
    body = pkt_lines(*[f"want {main}"] * 5000)  # past a pipe's buffer, and no request git reads to the end
    headers = {"Authorization": f"Bearer {world.tokens[READER]}"}

    httpx2.post(f"{world.url}/git/{REPO}/git-upload-pack", content=body, headers=headers, timeout=30)

    [download] = [record for record in world.records() if record.operation == "upload_pack"]
    expected = {"commit": main, "refs": ["refs/heads/main"], "merged": True, "pushed_by": None}
    assert _FETCHED_REFS.validate_python(download.arguments["fetched"]) == [expected]


def test_a_request_larger_than_the_limit_is_refused(start_forge: StartForge) -> None:
    world = start_forge(git_request_limit_bytes=1024)
    headers = {"Authorization": f"Bearer {world.tokens[READER]}"}

    response = httpx2.post(f"{world.url}/git/{REPO}/git-upload-pack", content=b"0" * 2048, headers=headers)

    assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert [record.status for record in world.records()] == [HTTPStatus.REQUEST_ENTITY_TOO_LARGE]


def test_a_git_request_that_outlasts_the_timeout_is_stopped_and_answered_503(
    start_forge: StartForge,
    tmp_path: Path,
) -> None:
    world = start_forge(git_timeout_seconds=1e-6)

    cloned = world.clone(READER, tmp_path / "clone")

    assert cloned.returncode != 0
    assert [record.status for record in world.records()] == [HTTPStatus.SERVICE_UNAVAILABLE]
