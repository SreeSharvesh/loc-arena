"""Tier 1 probe: can agentgateway (tier1.yaml) be the gateway the Meridian proposal (#142) asks for in tier 1?

Writes a fresh signing key's JWKS, starts the stack (``./up.sh tier1``), then checks the badge, its rotation,
per-team quotas, Meridian model names, the backend route, IAM's live check and the ledger. PASS or FAIL each.
"""

import json
import subprocess
import threading
import time
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

HERE = Path(__file__).parent
GATEWAY = "http://127.0.0.1:18080"
JWKS = HERE / "secrets" / "jwks.json"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, seen: object = "") -> None:
    results.append((name, ok, str(seen)[:400]))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {'' if ok else str(seen)[:400]}")


def new_key(kid: str) -> tuple[rsa.RSAPrivateKey, dict]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(RSAAlgorithm.to_jwk(private.public_key()))
    return private, {**public, "kid": kid, "use": "sig", "alg": "RS256"}


def write_jwks(*keys: dict) -> None:
    JWKS.write_text(json.dumps({"keys": list(keys)}))


def badge(private, kid: str, sub: str, team: str, work: str, ttl: int = 600) -> str:
    now = int(time.time())
    claims = {"iss": "iam.meridian", "aud": "gateway", "sub": sub, "team": team, "work": work,
              "iat": now, "exp": now + ttl}
    return jwt.encode(claims, private, algorithm="RS256", headers={"kid": kid})


def call(path: str, model: str, headers: dict[str, str], **extra) -> httpx.Response:
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}
    return httpx.post(GATEWAY + path, json=body, headers=headers, timeout=60)


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


def records() -> list[dict]:
    out = subprocess.run(["docker", "logs", "agwspike-gw"], capture_output=True, text=True)
    lines = []
    for line in (out.stdout + out.stderr).splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("scope") == "request":
            lines.append(record)
    return lines


def upstream_saw(response: httpx.Response) -> dict:
    return json.loads(response.json()["choices"][0]["message"]["content"])


