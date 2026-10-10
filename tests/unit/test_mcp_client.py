"""Each agent's MCP client: the tools it is offered come from its server's listing."""

from typing import Any

from loc_arena.scaffold.mcp_client import McpClients, McpServices, McpTools, over_http
from loc_arena.scaffold.tools import ToolResult
from mcp import Client
from mcp.server import MCPServer

UNREACHABLE = "http://127.0.0.1:9/mcp"  # the discard port: nothing listens there
TIMEOUT_SECONDS = 5.0


class _OtherServices:
    """The services an MCP tool would otherwise fall through to: they record what they are asked to run."""

    def __init__(self) -> None:
        self.ran: list[str] = []

    def run(self, tool: str, args: dict[str, Any]) -> ToolResult:
        self.ran.append(tool)
        return {"ran": tool}


def _server() -> MCPServer:
    server = MCPServer("echo", log_level="WARNING")

    @server.tool()
    def echo(text: str) -> str:
        """Say text back."""
        return text

    return server


def test_an_agent_is_offered_exactly_the_tools_its_server_lists() -> None:
    server = _server()

    specs = McpTools(lambda: Client(server)).specs()

    assert specs == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Say text back.",
                "parameters": {
                    "type": "object",
                    "properties": {"text": {"title": "Text", "type": "string"}},
                    "required": ["text"],
                    "title": "echoArguments",
                },
            },
        },
    ]


def test_a_call_to_an_unreachable_server_is_an_error_result() -> None:
    tools = McpTools(lambda: over_http(UNREACHABLE, "a-key", TIMEOUT_SECONDS))

    result = tools.call("echo", {"text": "hello"})

    assert result == {"error": "echo could not be called: the tool server is unreachable", "tool": "echo"}


def test_a_served_tool_the_caller_is_not_offered_is_refused_and_never_run_elsewhere() -> None:
    other = _OtherServices()
    unoffered = McpClients([McpTools(lambda: Client(_server()), granted=())])
    services = McpServices({"alpha": unoffered}, other, served={"echo"})

    result = services.run("echo", {"text": "hello", "actor_uid": "alpha"})

    assert result == {"error": "echo is not offered to you", "tool": "echo"}
    assert other.ran == []
