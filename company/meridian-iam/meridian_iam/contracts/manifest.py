"""The typed operation manifest every service ships once; startup checks it against its real routes/tools."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

_OP = re.compile(r"[a-z][a-z0-9_]{0,63}")
_PERMISSION = re.compile(r"[a-z][a-z0-9_]{0,31}")
_RESOURCE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_PARAM = re.compile(r"\{[a-z][a-z0-9_]*\}")
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}


class ManifestError(ValueError):
    """The manifest is invalid, ambiguous, or does not cover the service's routes and tools."""


@dataclass(frozen=True)
class Operation:
    """One stable operation ID bound to the business permission it needs (or declared public)."""

    name: str
    permission: str | None
    requestable: bool
    public: bool
    target: str | None
    authenticated: bool = False
    launch: bool = False  # starts work its service may issue a workload identity for (needs a permission)


@dataclass(frozen=True)
class Manifest:
    """A validated, immutable manifest: operations plus the HTTP routes and MCP tools that map onto them."""

    resource: str
    operations: Mapping[str, Operation]
    http: Mapping[str, str]
    tools: Mapping[str, str]


def _mapping(raw: Any, where: str) -> Mapping[str, Any]:  # noqa: ANN401 - untrusted input
    if not isinstance(raw, Mapping) or not all(isinstance(k, str) for k in raw):
        raise ManifestError(f"{where} must be a mapping with string keys")
    return raw


def _only(raw: Mapping[str, Any], allowed: set[str], where: str) -> None:
    if extra := set(raw) - allowed:
        raise ManifestError(f"{where}: unknown keys {sorted(extra)}")


def _operation(name: str, raw: Any) -> Operation:  # noqa: ANN401 - untrusted input
    if not _OP.fullmatch(name):
        raise ManifestError(f"invalid operation id {name!r}")
    spec = _mapping(raw, f"operation {name}")
    _only(
        spec,
        {"permission", "requestable", "public", "authenticated", "target", "launch"},
        f"operation {name}",
    )
    for flag in ("requestable", "public", "authenticated", "launch"):
        if not isinstance(spec.get(flag, False), bool):
            raise ManifestError(f"{name}.{flag} must be a boolean")
    permission, public = spec.get("permission"), spec.get("public", False)
    authenticated = spec.get("authenticated", False)
    if (permission is not None) + public + authenticated != 1:
        raise ManifestError(f"{name}: declare exactly one of permission, public or authenticated")
    if permission is not None and not (isinstance(permission, str) and _PERMISSION.fullmatch(permission)):
        raise ManifestError(f"{name}: invalid permission name")
    if (public or authenticated) and spec.get("requestable"):
        raise ManifestError(f"{name}: only a permission-bearing operation is requestable")
    if public and spec.get("target"):
        raise ManifestError(f"{name}: a public operation has no target")
    target = spec.get("target")
    if target is not None and not (isinstance(target, str) and _OP.fullmatch(target)):
        raise ManifestError(f"{name}: invalid target name")
    launch = spec.get("launch", False)
    if launch and (permission is None or target is not None):
        raise ManifestError(f"{name}: a launch operation needs a permission and no target")
    return Operation(name, permission, spec.get("requestable", False), public, target, authenticated, launch)


def _bindings(raw: Any, kind: str, operations: Mapping[str, Operation]) -> dict[str, str]:  # noqa: ANN401
    bound = dict(_mapping(raw, kind))
    for key, op in bound.items():
        if not isinstance(op, str):
            raise ManifestError(f"{kind} {key!r}: the operation must be a string, not {type(op).__name__}")
        if op not in operations:
            raise ManifestError(f"{kind} {key}: unknown operation {op!r}")
        if kind == "http":
            method, _, path = key.partition(" ")
            if method not in _METHODS or not path.startswith("/"):
                raise ManifestError(f"http {key!r}: route must be 'METHOD /path'")
            if any(seg.startswith("{") and not _PARAM.fullmatch(seg) for seg in path.split("/")):
                raise ManifestError(f"http {key!r}: malformed path parameter")
            target = operations[op].target
            if target and f"{{{target}}}" not in path:
                raise ManifestError(f"http {key!r}: target {target!r} is not a path parameter")
    return bound


def _shape(route: str) -> tuple[str, tuple[str, ...]]:
    method, path = route.split(" ", 1)
    return method, tuple("{}" if seg.startswith("{") else seg for seg in path.split("/"))


def load_manifest(raw: Mapping[str, Any]) -> Manifest:
    """Validate a parsed manifest (e.g. from YAML); raise ``ManifestError`` on anything invalid."""
    raw = _mapping(raw, "manifest")
    _only(raw, {"schema_version", "resource", "operations", "http", "tools"}, "manifest")
    if raw.get("schema_version") != 1:
        raise ManifestError("unsupported schema_version")
    resource = raw.get("resource")
    if not (isinstance(resource, str) and _RESOURCE.fullmatch(resource)):
        raise ManifestError("invalid resource id")
    declared = _mapping(raw.get("operations"), "operations")
    if not declared:
        raise ManifestError("no operations declared")
    operations = {name: _operation(name, spec) for name, spec in declared.items()}
    http = _bindings(raw.get("http", {}), "http", operations)
    tools = _bindings(raw.get("tools", {}), "tools", operations)
    if unbound := set(operations) - {*http.values(), *tools.values()}:
        raise ManifestError(f"operations with no route or tool: {sorted(unbound)}")
    shapes = [_shape(route) for route in http]
    if len(set(shapes)) != len(shapes):
        raise ManifestError("ambiguous routes: two routes share a method and shape")
    return Manifest(resource, MappingProxyType(operations), MappingProxyType(http), MappingProxyType(tools))


def resolve_http(manifest: Manifest, method: str, path: str) -> tuple[Operation, str | None] | None:
    """Map a concrete request to ``(operation, target value)``; ``None`` means unknown: fail closed.

    Static segments beat parameters. Methods match exactly: HEAD and OPTIONS need their own declaration, and a
    trailing slash is a different path. Services bind through their router (slice 4).
    """
    parts = path.split("/")
    best: tuple[int, Operation, dict[str, str]] | None = None
    for route, op in manifest.http.items():
        route_method, template = route.split(" ", 1)
        want = template.split("/")
        if route_method != method or len(want) != len(parts):
            continue
        params: dict[str, str] = {}
        if all(
            (w == p) or (w.startswith("{") and p != "" and params.setdefault(w[1:-1], p) == p)
            for w, p in zip(want, parts, strict=True)
        ):
            statics = sum(not w.startswith("{") for w in want)
            if best is None or statics > best[0]:
                best = (statics, manifest.operations[op], params)
    if best is None:
        return None
    operation, params = best[1], best[2]
    return operation, params.get(operation.target) if operation.target else None


def check_coverage(manifest: Manifest, routes: set[str], tools: set[str]) -> None:
    """Startup check: every registered route/tool is declared, and every declaration is registered."""
    problems = [
        *(f"undeclared route {r}" for r in sorted(routes - set(manifest.http))),
        *(f"declared but unregistered route {r}" for r in sorted(set(manifest.http) - routes)),
        *(f"undeclared tool {t}" for t in sorted(tools - set(manifest.tools))),
        *(f"declared but unregistered tool {t}" for t in sorted(set(manifest.tools) - tools)),
    ]
    if problems:
        raise ManifestError("; ".join(problems))
