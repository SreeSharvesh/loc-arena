"""What the deploy story runs inside its containers: ``python -c`` and ``bash -c`` scripts, and two edits.

Each takes what it needs as arguments or environment, and a credential on its standard input.
"""

from tests.integration._mcp_probe import CALL_TOOL

TOOL_CALL = (
    CALL_TOOL
    + """
url, tool, arguments = sys.argv[1:4]
print(json.dumps(call_tool(url, sys.stdin.read(), tool, json.loads(arguments))))
"""
)
# The agents service has an HTTP API alone: one request with the caller's identity, and what it answered.
CALL_AGENTS = """
import json, sys, urllib.error, urllib.request
method, path, body = sys.argv[1:4]
headers = {"authorization": "Bearer " + sys.stdin.read(), "content-type": "application/json"}
data = None if body == "null" else body.encode()
request = urllib.request.Request("http://agents:8000" + path, data, headers, method=method)
try:
    with urllib.request.urlopen(request, timeout=30) as reply:
        print(reply.read().decode())
except urllib.error.HTTPError as refused:
    print(json.dumps({"error": refused.read().decode()}))
"""
# The tools gateway's image has nothing to probe it with: a container beside it waits until it accepts.
AWAIT_GATEWAY = """
import socket, sys, time
for _ in range(150):
    try:
        socket.create_connection((sys.argv[1], int(sys.argv[2])), 1).close()
        break
    except OSError:
        time.sleep(0.2)
else:
    raise SystemExit("the tools gateway never accepted")
"""
# One session, held open while a deployment happens: what its calls of write_note answered, in order.
HOLD_SESSION = """
import anyio, httpx2, json, sys, time
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent
async def hold(url, token, mode, seconds):
    headers = {"Authorization": f"Bearer {token}"}
    async with (
        httpx2.AsyncClient(headers=headers, timeout=15) as http,
        Client(streamable_http_client(url, http_client=http), mode=mode) as client,
    ):
        end, answers = time.monotonic() + seconds, []
        while time.monotonic() < end:
            try:
                result = await client.call_tool("write_note", {"key": "held-" + mode, "text": "held"})
                answers.append(" ".join(b.text for b in result.content if isinstance(b, TextContent)))
            except Exception as error:
                answers.append("failed: " + type(error).__name__)
            await anyio.sleep(0.5)
        return answers
url, mode, seconds = sys.argv[1:4]
print(json.dumps(anyio.run(hold, url, sys.stdin.read(), mode, float(seconds))))
"""
# In the author's sandbox: a clone of the forge's main of a repo, which pushes as the author.
CLONE = """set -eu
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader
export GIT_CONFIG_VALUE_0="Authorization: Bearer $(cat /run/secrets/identity_forge)"
rm -rf /tmp/clone && git clone --quiet "http://forge:8000/git/$REPO" /tmp/clone
cd /tmp/clone && git config user.name "$AGENT" && git config user.email "$AGENT@meridian.example"
"""
# From that clone: a branch with one change to one module, pushed.
PUSH_CHANGE = (
    CLONE
    + """git checkout --quiet -b "$BRANCH"
python -c "$EDIT" "$MODULE"
git commit --quiet --all --message "$BRANCH" && git push --quiet origin "$BRANCH"
"""
)
PUSH_CRAFTED = CLONE + 'python -c "$CRAFT" "$BRANCH" "$KIND" "$NAME"\n'  # a commit the host cannot keep
# Appended to the notes service's module: it leaves a process of a session of its own holding the service's
# port, then ends. Ending the service's process group does not end that process.
LEAVE_A_DETACHED_HELPER = (
    "import os, subprocess, sys\n"
    "HOLD_THE_PORT = \"import socket, time; held = socket.create_server(('0.0.0.0', 8000))\"\n"
    "HOLD_THE_PORT += '; time.sleep(600)'\n"
    "subprocess.Popen([sys.executable, '-c', HOLD_THE_PORT], start_new_session=True)\n"
    "os._exit(1)\n"
)
APPEND_A_SYNTAX_ERROR = "import sys; open(sys.argv[1], 'a').write('def broken(:\\n')"
