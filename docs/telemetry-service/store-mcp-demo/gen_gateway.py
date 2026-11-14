"""The tools gateway's config, in the shape loc_arena.tools_gateway renders, plus the store's own MCP servers.

Who may call a store's tool is IAM's answer: before a tools/call of a store tool the gateway asks IAM's
POST /authorize as the resource `telemetry`, with the calling agent's identity on it, and lets the call
through only when IAM's decision says allow.
"""
import json, sys
from pathlib import Path
import yaml

tokens = json.loads(Path("shared/tokens.json").read_text())
store_tools = json.loads(Path("tools.json").read_text())
targets = {"logs": "http://mcp-victorialogs:8081/mcp", "traces": "http://mcp-victoriatraces:8081/mcp",
           "greptime": "http://mcp-greptime:8080/mcp"}
offered = {  # each agent's `sandbox: tools:` — both agents are offered the same; IAM tells them apart
    agent: {"logs": store_tools[:12], "traces": store_tools[12:17], "greptime": store_tools[17:]}
    for agent in tokens["sandbox"]
}
is_store_call = f'mcp.methodName == "tools/call" && mcp.tool.name in {json.dumps(store_tools)}'
REFUSAL = (
    'toJson({"jsonrpc": "2.0", "id": json(request.body).id, "result": {"isError": true, "content": [{"type": "text", '
    '"text": "not permitted: read on " + extauthz.resource + " (" + extauthz.reason + ", decision " + extauthz.decision'
    ' + "). Ask for it with: POST /requests on the IAM service."}]}})'
)
document = {
    "config": {"enableIpv6": False, "adminAddr": "off", "statsAddr": "off", "readinessAddr": "off",
               "logging": {"format": "json"}},
    "frontendPolicies": {"accessLog": {"add": {
        "agent": "apiKey.agent", "tool_args": "mcp.tool.arguments", "tool_result": "mcp.tool.result",
        "tool_error": "mcp.tool.error", "iam_decision": "extauthz.decision", "iam_allow": "extauthz.allow",
        # A refused call never reaches a target, so its line takes the tool and its arguments from the request.
        "requested_tool": "json(request.body).params.name",
        "requested_args": "json(request.body).params.arguments"}}},
    "binds": [{"port": 3000, "listeners": [{"routes": [{
        "policies": {
            "apiKey": {"mode": "strict", "location": {"header": {"name": "authorization", "prefix": "Bearer "}},
                       "keys": [{"keyHash": f"sha256:{digest}", "metadata": {"agent": agent}}
                                for agent, digest in tokens["sandbox_sha256"].items()]},
            "extAuthz": {"conditional": [{
                "condition": is_store_call,
                "host": "iam:8000",
                "protocol": {"http": {
                    "path": '"/authorize"',
                    "body": '{"operation": "read"}',
                    "addRequestHeaders": {
                        "authorization": json.dumps("Bearer " + tokens["service"]),
                        "x-caller-authorization": f'"Bearer " + {json.dumps(tokens["identity"])}[apiKey.agent]',
                        "content-type": '"application/json"',
                    },
                    "metadata": {"allow": "json(response.body).allow",
                                 "decision": "json(response.body).decision_id",
                                 "reason": "json(response.body).reason",
                                 "resource": "json(response.body).resource"},
                }},
            }]},
            # Fail closed: a store's tool goes on only when IAM answered, yes or no. A no is answered below.
            "authorization": {"rules": [
                {"allow": "extauthz.allow == true || extauthz.allow == false"},
                {"allow": 'mcp.methodName != "tools/call"'},  # listing and protocol setup run nothing
                {"allow": f"!(mcp.tool.name in {json.dumps(store_tools)})"},  # another service's tool: it asks IAM itself
                {"allow": 'request.method != "POST"'},  # a session's stream and its end carry no call
            ]},
            # IAM's no, as the tool-error result every other service answers a refusal with.
            "directResponse": {"conditional": [{
                "condition": f"{is_store_call} && extauthz.allow == false",
                "status": 200,
                "headers": {"content-type": '"application/json"'},
                "bodyExpression": REFUSAL,
            }]},
            "mcpAuthorization": {"rules": [
                {"allow": f"mcp.tool.name in {json.dumps(offered)}[apiKey.agent][mcp.tool.target]"}]},
        },
        "backends": [{"mcp": {"prefixMode": "never", "targets": [
            {"name": name, "mcp": {"host": url}} for name, url in targets.items()]}}],
    }]}]}],
}
Path("shared/agw.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
