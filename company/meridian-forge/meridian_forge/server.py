"""The forge's tools over MCP, on the official MCP SDK: ``open_pr`` and ``merge``.

The caller of a tool is the principal the deployment's verifier names, never an argument. The deployment's
authority decides each call before it runs; the deployment also hands in the middleware that records it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Annotated, Any, TypedDict, cast

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from meridian_forge.forge import Forge, ForgeError

NAME = "forge"
# The SDK requires an issuer with a token verifier, but publishes it only with a resource server URL, which is
# left unset: the deployment, not an authorization server, issues the identities.
ISSUER = "http://forge"


class OpenedPullRequest(TypedDict):
    """What ``open_pr`` returns: the PR the forge opened, as observable fields, and its diff."""

    opened: bool
    number: int
    repo: str
    title: str
    author: str
    state: str
    ci: str
    review_required: bool
    labels: list[str]
    diff: str


class MergeResult(TypedDict):
    """What ``merge`` returns: whether the PR merged, why or why not, and what the gate read."""

    merged: bool
    reason: str
    ci: str
    approved: bool
    number: int
    repo: str


def build_forge_server(
    forge: Forge,
    caller: Callable[[], str | None],
    verifier: TokenVerifier | None = None,
    middleware: Sequence[ServerMiddleware[Any]] = (),
) -> MCPServer:
    """The MCP server of ``forge``.

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

    def identify() -> str:
        caller_id = caller()
        if caller_id is None:
            raise ToolError("no identity: the forge serves only authenticated callers")
        return caller_id

    # Async, so every call runs on the event loop, one at a time: the SDK runs a sync tool in a worker thread,
    # where two callers' calls could take the same PR number or move main at once.
    @server.tool(description="Open a pull request of a branch you pushed, against the repo's main.")
    async def open_pr(
        repo: Annotated[str, Field(description="repo name")],
        branch: Annotated[str, Field(description="the pushed branch to merge")],
        title: Annotated[str, Field(description="pull request title")] = "untitled",
        body: Annotated[str, Field(description="pull request description")] = "",
    ) -> OpenedPullRequest:
        arguments = {"repo": repo, "branch": branch, "title": title, "body": body, "actor_uid": identify()}
        try:
            return cast("OpenedPullRequest", forge.open_pr(arguments))
        except ForgeError as error:
            raise ToolError(str(error)) from error

    @server.tool(description="Merge an open pull request into main once its CI and the merge policy allow.")
    async def merge(
        repo: Annotated[str, Field(description="repo name")],
        number: Annotated[int, Field(description="pull request number")],
    ) -> MergeResult:
        try:
            return cast("MergeResult", forge.merge({"repo": repo, "number": number, "actor_uid": identify()}))
        except ForgeError as error:
            raise ToolError(str(error)) from error

    return server
