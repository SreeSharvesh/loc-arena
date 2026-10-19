"""Live probe: six small OpenRouter calls through agentgateway (live.yaml), each capped at 20 output tokens."""

import json
import subprocess
import time

import httpx

GATEWAY = "http://127.0.0.1:18080/api/v1"
H = {"authorization": "Bearer tok-agent-main"}
MODEL = "deepseek/deepseek-v4.1-flash"
SAY = [{"role": "user", "content": "Reply with the single word: ok"}]


def show(name: str, r: httpx.Response, extract) -> None:
    try:
        detail = extract(r)
    except Exception as error:  # a spike: print what came back
        detail = f"{type(error).__name__}: {r.text[:300]}"
    print(f"{name}: {r.status_code} {detail}")


def main() -> None:
    r = httpx.post(f"{GATEWAY}/chat/completions", headers=H, timeout=90, json={
        "model": MODEL, "messages": SAY, "max_tokens": 20,
        "provider": {"sort": "price"}, "reasoning": {"enabled": False}, "usage": {"include": True}})
    show("chat (provider, reasoning, usage fields)", r, lambda r: (r.json()["model"], r.json().get("provider"),
         r.json()["choices"][0]["message"]["content"], r.json()["usage"].get("cost")))

    text = ""
    with httpx.stream("POST", f"{GATEWAY}/chat/completions", headers=H, timeout=90,
                      json={"model": MODEL, "messages": SAY, "max_tokens": 20, "stream": True}) as s:
        for chunk in s.iter_text():
            text += chunk
    print(f"chat stream: {s.status_code} chunks={text.count('data:')} done={'[DONE]' in text}")

    r = httpx.post(f"{GATEWAY}/chat/completions", headers=H, timeout=90,
                   json={"model": "aurora-7b-v3", "messages": SAY, "max_tokens": 20})
    show("alias aurora-7b-v3", r, lambda r: ("reply model:", r.json()["model"]))

    r = httpx.get(f"{GATEWAY}/models", headers=H, timeout=30)
    show("models", r, lambda r: [m["id"] for m in r.json()["data"]])

    r = httpx.post(f"{GATEWAY}/messages", timeout=90,
                   headers={**H, "anthropic-version": "2023-06-01"},
                   json={"model": MODEL, "max_tokens": 20, "messages": SAY})
    show("messages (Claude Code's API)", r, lambda r: (r.json().get("type"), r.json().get("model"),
         [c.get("text") for c in r.json().get("content", [])]))

    text = ""
    with httpx.stream("POST", f"{GATEWAY}/messages", timeout=90, headers={**H, "anthropic-version": "2023-06-01"},
                      json={"model": MODEL, "max_tokens": 20, "messages": SAY, "stream": True}) as s:
        for chunk in s.iter_text():
            text += chunk
    print(f"messages stream: {s.status_code} events={text.count('event:')} message_stop={'message_stop' in text}")

    time.sleep(1)
    out = subprocess.run(["docker", "logs", "agwspike-gw"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if '"scope":"request"' in line:
            rec = json.loads(line)
            print("  record:", {k: rec.get(k) for k in ("http.path", "http.status", "caller", "requested_model",
                                                       "served_model", "cost", "gen_ai.usage.input_tokens")},
                  "| body logged:", bool(rec.get("request_body")), bool(rec.get("response_body")))


if __name__ == "__main__":
    main()
