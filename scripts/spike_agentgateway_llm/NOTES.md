# Spike: agentgateway as the model gateway

Question: can agentgateway v1.6.0 (already our tools gateway) replace `loc_arena/gateway/proxy.py`, and does it give
the tier 1 gateway that the Meridian proposal (#142) asks for?

Answer: yes to both, with five things to decide or add, listed at the end. Every row below is a check that ran:
`probe_parity.py` (27/27) and `probe_tier1.py` (23/24, the miss is the model name in replies) against a mock
upstream, `llm_mode.yaml` by hand, and `probe_live.py` with six calls to OpenRouter.

Out of scope: `loc_arena/gateway/core.py`, `edge.py`, `client.py`, `transport.py` (the in-process path). The model
gateway stays its own agentgateway container, separate from the tools gateway: it alone holds the key, sits on the
egress network and writes the sealed record.

## A. What proxy.py does today

| Today (`proxy.py`, `test_gateway_proxy.py`) | agentgateway | How |
| --- | --- | --- |
| holds the key; the caller's `Authorization` is replaced | yes | `backendAuth.key.file`, watched, so the key rotates without a restart |
| the key never reaches a log | yes | checked in logs, mock and live |
| only `chat/completions`, `completions`, `models` are forwarded | yes | exact path matches; anything else 404, and recorded |
| no framework pages | yes | `adminAddr`, `statsAddr`, `readinessAddr` off; `/ui`, `/docs`, `/config_dump` are 404 |
| a forged `Host` cannot redirect | yes | `hostOverride` |
| streaming passes through | yes | one side effect: streams get `stream_options.include_usage: true` added |
| every call recorded with caller, request, reply | yes | access log on stdout, with `string(request.body)` and `string(response.body)`; a 200 KB reply arrives whole in one line |
| refused, failed and broken-off calls recorded | yes | 401, 403, 404, 429, 503 and a caller hanging up mid-stream each leave a record |
| `complete` flag | derived | no such field; a stream without `data: [DONE]` (or without output tokens) broke off |
| caller named by reverse DNS of its container | better | callers are admitted by the SHA-256 of their sandbox token, as the tools gateway already does; the record names the agent |
| runners: model allowlist (403) and calls per play (429) | yes | a runner route with `authorization` on `json(request.body).model` and a `localRateLimit` that never refills |
| no retries | yes | none configured |

Two settings are needed for the record to be complete. Both were found because a check failed:

- **Buffer the request body** with a policy that reads it (`size(request.body) >= 0` in `authorization`). Without it,
  a call whose upstream is never reached is recorded with an empty request.
- **Remove `accept-encoding` toward the provider.** OpenRouter then gzips, `string(response.body)` fails, and the log
  silently drops the field. `proxy.py` drops the same header today. Recording raw `response.body` (base64) instead
  would also avoid a silent drop.

## B. The tier 1 gateway in #142

| #142 asks for | agentgateway | Checked |
| --- | --- | --- |
| a badge on every call: a workload credential issued per piece of work | yes, as a JWT IAM signs | `jwtAuth` strict over a JWKS file: no badge, a forged one or an expired one is 401; the badge never reaches the provider |
| the same, with IAM's opaque workload secrets (the workload stack, #143 to #150) | yes, through HTTP `extAuthz` | IAM answers with the workload and team as headers, which become `extauthz.*`; a secret IAM revokes is refused on the next call |
| a forced rotation that really cuts off old credentials | yes, no restart | swapping the JWKS file refused old badges 0.5 s later; a 30 s stream in flight finished normally |
| the caller's identity from IAM | yes | badge claims (`jwt.sub`, `jwt.team`, `jwt.work`) or `extauthz.*` land in every record |
| per-team quotas | yes | `localRateLimit` keyed by `jwt.team`, in tokens: one team ran out (429) while another still got 200. Tokens are charged after the reply, so a call can overshoot |
| a ledger the company can read | yes | the database sink (SQLite or Postgres, `request_logs` table) with metadata only; the full record stays on stdout |
| monitored and backend routes | yes | two routes on one port, told apart in the record. The backend route takes a model key (`x-model-key`) that serves only its own model, plus the badge of the job it runs in |
| Meridian model names mapped to real models | yes, with one catch | `modelAliases` works, but replies name the provider's model. Rewriting them back works only by holding the whole stream (a 30 s stream arrived as one chunk) |
| official deployments behind the monitored route | yes | `llm:` mode routes by the requested model: `aurora-7b-v3` goes to its deployment without the provider key, a provider model gets the key, an unknown one is 404, a team rule per model gives 403 |
| a cap on the observation window's model calls | yes | a rate limit written into the live config took effect within 1.6 s; a stream in flight was unaffected |
| a standard chat completions endpoint | yes | also the Messages API (Claude Code) and the Responses API |

Live, through OpenRouter: chat with `provider`, `reasoning` and `usage` passed through (200); streamed chat ending
in `[DONE]`; `/api/v1/models`; Messages, plain and streamed, ending in `message_stop`. Every reply's body was
recorded once `accept-encoding` was removed.

## C. To decide or add

1. **Badge format: a JWT, or IAM's opaque secret checked live.**
   - A JWT is checked inside the gateway; revoking one means rotating the signing key, or using short lifetimes.
   - An opaque secret means one IAM call per model call (`extAuthz`, with an optional cache), and revocation is
     immediate.
   - Both ran. Pick one with the identity work.
2. **Meridian model names in replies.** Either `meridian-serving` deployments name their own model (#142's design:
   the gateway routes by name in `llm:` mode, and the server answers as `aurora-7b-v3`), or streaming is given up for
   aliased models. Plain `modelAliases` leaks the provider's model name to agents.
3. **The record's shape.**
   - The sealed record becomes the gateway container's stdout, copied with `docker compose logs gateway` where today
     `compose cp` copies `calls.jsonl`. That needs Docker's default log driver.
   - Its readers change, or read through an adapter: `explorer/reasoning.py` and `tests/integration/test_stack_reach.py`.
   - This is a change to the sealed log's format: a one-way door.
4. **Rate-limit state across a reload.** Not checked: whether a config reload resets the runner's per-play counter or
   a team's tokens. Buckets are also per replica (we run one).
5. **`/api/v1/completions`** (legacy text completion) passes through in binds mode only. `llm:` mode does not serve
   it. Check whether anything still calls it.

Estimated delta of the replacement (not built):

| Change | Lines |
| --- | --- |
| delete `proxy.py` | −259 |
| delete most of `test_gateway_proxy.py` | about −294 |
| narrow `GatewaySettings` | about −30 |
| add a config renderer beside `tools_gateway.py` | about +100 |
| add the log copy and adapter | about +30 |
| add rendering tests | about +80 |
| **net** | **about −370** |

## Files

| File | What it is |
| --- | --- |
| `parity.yaml` | `proxy.py`'s behaviour |
| `tier1.yaml` | the badge, backend and IAM routes, the ledger, team quotas |
| `llm_mode.yaml` | routing by model name |
| `live.yaml` | the live calls |
| `mock_upstream.py`, `mock_iam.py` | the stand-ins |
| `up.sh` | renders, validates and starts the stack |
| `probe_*.py` | the checks |

## Sources

- agentgateway v1.6.0 release: https://github.com/agentgateway/agentgateway/releases/tag/v1.6.0 (version under test,
  per-key local rate limits, Messages-to-Responses conversion)
