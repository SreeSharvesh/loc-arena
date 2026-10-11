"""Spike only: from a sandbox, call a tool its agent is not granted through the tools gateway; print the reply."""

import json
import sys
import urllib.error
import urllib.request

URL = "http://agentgateway:3000/mcp"
KEY = open("/run/secrets/sandbox_token").read().strip()
session = None


def send(message):
    global session
    headers = {"authorization": f"Bearer {KEY}", "content-type": "application/json",
               "accept": "application/json, text/event-stream"}
    if session:
        headers["mcp-session-id"] = session
    request = urllib.request.Request(URL, json.dumps(message).encode(), headers)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            session = response.headers.get("mcp-session-id") or session
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


client = {"name": "spike", "version": "0"}
send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": client}})
send({"jsonrpc": "2.0", "method": "notifications/initialized"})
print(send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": sys.argv[1], "arguments": json.loads(sys.argv[2])}}))
