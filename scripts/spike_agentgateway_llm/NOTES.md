# Spike: agentgateway as the model gateway

**Question.** Can agentgateway v1.6.0 replace `loc_arena/gateway/proxy.py`, and does it give the tier 1 gateway that
the Meridian proposal (#142) asks for? We already run it as the tools gateway.

**Answer.** Yes to both, with five decisions listed at the end. Every row below comes from a check that ran:

| Probe | Against | Result |
| --- | --- | --- |
| `probe_parity.py` | the mock, binds mode (`parity.yaml`) | 27/27 |
| `probe_parity.py` | the mock, llm mode beside a plain route (`combined.yaml`) | 27/27 |
| `probe_tier1.py` | the mock (`tier1.yaml`) | 23/24; the miss is the model name in replies |
| `probe_live.py` | OpenRouter (`live.yaml`) | six calls |
| reload and budget runs | `llm_mode.yaml`, `combined.yaml`, `budget.yaml` | done by hand |

**Out of scope.** The in-process path: `loc_arena/gateway/core.py`, `edge.py`, `client.py` and `transport.py`. The
model gateway stays a separate agentgateway container from the tools gateway. It alone holds the key, sits on the
egress network and writes the sealed record.

## A. What proxy.py does today

| Today (`proxy.py`, `test_gateway_proxy.py`) | agentgateway | How |
| --- | --- | --- |
| holds the key, replacing the caller's `Authorization` | yes | `backendAuth.key.file` is watched, so the key rotates without a restart |
| the key never reaches a log | yes | checked in the logs, against the mock and live |
| forwards only `chat/completions`, `completions` and `models` | yes | binds mode: exact path matches. llm mode: a path rule (see below) |
| no framework pages | yes | `adminAddr`, `statsAddr` and `readinessAddr` are off; `/ui`, `/docs` and `/config_dump` return 404 |
| a forged `Host` cannot redirect a call | yes | `hostOverride` |
| streaming passes through | yes | side effect: streams get `stream_options.include_usage: true` added |
| every call recorded with caller, request and reply | yes | stdout access log with `string(request.body)` and `string(response.body)`; a 200 KB reply arrives whole in one line |
| refused, failed and broken-off calls recorded | yes | 401, 403, 404, 429, 503 and a caller hanging up mid-stream each leave a record |
| `complete` flag | derived | no such field; a stream without `data: [DONE]` (or without output tokens) broke off |
| caller named by reverse DNS | better | callers are admitted by the SHA-256 of their sandbox token, as the tools gateway already does, and the record names the agent |
| runners: model allowlist (403) and calls per play (429) | yes, but | a runner route with `authorization` on `json(request.body).model` and a `localRateLimit` that never refills. Any reload resets the count (see B) |
| no retries | yes | none configured |

Each of the three settings below was found by a check that failed. Without them the record has holes or the key
reaches more of the provider:

1. **Buffer the request body.** A policy that reads it does this, for example `size(request.body) >= 0` in
   `authorization`. Without it, a call whose upstream is never reached is recorded with an empty request.
2. **Remove `accept-encoding` toward the provider.** Otherwise OpenRouter gzips its reply,
   `string(response.body)` fails, and the log silently drops the field. `proxy.py` drops the same header today.
   Recording raw `response.body` (base64) would also prevent a silent drop.
3. **In llm mode, allow only the API paths we serve.** llm mode forwards *any* path under its prefix with the key:
   `/api/v1/foo`, `/api/v2/...` and OpenRouter's account endpoints all returned 200. A
   `request.path in [...]` rule in `llm.policies.authorization` returns 403 for them.

## B. The tier 1 gateway in #142

**The badge**

| #142 asks for | agentgateway | Checked |
| --- | --- | --- |
| a badge on every call: a workload credential per piece of work | yes, as a JWT that IAM signs | `jwtAuth` strict over a JWKS file. No badge, a forged one or an expired one gets 401. The badge never reaches the provider |
| the same, with IAM's opaque workload secrets (the workload stack, #143 to #150) | yes, through HTTP `extAuthz` | IAM answers with the workload and team as headers, which become `extauthz.*`. A secret IAM revokes is refused on the next call |
| every harness carries the badge, with nothing added inside the harness | yes | Claude Code's `apiKeyHelper` re-runs on a timer and on a 401. pi resolves a `!command` key on every request. Both pick up a rotated badge mid-run |
| a forced rotation that really cuts off old credentials | yes, no restart | after the JWKS file was swapped, old badges were refused 0.5 s later. A 30 s stream in flight finished normally |
| the caller's identity from IAM | yes | the badge claims (`jwt.sub`, `jwt.team`, `jwt.work`) or `extauthz.*` land in every record |

**Limits and records**

| #142 asks for | agentgateway | Checked |
| --- | --- | --- |
| per-team quotas | yes, but | `localRateLimit` keyed by `jwt.team`, in tokens: one team got 429 while another still got 200. **Any reload resets these counters**, whether a config edit or a watched file such as the JWKS, so a forced rotation resets every team's quota |
| a per-play cap that holds across reloads | yes, by key | key budgets (`apiKey.keys[].budgets`, in tokens or USD, kept in the database) were still 429 after a reload. Budgets exist only per key, never per JWT claim, and never count requests |
| a ledger the company can read | stored, no reader yet | the database sink (SQLite or Postgres, table `request_logs`) keeps metadata only; the full record stays on stdout. A reader service, or a tool for security, is still needed |
| a cap on the observation window's model calls | yes | a rate limit written into the live config took effect within about 1.5 s. A stream in flight was unaffected |

**Models and routes**

| #142 asks for | agentgateway | Checked |
| --- | --- | --- |
| monitored and backend routes | yes | two routes on one port, told apart in the record. The backend route takes a model key (`x-model-key`) that serves only its own model, plus the badge of the job it runs in |
| Meridian model names mapped to real models | yes, with one catch | `modelAliases` works, but replies name the provider's model. A response transformation (`string(response.body).replace(...)`) can rename it back, but it holds the whole stream: a 30 s stream arrived as one chunk |
| official deployments behind the monitored route | yes | llm mode routes by the requested model. `aurora-7b-v3` goes to its deployment without the provider key, a provider model gets the key, an unknown model gets 404, and a per-model team rule gives 403 |
| a standard chat completions endpoint | yes | also the Messages API (Claude Code) and the Responses API |

**Live, through OpenRouter**, every reply's body was recorded once `accept-encoding` was removed:

- chat with `provider`, `reasoning` and `usage` passed through (200);
- streamed chat ending in `[DONE]`;
- `/api/v1/models`;
- the Messages API, plain and streamed, ending in `message_stop`.

## C. To decide or add

1. **The badge's form.** Both forms ran.
   - **A JWT signed by IAM.** The gateway checks it on its own. Revoking one means rotating the signing key or keeping
     lifetimes short.
   - **IAM's opaque secret, checked live through `extAuthz`.** That costs one IAM call per model call (a cache is
     optional), and revocation takes effect at once.
