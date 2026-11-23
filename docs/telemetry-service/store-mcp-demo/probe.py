"""Requests that try to reach a store past IAM, as an agent that holds nothing: each must be refused."""
import json
from pathlib import Path

import httpx2

token = json.loads(Path("/demo/shared/tokens.json").read_text())["sandbox"]["datapipe-agent"]
headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
url = "http://agentgateway:3000/mcp"
QUERY: dict[str, str | int] = {"query": "* | fields _msg", "start": "2026-01-01T00:00:00Z", "limit": 1}


def call(name: str, arguments: dict[str, str | int]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


with httpx2.Client(timeout=30) as http:
    hello = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}}
    opened = http.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": hello})
    session = {**headers, "Mcp-Session-Id": opened.headers.get("mcp-session-id", "")}
    http.post(url, headers=session, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    probes = {
        "a store's tool under its target's prefix": ("POST", call("logs_query", QUERY)),
        "a tool call behind a listing, in one batch": (
            "POST",
            [{"jsonrpc": "2.0", "id": 3, "method": "tools/list"}, call("query", QUERY)],
        ),
        "a listing of the stores' resources": ("POST", {"jsonrpc": "2.0", "id": 4, "method": "resources/list"}),
        "a listing of the stores' prompts": ("POST", {"jsonrpc": "2.0", "id": 5, "method": "prompts/list"}),
        "a tool call sent with PUT": ("PUT", call("query", QUERY)),
    }
    for probe, (method, body) in probes.items():
        answer = http.request(method, url, headers=session, json=body)
        text = answer.text.replace("\n", " ")
        leaked = "_msg" in text  # what a store's answer to the query would hold
        print(f"{'LEAKED' if leaked else 'refused'}: {probe}: {answer.status_code} {text[:160]}")
