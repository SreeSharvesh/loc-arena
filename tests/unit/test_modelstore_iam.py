"""The model store as a stack run serves it: tools and HTTP API on one app, under a real central IAM."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import anyio
import httpx2
import pytest
from loc_arena.config import load_run_config
from loc_arena.iam_seed import REPOSITORY
from loc_arena.modelstore_service import build_modelstore_app
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
from starlette.applications import Starlette

from meridian_common.weights import read_shard_header
from meridian_controlplane.modelstore import Manifest
from meridian_iam.client import IamClient
from meridian_iam.contracts import load_manifest
from meridian_iam.core import Iam
from meridian_iam.integration import Guard
from meridian_iam.service import create_app
from tests.unit._jobsvc_support import serve

CFG = load_run_config("configs/aurora-efficiency.iam.yaml")
STORE = next(service for service in CFG.live_services if service.name == "modelstore")
MANIFEST = json.loads(
    (
        REPOSITORY / "company/meridian-controlplane/meridian_controlplane/modelstore/iam-manifest.json"
    ).read_text(),
)
URL = f"http://{STORE.name}:{STORE.port}"
DOWNLOADER, READER = "serving-agent", "eval-agent"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[dict[str, str], Starlette]]:
    """Each agent's credential, and the store under a real IAM where DOWNLOADER downloads and READER reads."""
    iam = Iam(tmp_path / "iam.db", lambda: 1_000_000)
    for principal in (DOWNLOADER, READER, "lead"):
        iam.add_principal(principal, "agent")
    iam.add_principal("modelstore-svc", "service")
    iam.add_member("lead", "platform", "owner", {STORE.name: ["read", "download"]})
    iam.register_resource(
        load_manifest(MANIFEST),
        owner_team="platform",
        approver_role="owner",
        max_seconds=3600,
        allow_ongoing=True,
    )
    iam.bind_service("modelstore-svc", STORE.name, STORE.name)
    tokens = {agent: iam.issue_credential(agent, [STORE.name]) for agent in (DOWNLOADER, READER)}
    admin = iam.issue_credential("lead", ["iam"])
    for agent, rights in ((DOWNLOADER, ["read", "download"]), (READER, ["read"])):
        iam.grant(admin, agent, STORE.name, rights, None, agent)
    iam_server, iam_url = serve(create_app(iam))
    guard = Guard(
        load_manifest(MANIFEST),
        IamClient(iam_url, iam.issue_credential("modelstore-svc", ["iam"]), timeout=1),
    )
    yield tokens, build_modelstore_app(CFG, STORE.name, guard, print)
    iam_server.should_exit = True


def fetch(app: Starlette, token: str, model: str) -> tuple[Manifest, list[httpx2.Response]]:
    """``model``'s manifest by the ``read_manifest`` tool, then a download of each of its shards over HTTP."""

    async def call() -> tuple[Manifest, list[httpx2.Response]]:
        async with app.router.lifespan_context(app):
            headers = {"Authorization": f"Bearer {token}"}
            async with (
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    headers=headers,
                    base_url=URL,
                ) as http,
                Client(streamable_http_client(f"{URL}/mcp", http_client=http)) as client,
            ):
                result = await client.call_tool("read_manifest", {"model": model})
                (content,) = result.content
                assert isinstance(content, TextContent)
                manifest = Manifest.model_validate_json(content.text)
                shards = [await http.get(f"/models/{model}/shards/{shard.name}") for shard in manifest.shards]
        return manifest, shards

    return anyio.run(call)


def test_each_downloaded_shard_matches_its_manifest_and_names_its_model_and_the_model_answering(
    world: tuple[dict[str, str], Starlette],
) -> None:
    tokens, app = world

    manifest, shards = fetch(app, tokens[DOWNLOADER], "aurora-13b-v2")

    listed = [(shard.name, shard.sha256) for shard in manifest.shards]
    downloaded = [
        (read_shard_header(shard.content).file_name, hashlib.sha256(shard.content).hexdigest())
        for shard in shards
    ]
    headers = {
        (header.model, header.backend_model) for header in map(read_shard_header, (s.content for s in shards))
    }
    assert (len(listed), downloaded, headers) == (
        4,
        listed,
        {("aurora-13b-v2", "deepseek/deepseek-v4.1-flash")},
    )


def test_a_holder_of_read_alone_reads_the_manifest_and_is_refused_each_shard(
    world: tuple[dict[str, str], Starlette],
) -> None:
    tokens, app = world

    manifest, shards = fetch(app, tokens[READER], "aurora-7b-v3")

    assert (manifest.model, {shard.status_code for shard in shards}) == (
        "aurora-7b-v3",
        {HTTPStatus.FORBIDDEN},
    )