- Config reference at v1.6.0: https://github.com/agentgateway/agentgateway/blob/v1.6.0/schema/config.md (`llm:` mode
  fields, `llm.providers[].provider.custom.formats`, route `transformations.response.body`, `extAuthz`, `jwtAuth`)
- CEL reference at v1.6.0: https://github.com/agentgateway/agentgateway/blob/v1.6.0/schema/cel.md (`llm.*`, `jwt.*`,
  `apiKey.*`, `extauthz.*`, body variables)
- Policy order on a route: https://github.com/agentgateway/agentgateway/blob/v1.6.0/crates/agentgateway/src/proxy/httpproxy.rs
  (jwt, apiKey, extAuthz, authorization, then rate limits)
- File-backed keys watched: https://github.com/agentgateway/agentgateway/blob/v1.6.0/crates/agentgateway/src/types/local.rs
  (`backendAuth.key.file` reloads on change)
- Access-log database sink: https://agentgateway.dev/docs/standalone/latest/documentation/observability/access-logs/database/
  (shares the stdout filter; `llm: metadata | full`)
- LLM API keys: https://agentgateway.dev/docs/standalone/latest/documentation/llm/api-keys/ (inline, env and file keys)
- OpenRouter API reference: https://openrouter.ai/docs/api-reference/overview (`/api/v1` paths, `provider` and
  `reasoning` request fields)
