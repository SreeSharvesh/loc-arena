"""OTLP probe: does the Collector's file (collector.yaml) hold the same record as agentgateway's stdout?

Run after ``./up.sh parity_otel``. Runs the parity probe, then compares the two records call by call, and tries
the ways a record could be lost: the Collector down during a call, a reply larger than gRPC's default 4 MiB
message, and stopping the gateway, which is safe only after its records are drained to the Collector.
"""

import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

HERE = Path(__file__).parent
GATEWAY = "http://127.0.0.1:18080/api/v1/chat/completions"
SANDBOX = {"authorization": "Bearer tok-agent-main"}
KEY = "sk-or-SPIKEFAKEKEY"
results: list[tuple[str, bool]] = []


def check(name: str, ok: bool, seen: object = "") -> None:
    results.append((name, ok))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {'' if ok else str(seen)[:300]}")


def value(v: dict) -> object:
    return next(iter(v.values())) if v else None


def otel_records() -> list[dict]:
    raw = subprocess.run(["docker", "run", "--rm", "-v", "agwspike-records:/records", "alpine:3.20", "cat",
                          "/records/calls.jsonl"], capture_output=True, text=True).stdout
    records = []
    for line in raw.splitlines():
        for resource in json.loads(line)["resourceLogs"]:
            for scope in resource["scopeLogs"]:
                for record in scope["logRecords"]:
                    records.append({a["key"]: value(a["value"]) for a in record["attributes"]})
    return records


def stdout_records() -> list[dict]:
    logs = subprocess.run(["docker", "logs", "agwspike-gw"], capture_output=True, text=True)
    return [json.loads(line) for line in (logs.stdout + logs.stderr).splitlines() if '"scope":"request"' in line]


def call(model: str, **extra) -> int:
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], **extra}
    try:
        return httpx.post(GATEWAY, json=body, headers=SANDBOX, timeout=60).status_code
    except httpx.HTTPError as error:
        return -1 if "ReadError" not in type(error).__name__ else -2


def otel_has(model: str, wait: float = 8.0) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if any(f'"{model}"' in str(r.get("request_body", "")) for r in otel_records()):
            return True
        time.sleep(0.5)
    return False


def main() -> None:
    subprocess.run([sys.executable, str(HERE / "probe_parity.py")], check=True, capture_output=True)
    time.sleep(2)
    stdout, otel = stdout_records(), otel_records()
    by_stdout = Counter((r["http.path"], r["http.status"]) for r in stdout)
    by_otel = Counter((r.get("url.path"), int(r.get("http.response.status_code", 0))) for r in otel)
    check(f"every stdout record is in the OTLP file ({len(otel)} of {len(stdout)})", by_stdout == by_otel,
          {"only stdout": by_stdout - by_otel, "only otel": by_otel - by_stdout})
    statuses = sorted({s for _, s in by_otel})
    check("refused and failed calls are there (401, 403, 404, 429, 503)", {401, 403, 404, 429, 503} <= set(statuses),
          statuses)
    longest = max(len(str(r.get("response_body", ""))) for r in otel)
    check("a 200 KB reply arrives whole", longest >= 200_000, longest)
    check("callers are named", any(r.get("caller") == "agent-main" for r in otel))
    check("the key is in no record", KEY not in json.dumps(otel))

    # A reply above gRPC's default 4 MiB message size.
    status = call("big-6mb", big=6_000_000)
    check("a 6 MB reply is recorded", status == 200 and otel_has("big-6mb"), status)

    # The Collector down during a call, then back.
    subprocess.run(["docker", "stop", "agwspike-otel"], capture_output=True)
    status = call("while-collector-down")
    time.sleep(1)
    subprocess.run(["docker", "start", "agwspike-otel"], capture_output=True)
    check("a call made while the Collector is down is recorded once it is back", otel_has("while-collector-down", 20),
          status)

    # Stopping: drain first. Wait (bounded) until the Collector holds every record stdout has, then stop.
    status = call("drain-then-stop")
    deadline = time.monotonic() + 10
    while len(otel_records()) < len(stdout_records()) and time.monotonic() < deadline:
        time.sleep(0.5)
    drained = len(otel_records()) == len(stdout_records())
    subprocess.run(["docker", "stop", "agwspike-gw"], capture_output=True)
    check("drained before the stop: the OTLP file matches stdout, the last call included",
          drained and otel_has("drain-then-stop"), (status, len(otel_records()), len(stdout_records())))

    print(f"\n{sum(ok for _, ok in results)}/{len(results)} passed")


if __name__ == "__main__":
    main()
