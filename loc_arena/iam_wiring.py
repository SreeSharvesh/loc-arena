"""How a stack service joins central IAM, which every agent-facing service runs under.

A service joins by environment (``service_environment``): the renderer sets ``LOC_ARENA_IAM_URL``, the path
of its manifest, the path of its own IAM credential, and how long and how often it asks IAM. Company packages
are importable only inside a service container (the episode and the host do not put them on the path), so the
imports are made when a service starts, never at module load. A service started without them does not start.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
from fastapi import Depends, FastAPI
from mcp.server import MCPServer
from starlette.applications import Starlette
from starlette.routing import Mount

from loc_arena.mcp_service import build_app
from loc_arena.settings import IamSettings

URL_VARIABLE = "LOC_ARENA_IAM_URL"
MANIFEST_VARIABLE = "LOC_ARENA_IAM_MANIFEST"
CREDENTIAL_VARIABLE = "LOC_ARENA_IAM_CREDENTIAL"
TIMEOUT_VARIABLE = "LOC_ARENA_IAM_TIMEOUT_SECONDS"
RETRIES_VARIABLE = "LOC_ARENA_IAM_RETRIES"


def service_environment(settings: IamSettings, manifest: str, credential: str) -> dict[str, str]:
    """What a managed service's environment holds to join IAM: what ``guard_from_environment`` reads."""
    return {
        URL_VARIABLE: f"http://{settings.service}:{settings.port}",
        MANIFEST_VARIABLE: manifest,
        CREDENTIAL_VARIABLE: credential,
        TIMEOUT_VARIABLE: str(settings.timeout_seconds),
        RETRIES_VARIABLE: str(settings.retries),
    }


def guard_from_environment(environ: Mapping[str, str]) -> Any:  # noqa: ANN401 - a meridian_iam Guard
    """The IAM guard this service is configured for; without one, the service refuses to start.

    It refuses as well without a valid timeout and retries: the settings' bounds hold here.
    """
    if URL_VARIABLE not in environ:
        raise SystemExit(f"this service runs only under central IAM, and {URL_VARIABLE} is not set")
    try:
        limits = IamSettings.model_validate(
            {"timeout_seconds": float(environ[TIMEOUT_VARIABLE]), "retries": int(environ[RETRIES_VARIABLE])},
        )
    except (KeyError, ValueError) as error:  # pydantic's ValidationError is a ValueError
        wanted = f"{TIMEOUT_VARIABLE} and {RETRIES_VARIABLE}"
        raise SystemExit(f"central IAM needs a valid {wanted}: {error}") from None
    client_module = importlib.import_module("meridian_iam.client")
    contracts = importlib.import_module("meridian_iam.contracts")
    integration = importlib.import_module("meridian_iam.integration")
    manifest = contracts.load_manifest(json.loads(Path(environ[MANIFEST_VARIABLE]).read_text()))
    credential = Path(environ[CREDENTIAL_VARIABLE]).read_text().strip()
    client = client_module.IamClient(
        environ[URL_VARIABLE],
        credential,
        timeout=limits.timeout_seconds,
        retries=limits.retries,
    )
    return integration.Guard(manifest, client)


def build_service_app(
    guard: Any,  # noqa: ANN401 - a meridian_iam Guard
    server: MCPServer,
    service: str,
    api: FastAPI | None = None,
) -> Starlette:
    """The app serving ``server`` as ``service``, and ``api`` beside it, if they serve ``guard``'s manifest.

    Together their tools and routes must be exactly the manifest's: a tool or route added without a declared
    operation would otherwise be reachable and unchecked by name, and a declared operation with neither means
    the manifest describes a service that is not the one running. The app also answers 503 when IAM cannot
    verify a caller.
    """
    tools = {tool.name for tool in anyio.run(server.list_tools)}
    if api is None:
        guard.verify_routes(set(), tools)
    else:
        importlib.import_module("meridian_iam.integration.asgi").verify_app(guard, api, tools)
    app = importlib.import_module("meridian_iam.integration.mcp").answer_outages(build_app(server, service))
    if api is not None:
        app.router.routes.append(Mount("/", app=api))  # after /mcp, so the HTTP API serves the rest
    return app


def http_dependency(guard: Any) -> Any:  # noqa: ANN401 - a meridian_iam Guard, and FastAPI's Depends
    """The app-level dependency through which ``guard`` decides each HTTP request before its handler."""
    return Depends(importlib.import_module("meridian_iam.integration.asgi").dependency(guard))


def load_iam_authority(guard: Any) -> Any:  # noqa: ANN401 - a meridian_iam Guard, and the CentralAuthority over it
    """The ``CentralAuthority`` over ``guard`` that a company service takes."""
    return importlib.import_module("loc_arena.agents_iam").IamAuthority(guard)


def iam_options(guard: Any, service: str) -> dict[str, Any]:  # noqa: ANN401 - a meridian_iam Guard
    """The ``verifier`` and ``extra_middleware`` that put ``guard`` in front of an MCP service."""
    mcp = importlib.import_module("meridian_iam.integration.mcp")
    return {
        "verifier": mcp.IamVerifier(guard, service),
        "extra_middleware": (mcp.tool_authorizer(guard),),
        "annotate": mcp.decision_attributes,
    }