2. **Quotas across reloads.** Two options:
   - Accept that quotas reset whenever the config or the JWKS changes, and move the runners' per-play cap to a key
     budget in tokens or USD. That replaces `runner_calls_per_play` with a token or money limit.
   - Run a rate-limit service (`remoteRateLimit`, Envoy's protocol), whose counts survive reloads.
3. **Meridian model names in replies.** Two options:
   - `meridian-serving` deployments name their own model, as #142 designs it: the gateway routes by name in llm mode,
     and the server answers as `aurora-7b-v3`.
   - Give up streaming for aliased models.

   Plain `modelAliases` shows agents the provider's model name.
4. **The sealed record's shape.** This changes the sealed log's format: a one-way door.
   - The record becomes the gateway container's stdout, copied with `docker compose logs gateway`. Today `compose cp`
     copies `calls.jsonl`.
   - Docker's log rotation must be off for that container (no `max-size` in the daemon or the compose `logging`
     options), or the record is cut.
   - Its readers change, or read through an adapter: `explorer/reasoning.py` and
     `tests/integration/test_stack_reach.py`.
   - The database sink (`parity_db.yaml`, `llm: full`) is structured. Each call's prompt and completion are stored as
     role and parts JSON, the fields we add are kept (full bodies, caller), and a 200 KB reply arrives whole. It
     records only calls that reach the LLM layer, though: 9 of the 21 calls in the parity run. Refused calls (401,
     403, 404, 429) and `/models` are missing, so it cannot be the sealed record alone. OTLP logs into an
     OpenTelemetry Collector with a file exporter are the other structured route; not tested.
