"""The deploy service's tools over MCP, on the official MCP SDK: ``deploy`` and ``list_deployments``.

The caller of a tool is the principal the deployment's verifier names, never an argument. The deployment's
authority decides each call before it runs; the deployment also hands in the middleware that records it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from meridian_deploy.deployments import DeployError, Deployment, Deployments

NAME = "deploy"
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the deployment, not an authorization server, issues the identities.
ISSUER = "http://deploy"
Service = Annotated[str, Field(description="the service's name, such as notes")]


class DeploymentList(BaseModel):
    """What ``list_deployments`` returns: a service's deployments, newest first."""

    deployments: list[Deployment]


def build_deploy_server(
    deployments: Deployments,
    caller: Callable[[], str | None],
    verifier: TokenVerifier | None = None,
    middleware: Sequence[ServerMiddleware[Any]] = (),
) -> MCPServer:
    """The MCP server over ``deployments``.

    ``caller`` names who calls and ``verifier`` admits callers over HTTP; ``middleware`` runs around each
    call.
    """
    auth = AuthSettings(issuer_url=ISSUER, resource_server_url=None) if verifier else None
    server = MCPServer(
        NAME,
        token_verifier=verifier,
        auth=auth,
        middleware=list(middleware),
        log_level="WARNING",
    )

    # Sync, so the SDK runs each call in a worker thread: starting a deployment waits for its credential.
    # The deployment then runs in a thread of the registry's, which takes one of a service at a time.
    @server.tool(
        description="Start a deployment of the code merged on main to a service, and return at once with "
        "its number and the state started. The deployment goes on by itself: the service starts again on "
        "every repo's main as the forge holds it now. Ask list_deployments how it ended: serving, or "
        "failed, the service then running what it ran before, with the reason or the end of its errors in "
        "detail. A service takes one deployment at a time: while one is in progress, deploy is refused.",
    )
    def deploy(service: Service) -> Deployment:
        caller_id = caller()
        if caller_id is None:
            raise ToolError("no identity: the deploy service serves only authenticated callers")
        try:
            return deployments.deploy(caller_id, service)
        except DeployError as error:
            raise ToolError(str(error)) from error

    @server.tool(
        description="A service's deployments, newest first: who, when, each repo's commit, and its state: "
        "started while it is in progress, then serving or failed, with why in detail.",
    )
    def list_deployments(service: Service) -> DeploymentList:
        try:
            return DeploymentList(deployments=deployments.list_deployments(service))
        except DeployError as error:
            raise ToolError(str(error)) from error

    return server
