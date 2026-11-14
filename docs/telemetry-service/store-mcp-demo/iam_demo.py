"""Central IAM, real, for the demo: one resource `telemetry` whose tools all need `read`; serves the IAM API."""
import os
import hashlib, json, secrets, sys, time
from pathlib import Path

import uvicorn
from meridian_iam.contracts import load_manifest
from meridian_iam.core import Iam
from meridian_iam.service import create_app

TOOLS = json.loads(Path("/demo/tools.json").read_text())  # every tool the three MCP servers offer
manifest = {
    "schema_version": 1,
    "resource": "telemetry",
    "operations": {"read": {"permission": "read", "requestable": True}},
    "tools": {tool: "read" for tool in TOOLS},
}
audit = open("/shared/iam.audit.jsonl", "a")
iam = Iam("/tmp/iam.db", lambda: int(time.time()), audit_sink=lambda e: (audit.write(json.dumps(e) + "\n"), audit.flush()))
for agent in ("controlplane-agent", "datapipe-agent", "telemetry-owner"):
    iam.add_principal(agent, "agent")
iam.add_member("telemetry-owner", "research-platform", "approver", {"telemetry": ["read"]})
iam.register_resource(load_manifest(manifest), owner_team="research-platform", approver_role="approver",
                      max_seconds=86400, allow_ongoing=True)
iam.add_principal("telemetry-service", "service")
iam.bind_service("telemetry-service", "telemetry", "telemetry")
iam.add_baseline_grant("controlplane-agent", "telemetry", ["read"])  # datapipe-agent holds nothing
sandbox = {a: secrets.token_urlsafe(24) for a in ("controlplane-agent", "datapipe-agent")}
out = {
    "service": iam.issue_credential("telemetry-service", ["iam"]),  # the gateway decides for `telemetry` with it
    "identity": {a: iam.issue_credential(a, ["telemetry", "iam:telemetry"]) for a in sandbox},
    "iam": {a: iam.issue_credential(a, ["iam"]) for a in (*sandbox, "telemetry-owner")},
    "sandbox": sandbox,
    "sandbox_sha256": {a: hashlib.sha256(t.encode()).hexdigest() for a, t in sandbox.items()},
}
Path("/shared/tokens.json.new").write_text(json.dumps(out))
os.replace("/shared/tokens.json.new", "/shared/tokens.json")  # whole, or not there
uvicorn.run(create_app(iam), host="0.0.0.0", port=8000, log_level="warning")
