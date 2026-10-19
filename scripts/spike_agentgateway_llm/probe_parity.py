"""Parity probe: does agentgateway (parity.yaml) do what loc_arena/gateway/proxy.py and its tests require?

Run after ``./up.sh parity``. Each check prints PASS or FAIL with what it saw; the access log is read from the
gateway container's stdout (``docker logs``), which is where agentgateway writes it.
"""

import base64
import hashlib
import json
import subprocess
import time

import httpx

GATEWAY = "http://127.0.0.1:18080"
KEY = "sk-or-SPIKEFAKEKEY"
SANDBOX, RUNNER, STRANGER = "tok-agent-main", "tok-runner-1", "tok-nobody"
RUNNER_MODEL = "deepseek/deepseek-v4.1-flash"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, seen: object = "") -> None:
    results.append((name, ok, str(seen)[:300]))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {'' if ok else seen}")


def log_lines() -> list[dict]:
    out = subprocess.run(["docker", "logs", "agwspike-gw"], capture_output=True, text=True).stdout
    out += subprocess.run(["docker", "logs", "agwspike-gw"], capture_output=True, text=True).stderr
    lines = []
    for line in out.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            lines.append({"unparsed": line})
            continue
        if record.get("scope") == "request":
            lines.append(record)
    return lines


def last_log(path: str, wait: float = 3.0) -> dict | None:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        matching = [line for line in log_lines() if line.get("http.path") == path]
        if matching:
            return matching[-1]
        time.sleep(0.2)
    return None


def chat(token: str | None, body: dict, path: str = "/api/v1/chat/completions", **kw) -> httpx.Response:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return httpx.post(GATEWAY + path, json=body, headers=headers, timeout=60, **kw)


def echo(response: httpx.Response) -> dict:
    return json.loads(response.json()["choices"][0]["message"]["content"])


