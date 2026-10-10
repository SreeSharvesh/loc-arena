"""Each agent's MCP client: the tools it is offered come from its server's listing."""

from loc_arena.scaffold.mcp_client import McpTools, over_http
from mcp import Client
from mcp.server import MCPServer

UNREACHABLE = "http://127.0.0.1:9/mcp"  # the discard port: nothing listens there
TIMEOUT_SECONDS = 5.0


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
