"""A scenario pack's `services:`: which entries are live, and every entry the loader refuses, naming it."""

from __future__ import annotations

from pathlib import Path

import pytest
from scenarios.loader import EngineModule, LiveService, load_scenario

PACK_HEADER = "scorer: a_scorer\nverifier: a_verifier\nservices:\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A scenarios root: a pack with a service directory and a sealed one, and a Dockerfile outside it."""
    for built in ("pack/services/notes", "pack/reference", "pack", "outside"):
        (tmp_path / built).mkdir(parents=True, exist_ok=True)
        (tmp_path / built / "Dockerfile").write_text("FROM scratch\n")
    (tmp_path / "pack" / "services" / "empty").mkdir()
    return tmp_path


def load_services(root: Path, services: str) -> object:
    (root / "pack" / "scenario.yaml").write_text(PACK_HEADER + services)
    return load_scenario("pack", root=root).live_services


@pytest.mark.parametrize(
    ("entry", "named", "reason"),
    [
        ("gateway: {image: x, port: 1}", "gateway", "taken by the stack"),
        ("episode: {image: x, port: 1}", "episode", "taken by the stack"),
        ("agentgateway: {image: x, port: 1}", "agentgateway", "taken by the stack"),
        ("sandbox-agent-main: {image: x, port: 1}", "sandbox-agent-main", "taken by the stack"),
        ("Notes: {image: x, port: 1}", "Notes", "must be a DNS label"),
        ("notes: {build: services/notes, image: x, port: 1}", "notes", "give one of build, image or module"),
        ("forge: {image: x, module: a.module, port: 1}", "forge", "give one of build, image or module"),
        ("notes: {build: ../outside, port: 1}", "notes", "is outside"),
        ("notes: {build: ., port: 1}", "notes", "must be a directory under the pack"),
        ("notes: {build: reference, port: 1}", "notes", "must be a directory under the pack"),
        ("notes: {build: services/empty, port: 1}", "notes", "holds no Dockerfile"),
        ("notes: {build: services/notes}", "notes", "needs a port"),
        ("forge: {module: a.module}", "forge", r"a live service \(build, image or module\) needs a port"),
        ("notes: {build: services/notes, port: 1, rights: [Read]}", "notes", "should match pattern"),
        ("notes: {build: services/notes, port: 1, rights: [read, read]}", "notes", "names 'read' twice"),
        ("forge: {module: a.module, port: 1, tools: [open pr]}", "forge", "should match pattern"),
        ("forge: {module: a.module, port: 1, tools: [open_pr, open_pr]}", "forge", "names 'open_pr' twice"),
        (
            "notes: {build: services/notes, port: 1, rights: [read], transitive: true}",
            "notes",
            "transitive needs",
        ),
        ("notes: {image: x, port: 1}", "notes", "a ready image needs a healthcheck"),
        (
            "notes: {image: x, port: 1, volumes: ['/var/run/docker.sock:/var/run/docker.sock']}",
            "notes",
            "Extra inputs are not permitted",
        ),
    ],
    ids=[
        "the gateway's name",
        "the episode's name",
        "the tools gateway's name",
        "a sandbox's name",
        "no DNS label",
        "both build and image",
        "both image and module",
        "build outside the pack",
        "build at the pack's root",
        "build in the sealed reference",
        "build with no Dockerfile",
        "live with no port",
        "a module with no port",
        "a right in upper case",
        "a right named twice",
        "a tool name with a space",
        "a tool named twice",
        "transitive with no grant right",
        "a ready image with no healthcheck",
        "a key no service may have",
    ],
)
def test_an_invalid_service_entry_is_refused_at_load_naming_it(
    root: Path,
    entry: str,
    named: str,
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=f"(?s)service '{named}' is invalid.*{reason}"):
        load_services(root, f"  {entry}\n")


def test_an_entry_with_neither_build_nor_image_stays_simulated(root: Path) -> None:
    services = load_services(root, "  ticketboard: {port: 8090, rights: [read]}\n")

    assert services == ()


def test_an_entry_with_a_module_is_live_running_that_module_of_the_engine_image(root: Path) -> None:
    services = load_services(root, "  forge: {module: a.module, port: 8000, tools: [open_pr]}\n")

    assert services == (
        LiveService(
            name="forge",
            source=EngineModule("a.module"),
            port=8000,
            command=None,
            healthcheck=None,
            rights=(),
            transitive=False,
            description="",
            tools=("open_pr",),
        ),
    )


def test_two_live_services_serving_one_tool_name_are_refused_naming_both(root: Path) -> None:
    services = (
        "  forge: {module: a.module, port: 1, tools: [open_pr]}\n"
        "  board: {module: b.module, port: 2, tools: [open_pr]}\n"
    )

    with pytest.raises(ValueError, match=r"tool 'open_pr' is served by both 'forge' and 'board'"):
        load_services(root, services)
