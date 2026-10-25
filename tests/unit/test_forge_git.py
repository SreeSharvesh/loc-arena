"""The forge's git repositories as a stack run serves them: real git over HTTP, with a real IAM deciding."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any

import httpx2
import pytest
from loc_arena.config import load_run_config
from loc_arena.forge_seed import seed_company_repositories
from loc_arena.forge_service import SERVICE, build_forge_app
from loc_arena.iam_seed import REPOSITORY
from loc_arena.recorded_events import ServiceRequestRecord, build_events_from_service_request
from loc_arena.settings import ForgeSettings
from loc_arena.tasks.main_task_grader import COMPANY_REPOS

from meridian_forge.repositories import repository_path
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
READER, OUTSIDER = "serving-agent", "eval-agent"  # OUTSIDER holds nothing on the forge
REPO = "meridian-serving"
# A client's git, with neither the user's nor the system's configuration, and no prompt for a password.
CLIENT_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
COMMITTER = ["-c", "user.name=Serving Agent", "-c", "user.email=serving-agent@meridian.example"]


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

    def clone(self, agent: str, destination: Path) -> subprocess.CompletedProcess[str]:
        return self.git(agent, "clone", f"{self.url}/git/{REPO}", str(destination))

    def records(self) -> list[ServiceRequestRecord]:
        return [
            ServiceRequestRecord.model_validate_json(line) for line in self.lines if '"operation"' in line
        ]


StartForge = Callable[..., World]


@pytest.fixture
def start_forge(tmp_path: Path) -> Iterator[StartForge]:
    """Start the forge under a real IAM: its forge settings the run config's but for those given."""
    iam = Iam(tmp_path / "iam.db", lambda: 1_000_000)
    for principal in (READER, OUTSIDER, "lead"):
        iam.add_principal(principal, "agent")
    iam.add_principal("forge-svc", "service")
    iam.add_member("lead", "platform", "owner", {SERVICE: ["read", "open_pr"]})
    iam.register_resource(
        load_manifest(MANIFEST),
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
        allow_ongoing=True,
    )
    iam.bind_service("forge-svc", SERVICE, SERVICE)
    tokens = {agent: iam.issue_credential(agent, [SERVICE, f"iam:{SERVICE}"]) for agent in (READER, OUTSIDER)}
    admin = iam.issue_credential("lead", ["iam"])
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


def test_a_pull_brings_the_commit_main_moved_to(world: World, tmp_path: Path) -> None:
    world.clone(READER, tmp_path / "clone")
    # main moves on the forge's side, as a merge will move it: a commit pushed straight to the bare repository
    other = tmp_path / "other"
    subprocess.run(
        ["git", "clone", "--quiet", str(repository_path(world.repositories, REPO)), str(other)],
        check=True,
    )
    (other / "NOTES.md").write_text("merged by a teammate\n")
    subprocess.run(["git", "-C", str(other), "add", "NOTES.md"], check=True)
    subprocess.run(["git", *COMMITTER, "-C", str(other), "commit", "--quiet", "-m", "Add notes"], check=True)
    subprocess.run(["git", "-C", str(other), "push", "--quiet", "origin", "main"], check=True)

    pulled = world.git(READER, "pull", "--quiet", cwd=tmp_path / "clone")

    assert pulled.returncode == 0, pulled.stderr
    assert (tmp_path / "clone/NOTES.md").read_text() == "merged by a teammate\n"


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


def test_a_push_is_refused_and_main_does_not_move(world: World, tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    world.clone(READER, clone)
    before = main_commit(world.repositories)
    (clone / "NOTES.md").write_text("pushed straight to main\n")
    world.git(READER, "add", "NOTES.md", cwd=clone)
    world.git(READER, *COMMITTER, "commit", "--quiet", "-m", "Add notes", cwd=clone)

    pushed = world.git(READER, "push", "origin", "main", cwd=clone)

    assert pushed.returncode != 0
    assert main_commit(world.repositories) == before


def test_seeding_gives_every_repository_the_same_main_each_time_and_keeps_one_already_there(
    tmp_path: Path,
) -> None:
    seed_company_repositories(ForgeSettings(repositories=tmp_path / "first"))
    seed_company_repositories(ForgeSettings(repositories=tmp_path / "second"))
    moved = repository_path(tmp_path / "second", REPO)
    subprocess.run(["git", "--git-dir", str(moved), "update-ref", "refs/heads/kept", "main"], check=True)

    seed_company_repositories(ForgeSettings(repositories=tmp_path / "second"))  # a restarted forge

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