def main() -> None:
    body = {"model": "deepseek/deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}],
            "provider": {"order": ["deepinfra"]}, "reasoning": {"effort": "low"}}

    r = chat(SANDBOX, body)
    seen = echo(r)
    check("upstream gets the gateway key in place of the caller's",
          seen["authorization_sha256"] == hashlib.sha256(f"Bearer {KEY}".encode()).hexdigest(), seen)
    check("OpenRouter fields (provider, reasoning) pass through unchanged",
          seen["body"].get("provider") == body["provider"] and seen["body"].get("reasoning") == body["reasoning"], seen)
    rec = last_log("/api/v1/chat/completions")
    check("a call is recorded with caller, request and reply",
          rec is not None and rec.get("caller") == "agent-main" and '"hi"' in rec.get("request_body", "")
          and "chatcmpl-mock" in rec.get("response_body", ""), rec)

    r = httpx.post(GATEWAY + "/api/v1/completions", json={"model": "m", "prompt": "x"},
                   headers={"authorization": f"Bearer {SANDBOX}"}, timeout=30)
    check("legacy /api/v1/completions passes through", r.status_code == 200 and "/api/v1/completions" in r.text,
          (r.status_code, r.text[:200]))
    r = httpx.get(GATEWAY + "/api/v1/models", headers={"authorization": f"Bearer {SANDBOX}"})
    check("/api/v1/models passes through", r.status_code == 200 and "mock/model" in r.text, (r.status_code, r.text[:200]))

    r = chat(SANDBOX, body, path="/api/v1/embeddings")
    rec = last_log("/api/v1/embeddings")
    check("a path outside the allowlist is refused", r.status_code in (403, 404), r.status_code)
    check("a refused path is recorded", rec is not None, rec)
    for page in ("/docs", "/ui", "/config_dump"):
        r = httpx.get(GATEWAY + page)
        check(f"no framework page at {page}", r.status_code in (403, 404), (r.status_code, r.text[:100]))

    r = chat(None, body)
    rec = last_log("/api/v1/chat/completions")
    check("a call without a token is refused (401)", r.status_code == 401, r.status_code)
    check("a refused (no token) call is recorded", rec is not None and rec.get("http.status") == 401, rec)
    r = chat(STRANGER, body)
    check("an unknown token is refused (401)", r.status_code == 401, r.status_code)

    r = httpx.post(GATEWAY + "/api/v1/chat/completions", json=body, timeout=30,
                   headers={"authorization": f"Bearer {SANDBOX}", "host": "evil.example"})
    check("a forged Host header cannot redirect the call", r.status_code == 200 and "chatcmpl-mock" in r.text,
          r.status_code)

    streamed = {**body, "stream": True}
    with httpx.stream("POST", GATEWAY + "/api/v1/chat/completions", json=streamed, timeout=60,
                      headers={"authorization": f"Bearer {SANDBOX}"}) as r:
        raw = b"".join(r.iter_bytes())
    text = raw.decode()
    check("a streamed reply reaches the caller, ending in [DONE]", r.status_code == 200 and "data: [DONE]" in text,
          text[-200:])
    time.sleep(0.5)
    rec = last_log("/api/v1/chat/completions")
    check("the whole SSE stream is recorded", rec is not None and rec.get("response_body", "").count("data:") ==
          text.count("data:"), (rec or {}).get("response_body", "")[-200:])
    check("stream_options.include_usage is added by the gateway (side effect)",
          '"include_usage":true' in (rec or {}).get("request_body", "").replace(" ", "")
          or "include_usage" in text, (rec or {}).get("request_body", "")[:300])

    big = 200_000
    r = chat(SANDBOX, {**body, "big": big})
    time.sleep(0.5)
    rec = last_log("/api/v1/chat/completions")
    got = len((rec or {}).get("response_body", ""))
    check(f"a {big // 1000} KB reply is recorded intact in one log line", got >= big, got)

    # A caller hanging up mid-stream.
    with httpx.stream("POST", GATEWAY + "/api/v1/chat/completions", timeout=60,
                      json={**body, "model": "m-stream-slow", "stream": True},
                      headers={"authorization": f"Bearer {SANDBOX}"}) as r:
        for i, _ in enumerate(r.iter_bytes()):
            if i >= 1:
                break
    time.sleep(2)
    rec = last_log("/api/v1/chat/completions")
    check("a caller hanging up mid-stream is recorded", rec is not None and "m-stream-slow" in rec.get("request_body", ""),
          rec)
    if rec:
        flags = {k: v for k, v in rec.items() if k not in ("request_body", "response_body")}
        print("      hang-up record:", json.dumps(flags)[:600])

    r = chat(RUNNER, {**body, "model": "openai/gpt-5"}, path="/runner/api/v1/chat/completions")
    check("a runner is refused a model runners may not call (403)", r.status_code == 403, (r.status_code, r.text[:200]))
    rec = last_log("/runner/api/v1/chat/completions")
    check("the runner refusal is recorded", rec is not None and rec.get("http.status") == 403, rec)
    r = chat(SANDBOX, body, path="/runner/api/v1/chat/completions")
    check("a sandbox token is refused on the runner route", r.status_code == 403, r.status_code)
    statuses = [chat(RUNNER, {**body, "model": RUNNER_MODEL}, path="/runner/api/v1/chat/completions").status_code
                for _ in range(4)]
    check("runners get 3 calls a play, then 429", statuses == [200, 200, 200, 429], statuses)
    r = chat(RUNNER, body)
    check("a runner token is refused on the sandbox route", r.status_code == 403, r.status_code)

    subprocess.run(["docker", "stop", "agwspike-mock"], capture_output=True)
    r = chat(SANDBOX, {**body, "model": "upstream-down-probe"})
    rec = last_log("/api/v1/chat/completions")
    check("an unreachable upstream is a 5xx to the caller", 500 <= r.status_code < 600, (r.status_code, r.text[:200]))
    check("an unreachable upstream is recorded", rec is not None and "upstream-down-probe" in rec.get("request_body", "")
          and rec.get("http.status", 0) >= 500, rec)
    subprocess.run(["docker", "start", "agwspike-mock"], capture_output=True)

    logs = subprocess.run(["docker", "logs", "agwspike-gw"], capture_output=True, text=True)
    everything = logs.stdout + logs.stderr
    encoded = [KEY, base64.b64encode(KEY.encode()).decode()[:16]]
    check("the log never holds the key", not any(e in everything for e in encoded))

    print(f"\n{sum(ok for _, ok, _ in results)}/{len(results)} passed")
    with open("probe_parity.results.json", "w") as f:
        json.dump([{"check": n, "pass": ok, "seen": s} for n, ok, s in results], f, indent=1)


if __name__ == "__main__":
    main()
