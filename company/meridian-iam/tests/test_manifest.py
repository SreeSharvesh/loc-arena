"""The manifest contract: strict loading, immutability, route resolution and startup coverage."""

from __future__ import annotations

import copy
from types import MappingProxyType
from typing import Any

import pytest
from iam_support import NOTES

from meridian_iam.contracts import ManifestError, check_coverage, load_manifest, resolve_http


def mutated(change: Any) -> dict[str, Any]:  # noqa: ANN401
    raw = copy.deepcopy(NOTES)
    change(raw)
    return raw


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(extra=1),
        lambda r: r.update(schema_version=2),
        lambda r: r.update(resource="Bad Name"),
        lambda r: r["operations"]["admin"].update(requestable="yes"),
        lambda r: r["operations"]["admin"].update(public=True),
        lambda r: r["operations"]["health"].update(permission="read"),
        lambda r: r["operations"]["health"].update(requestable=True),
        lambda r: r["operations"]["admin"].update(permission="Not Valid"),
        lambda r: r["operations"]["admin"].update(surprise=1),
        lambda r: r["operations"].update({"orphan": {"permission": "read"}}),
        lambda r: r["http"].update({"GET /x": "nope"}),
        lambda r: r["http"].update({"FETCH /x": "admin"}),
        lambda r: r["http"].update({"GET /notes/{id}": "read_note"}),
        lambda r: r["http"].update({"GET /notes/{key": "read_note"}),
        lambda r: r["operations"]["read_note"].update(target="missing"),
        lambda r: r["operations"]["admin"].update(launch="yes"),
        lambda r: r["operations"]["health"].update(launch=True),  # a launch needs a permission
        lambda r: r["operations"]["read_note"].update(launch=True),  # and no target
        lambda r: r.update(operations={}),
        lambda r: r.update(tools=[]),  # a falsy non-mapping is not "no tools"
        lambda r: r.update(http=[]),
        lambda r: r.update(tools=None),
        lambda r: r["http"].update({"GET /x": ["read_note"]}),  # a binding must name its operation by string
        lambda r: r["http"].update({"GET /y": {"op": "read_note"}}),
        lambda r: r["http"].update({"GET /z": 7}),
    ],
)
def test_invalid_manifest_is_rejected(change: Any) -> None:  # noqa: ANN401
    with pytest.raises(ManifestError):
        load_manifest(mutated(change))


def test_manifest_is_immutable() -> None:
    manifest = load_manifest(NOTES)
    assert isinstance(manifest.operations, MappingProxyType)
    with pytest.raises(TypeError):
        manifest.http["GET /evil"] = "admin"  # ty: ignore[invalid-assignment]


def test_static_route_beats_parameter_and_unknowns_fail_closed() -> None:
    raw = mutated(
        lambda r: (
            r["operations"].update(latest={"permission": "read"}),
            r["http"].update({"GET /notes/latest": "latest"}),
        ),
    )
    manifest = load_manifest(raw)
    latest = resolve_http(manifest, "GET", "/notes/latest")
    assert latest is not None
    assert latest[0].name == "latest"
    keyed = resolve_http(manifest, "GET", "/notes/abc")
    assert keyed is not None
    op, target = keyed
    assert (op.name, target) == ("read_note", "abc")
    for method, path in [
        ("HEAD", "/notes/abc"),
        ("OPTIONS", "/notes/abc"),
        ("GET", "/notes/abc/"),
        ("DELETE", "/notes/abc"),
        ("GET", "/notes/"),
    ]:
        assert resolve_http(manifest, method, path) is None


def test_coverage_requires_exact_match() -> None:
    manifest = load_manifest(NOTES)
    check_coverage(manifest, set(NOTES["http"]), set())
    with pytest.raises(ManifestError, match="undeclared route DELETE /notes/x"):
        check_coverage(manifest, {*NOTES["http"], "DELETE /notes/x"}, set())
    with pytest.raises(ManifestError, match="declared but unregistered route POST /admin"):
        check_coverage(manifest, set(NOTES["http"]) - {"POST /admin"}, set())
