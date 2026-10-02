"""The execution app: one agent's code tools, served in that agent's own sandbox."""

# No ``from __future__ import annotations``: FastAPI reads route annotations at runtime.

import os
import threading
from http import HTTPStatus
from pathlib import Path

from fastapi import FastAPI, HTTPException

from loc_arena.execution.checkout import Checkout, list_codebase_repositories
from loc_arena.execution.workspace import Workspace
from loc_arena.stack.constants import (
    HEALTH_ROUTE,
    IMAGE_CODEBASE_PATH,
    SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE,
    TOOL_CALLS_ROUTE,
    WORKSPACES_ROUTE,
)
from loc_arena.stack.contracts import (
    CodeToolCall,
    CodeToolResult,
    EpisodeHandle,
    ExecutionHealth,
    WorkspaceCreate,
)
from loc_arena.stack.settings import load_settings_from_environment


def create_execution_app(workspace: Workspace, *, agent_id: str, seed_source: Path) -> FastAPI:
    """The app serving ``workspace`` for ``agent_id``; opening an episode seeds it from ``seed_source``."""
    app = FastAPI(title=f"LOC-Arena execution ({agent_id})")
    opening = threading.Lock()  # sync routes run in a thread pool: one episode opens at a time
    open_handle: str | None = None

    @app.get(HEALTH_ROUTE)
    def report_health() -> ExecutionHealth:
        return ExecutionHealth(ok=True, agent_id=agent_id)

    @app.post(WORKSPACES_ROUTE, status_code=HTTPStatus.CREATED)
    def open_workspace(request: WorkspaceCreate) -> None:
        nonlocal open_handle
        with opening:
            if open_handle not in (None, request.handle):
                raise HTTPException(HTTPStatus.CONFLICT, detail="this sandbox already serves another episode")
            workspace.seed(seed_source)
            open_handle = request.handle

    @app.post(TOOL_CALLS_ROUTE)
    def run_tool_call(handle: EpisodeHandle, call: CodeToolCall) -> CodeToolResult:
        if handle != open_handle:
            raise HTTPException(HTTPStatus.NOT_FOUND, detail="no workspace is open for this episode")
        return workspace.run(call)

    return app


def build_execution_app(codebase: Path = IMAGE_CODEBASE_PATH) -> FastAPI:
    """The app uvicorn serves in a sandbox (``--factory``), from the settings and agent id compose renders."""
    settings = load_settings_from_environment()
    checkout = Checkout(settings.execution.workspace_root, list_codebase_repositories(codebase))
    return create_execution_app(
        Workspace(checkout, settings.execution, shell_enabled=True),
        agent_id=os.environ[SANDBOX_AGENT_ID_ENVIRONMENT_VARIABLE],
        seed_source=codebase,
    )
