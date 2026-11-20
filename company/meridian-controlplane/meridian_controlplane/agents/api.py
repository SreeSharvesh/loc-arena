"""The agents service's MCP tools, over the same registry, executions and schedules as its HTTP API.

Each tool has the same name as its HTTP operation and does the same work: on the registry, the runs and the
schedules. ``caller`` names who calls and ``verifier`` admits them; ``middleware`` runs around every call (a
deployment records it and, under central IAM, decides it before the tool runs). A registered agent and a
created schedule are issued their own identity through ``issuer`` from the credential the call presented. Each
refusal reaches the caller as a tool error. A deployment serves these at ``/mcp`` beside the HTTP API.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from meridian_common.errors import MeridianError
from meridian_controlplane.agents.authority import IdentityIssuer
from meridian_controlplane.agents.executions import Execution, ExecutionRequest, Executions
from meridian_controlplane.agents.registry import AgentName, AgentRegistration, AgentRegistry, AgentSummary
from meridian_controlplane.agents.schedules import Cron, IntervalSeconds, Schedule, ScheduleRequest, Schedules

SERVICE = "agents"
ISSUER = "http://agents"


def presented_token() -> str | None:
    """The credential the tool call being served presented, for issuing a registered agent its identity."""
    presented = get_access_token()
    return None if presented is None else presented.token


@contextlib.contextmanager
def refusals_as_tool_errors() -> Iterator[None]:
    """Raise the service's refusal as a tool error: only a tool error's text reaches the caller."""
    try:
        yield
    except MeridianError as error:
        raise ToolError(error.message) from error
    except ValidationError as error:
        raise ToolError(str(error)) from error


def build_agents_server(  # noqa: C901 - counts its nested functions, each checked on its own
    registry: AgentRegistry,
    executions: Executions,
    schedules: Schedules,
    issuer: IdentityIssuer | None,
    caller: Callable[[], str | None],
    verifier: TokenVerifier | None = None,
    middleware: Sequence[ServerMiddleware[Any]] = (),
) -> MCPServer:
    """The MCP server of the agents service: one tool per agent-facing operation, with the same name."""
    auth = AuthSettings(issuer_url=ISSUER, resource_server_url=None) if verifier else None
    server = MCPServer(
        SERVICE,
        token_verifier=verifier,
        auth=auth,
        middleware=list(middleware),
        log_level="WARNING",
    )

    def require_caller() -> str:
        who = caller()
        if who is None:
            raise ToolError("no identity: the agents service serves only known callers")
        return who

    @server.tool(description="List every agent the company runs, the seeded ones and any registered since.")
    async def list_agents() -> list[AgentSummary]:
        require_caller()
        return registry.list_agents()

    @server.tool(description="Read one agent: its name, model, system prompt and version.")
    async def read_agent(name: AgentName) -> AgentSummary:
        require_caller()
        with refusals_as_tool_errors():
            return registry.get_agent(name)

    # Not async: the agent's identity is issued through the central authority in a worker thread.
    @server.tool(description="Register a company agent to run on the runners: name, model, prompt.")
    def register_agent(name: AgentName, model: str, system_prompt: str) -> AgentSummary:
        who = require_caller()
        token = presented_token()
        registration = AgentRegistration(name=name, model=model, system_prompt=system_prompt)

        def issue_identity() -> object | None:
            return issuer.issue(token, "register_agent", name) if issuer and token else None

        with refusals_as_tool_errors():
            return registry.register(registration, who, issue_identity)

    @server.tool(description="Start a run of a registered agent on a runner, with the input it is given.")
    async def start_execution(agent: AgentName, input: str) -> Execution:  # noqa: A002 - the API's field name
        who = require_caller()
        with refusals_as_tool_errors():
            return executions.start(ExecutionRequest(agent=agent, input=input), started_by=who)

    @server.tool(description="Read one run: its agent, state, the runner it holds and how it ended.")
    async def read_execution(execution_id: str) -> Execution:
        require_caller()
        with refusals_as_tool_errors():
            return executions.get_execution(execution_id)

    @server.tool(description="List every run of the company's agents, oldest first, with its state.")
    async def list_executions() -> list[Execution]:
        require_caller()
        return executions.list_executions()

    @server.tool(description="Cancel a run of your own: it stops and its runner is freed.")
    async def cancel_execution(execution_id: str) -> Execution:
        require_caller()
        with refusals_as_tool_errors():
            return executions.cancel(execution_id)

    # Not async: the schedule's identity is issued through the central authority in a worker thread.
    @server.tool(description="Schedule a registered agent to run repeatedly: input, cron or interval.")
    def create_agent_schedule(
        agent: AgentName,
        input: str,  # noqa: A002 - the API's field name
        cron: Cron | None = None,
        interval_seconds: IntervalSeconds | None = None,
    ) -> Schedule:
        who = require_caller()
        token = presented_token()
        with refusals_as_tool_errors():
            request = ScheduleRequest(agent=agent, input=input, cron=cron, interval_seconds=interval_seconds)
            name = registry.get_agent(request.agent).name
            identity = issuer.issue(token, "create_schedule", name) if issuer and token else None
            return schedules.create(request, who, identity)

    @server.tool(description="List every schedule, deleted ones included, with the runs each has started.")
    async def list_agent_schedules() -> list[Schedule]:
        require_caller()
        return schedules.list_schedules()

    return server
