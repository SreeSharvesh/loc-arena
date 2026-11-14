"""datapipe-agent asks IAM for read on telemetry; telemetry-owner approves it. Each with its own IAM credential."""
import json, time, urllib.request
from pathlib import Path
tokens = json.loads(Path("/demo/shared/tokens.json").read_text())["iam"]
def call(who, path, body, key):
    request = urllib.request.Request("http://iam:8000" + path, json.dumps(body).encode(),
        {"content-type": "application/json", "authorization": f"Bearer {tokens[who]}", "idempotency-key": key})
    return json.load(urllib.request.urlopen(request))
filed = call("datapipe-agent", "/requests", {"resource": "telemetry", "permissions": ["read"], "reason": "debug the nightly load", "until": int(time.time()) + 3600}, "ask-1")
print("filed:", {k: filed[k] for k in filed if k in ("id", "request", "status", "approvers", "next")})
decided = call("telemetry-owner", f"/requests/{filed.get('id') or filed['request']}/decision", {"approve": True}, "decide-1")
print("decided:", {k: decided[k] for k in decided if k in ("status", "permissions", "until", "decided_by")})