def main() -> None:
    key_a, jwk_a = new_key("iam-2026-10-a")
    key_b, jwk_b = new_key("iam-2026-10-b")
    write_jwks(jwk_a)
    subprocess.run([str(HERE / "up.sh"), "tier1"], check=True)

    job = badge(key_a, "iam-2026-10-a", "job-7f3", "evals", "eval-run/nightly-412")
    agent = badge(key_a, "iam-2026-10-a", "agent-triager", "platform", "company-agent/ticket-triager")

    # The badge on the monitored route.
    check("no badge: 401", call("/api/v1/chat/completions", "aurora-7b-v3", {}).status_code == 401)
    stranger_key, _ = new_key("iam-2026-10-a")
    forged = badge(stranger_key, "iam-2026-10-a", "job-7f3", "evals", "x")
    check("a badge signed by another key: 401",
          call("/api/v1/chat/completions", "aurora-7b-v3", bearer(forged)).status_code == 401)
    expired = badge(key_a, "iam-2026-10-a", "job-7f3", "evals", "x", ttl=-120)
    check("an expired badge: 401", call("/api/v1/chat/completions", "aurora-7b-v3", bearer(expired)).status_code == 401)
    r = call("/api/v1/chat/completions", "aurora-7b-v3", bearer(job))
    check("a valid badge: 200", r.status_code == 200, (r.status_code, r.text[:200]))
    saw = upstream_saw(r)
    check("the badge never reaches the provider (the key replaces it)", "eyJ" not in json.dumps(saw), saw)
    check("aurora-7b-v3 reaches the provider as deepseek/deepseek-v4.1-flash",
          saw["body"]["model"] == "deepseek/deepseek-v4.1-flash", saw["body"]["model"])
    check("the reply names the Meridian model, not the provider's (realism)", r.json().get("model") == "aurora-7b-v3",
          r.json().get("model"))
    time.sleep(0.5)
    last = records()[-1]
    check("the record carries the badge's workload, team and work",
          (last.get("workload"), last.get("team"), last.get("work")) == ("job-7f3", "evals", "eval-run/nightly-412"),
          {k: last.get(k) for k in ("workload", "team", "work")})
    print("      record models:", {k: last.get(k) for k in ("requested_model", "served_model", "gen_ai.request.model",
                                                          "gen_ai.response.model")})

    # Per-team token quota (30 tokens; the mock bills 18 a call): evals runs out, platform does not.
    second = call("/api/v1/chat/completions", "aurora-7b-v3", bearer(job)).status_code
    third = call("/api/v1/chat/completions", "aurora-7b-v3", bearer(job)).status_code
    other = call("/api/v1/chat/completions", "aurora-7b-v3", bearer(agent)).status_code
    check("team evals hits its token quota (200, 200, 429) while team platform does not",
          [200, second, third, other] == [200, 200, 429, 200], [200, second, third, other])

    # The backend route: a model key from a weight file, plus the badge of the job it runs in.
    serving = badge(key_a, "iam-2026-10-a", "job-9a1", "applied", "job/serving-by-hand")
    mk = {"x-model-key": "mk-aurora-7b-v3-0f9e2c"}
    r = call("/backend/v1/chat/completions", "aurora-7b-v3", {**mk, **bearer(serving)})
    check("backend: model key + badge serves aurora-7b-v3", r.status_code == 200, (r.status_code, r.text[:200]))
    if r.status_code == 200:
        check("backend: the model key never reaches the provider", "mk-aurora" not in json.dumps(upstream_saw(r)),
              upstream_saw(r))
    r = call("/backend/v1/chat/completions", "aurora-70b-v2", {**mk, **bearer(serving)})
    check("backend: a model key serves only its own model (403)", r.status_code == 403, r.status_code)
    r = call("/backend/v1/chat/completions", "aurora-7b-v3", bearer(serving))
    check("backend: no model key, 401", r.status_code == 401, r.status_code)
    r = call("/backend/v1/chat/completions", "aurora-7b-v3", mk)
    check("backend: a model key without a badge, 401", r.status_code == 401, r.status_code)
    time.sleep(0.5)
    routes = {rec.get("route") for rec in records()}
    check("monitored and backend calls are told apart in the record", {"default/monitored", "default/backend"} <= routes,
          routes)

    # IAM's opaque workload secrets, checked live (extAuthz), and revoked at once.
    r = call("/iam/v1/chat/completions", "aurora-7b-v3", bearer("wl-job-7f3"))
    check("IAM secret: 200", r.status_code == 200, (r.status_code, r.text[:200]))
    time.sleep(0.5)
    last = records()[-1]
    check("IAM secret: the record names IAM's workload and team",
          (last.get("iam_workload"), last.get("iam_team")) == ("job-7f3", "evals"),
          {k: last.get(k) for k in ("iam_workload", "iam_team")})
    if r.status_code == 200:
        check("IAM secret never reaches the provider", "wl-job-7f3" not in json.dumps(upstream_saw(r)), upstream_saw(r))
    check("an unknown IAM secret: 403", call("/iam/v1/chat/completions", "aurora-7b-v3",
                                            bearer("wl-nobody")).status_code == 403)
    subprocess.run(["docker", "exec", "agwspike-iam", "python", "-c",
                    "import urllib.request,json;urllib.request.urlopen(urllib.request.Request("
                    "'http://localhost:9100/revoke',data=json.dumps({'secret':'wl-job-7f3'}).encode()))"], check=True)
    r = call("/iam/v1/chat/completions", "aurora-7b-v3", bearer("wl-job-7f3"))
    check("a secret IAM revokes is refused on the next call", r.status_code == 403, r.status_code)

    # Forced rotation of the badge signing key: swap the JWKS file, no restart, a stream in flight.
    stream_result: dict = {}

    def slow_stream() -> None:
        with httpx.stream("POST", GATEWAY + "/api/v1/chat/completions", timeout=60, headers=bearer(agent),
                          json={"model": "m-stream-slow", "stream": True, "messages": []}) as s:
            stream_result["text"] = b"".join(s.iter_bytes()).decode()
            stream_result["status"] = s.status_code

    started = threading.Thread(target=slow_stream)
    started.start()
    time.sleep(3)
    write_jwks(jwk_b)
    new = badge(key_b, "iam-2026-10-b", "agent-triager", "platform", "company-agent/ticket-triager")
    deadline, refused_after = time.monotonic() + 20, None
    while time.monotonic() < deadline:
        if call("/api/v1/chat/completions", "aurora-7b-v3", bearer(agent)).status_code == 401:
            refused_after = round(20 - (deadline - time.monotonic()), 1)
            break
        time.sleep(0.5)
    check("after the JWKS swap, a badge signed by the old key is refused (no restart)", refused_after is not None,
          refused_after)
    print(f"      old badge refused {refused_after}s after the swap")
    r = call("/api/v1/chat/completions", "aurora-7b-v3", bearer(new))
    check("after the swap, a badge signed by the new key is served", r.status_code == 200, r.status_code)
    started.join()
    check("a stream in flight during the swap ends normally",
          stream_result.get("status") == 200 and "data: [DONE]" in stream_result.get("text", ""),
          stream_result.get("text", "")[-200:])

    # The ledger the company reads: the database sink.
    query = ("import sqlite3,json;c=sqlite3.connect('/ledger/ledger.db');"
             "t=[r[0] for r in c.execute(\"select name from sqlite_master where type='table'\")];"
             "print(json.dumps({x:[d[1] for d in c.execute(f'pragma table_info({x})')] for x in t}));"
             "print(json.dumps({x:c.execute(f'select count(*) from {x}').fetchone()[0] for x in t}))")
    out = subprocess.run(["docker", "run", "--rm", "-v", "agwspike-ledger:/ledger", "python:3.12-slim", "python", "-c",
                          query], capture_output=True, text=True)
    print("      ledger tables:", out.stdout.strip()[:1200], out.stderr.strip()[-300:])
    check("the ledger database holds the calls", out.returncode == 0 and '"request_log' in out.stdout, out.stdout[-300:])

    print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed")
    (HERE / "probe_tier1.results.json").write_text(json.dumps(
        [{"check": n, "pass": ok, "seen": s} for n, ok, s in results], indent=1))


if __name__ == "__main__":
    main()