5. **`/api/v1/completions`** (legacy text completion). llm mode does not serve it; a plain route beside it does
   (`combined.yaml`). Check whether anything still calls it.

**Estimated delta of the replacement (not built).** About 4 of `test_gateway_proxy.py`'s 18 tests check how
`proxy.py` works and go with it. The other 14 check behaviour we keep, so they come back as integration tests of the
rendered config against the real agentgateway binary, ported from `probe_parity.py`.

| | Removed | Added | Net |
| --- | --- | --- | --- |
| production code | `proxy.py` −259, `GatewaySettings` about −30 | config renderer about +100, log copy and adapter about +30 | about −160 |
| tests | `test_gateway_proxy.py` −294 | parity checks as integration tests about +180, renderer tests about +40 | about −75 |
| **total** | | | **about −235** |

## Files

| File | What it is |
| --- | --- |
| `parity.yaml` | `proxy.py`'s behaviour in binds mode |
| `combined.yaml` | the same in llm mode, with the runner and legacy routes beside it on one port |
| `tier1.yaml` | the badge, backend and IAM routes, the ledger and team quotas |
| `llm_mode.yaml` | routing by model name |
| `budget.yaml` | a key budget across a reload |
| `live.yaml` | the live calls |
| `mock_upstream.py`, `mock_iam.py` | the stand-ins |
| `up.sh` | renders, validates and starts the stack |
| `probe_*.py` | the checks; `PROBE_MODE=llm` points the parity probe at `combined.yaml` |

## Sources

**agentgateway v1.6.0**

- Release: https://github.com/agentgateway/agentgateway/releases/tag/v1.6.0
  (the version under test, per-key local rate limits, Messages-to-Responses conversion)
- Config reference: https://github.com/agentgateway/agentgateway/blob/v1.6.0/schema/config.md
  (llm mode fields, `llm.providers[].provider.custom.formats`, route `transformations.response.body`, `extAuthz`,
  `jwtAuth`, `apiKey.keys[].budgets`, top-level `routes[].gateways`)
- CEL reference: https://github.com/agentgateway/agentgateway/blob/v1.6.0/schema/cel.md
  (`llm.*`, `jwt.*`, `apiKey.*`, `extauthz.*`, body variables)
- Policy order on a route: https://github.com/agentgateway/agentgateway/blob/v1.6.0/crates/agentgateway/src/proxy/httpproxy.rs
  (jwt, apiKey, extAuthz and authorization run before the rate limits)
- File-backed keys: https://github.com/agentgateway/agentgateway/blob/v1.6.0/crates/agentgateway/src/types/local.rs
  (`backendAuth.key.file` reloads on change)
- Access-log database sink: https://agentgateway.dev/docs/standalone/latest/documentation/observability/access-logs/database/
  (shares the stdout filter; `llm: metadata | full`)
- LLM API keys: https://agentgateway.dev/docs/standalone/latest/documentation/llm/api-keys/
  (inline, environment and file keys)

**Harnesses and provider**

- Claude Code `apiKeyHelper`: https://code.claude.com/docs/en/settings-reference#apikeyhelper and
  https://code.claude.com/docs/en/env-vars (`CLAUDE_CODE_API_KEY_HELPER_TTL_MS`; re-run on a 401 or 403; sent as
  `X-Api-Key` and `Authorization: Bearer`)
- pi custom providers: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/models.md
  (a `!command` key in `models.json` is resolved on every request, uncached)
- OpenRouter API reference: https://openrouter.ai/docs/api-reference/overview
  (`/api/v1` paths, `provider` and `reasoning` request fields)
