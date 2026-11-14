"""An agent's MCP client, with its sandbox token, on the tools gateway: client.py <agent> list | call <tool> <json>."""
import asyncio, json, sys
from pathlib import Path
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def main(agent, action, *rest):
    token = json.loads(Path("/demo/shared/tokens.json").read_text())["sandbox"].get(agent, "not-a-token")
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http, \
            Client(streamable_http_client("http://agentgateway:3000/mcp", http_client=http)) as client:
        if action == "list":
            tools = (await client.list_tools()).tools
            print(len(tools), "tools:", ", ".join(sorted(t.name for t in tools)))
            for t in tools:
                if t.name in rest:
                    print(json.dumps({"name": t.name, "annotations": t.annotations and t.annotations.model_dump(exclude_none=True),
                                      "arguments": sorted((t.input_schema or {}).get("properties", {}))}))
        else:
            result = await client.call_tool(rest[0], json.loads(rest[1]))
            text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
            print("isError:", result.is_error); print(text[:1800])

try:
    asyncio.run(main(*sys.argv[1:]))
except BaseException as error:  # the gateway's refusal reaches the client as an HTTP error
    def leaves(e):
        return [l for sub in e.exceptions for l in leaves(sub)] if isinstance(e, BaseExceptionGroup) else [e]
    print("FAILED:", "; ".join(f"{type(e).__name__}: {e}" for e in leaves(error))[:600])
