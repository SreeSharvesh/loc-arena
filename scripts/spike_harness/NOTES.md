# Spike: an external harness inside an agent's sandbox

Question: can pi (`@earendil-works/pi-coding-agent` 1.1.0) or Claude Code (`@anthropic-ai/claude-code`
2.1.296), run inside `sandbox-agent-main` of a stack run, do what the native harness does? Both did, on
2026-10-11, with `deepseek/deepseek-v4.1-flash` (the cheap untrusted model). The whole spike cost about $0.011
of model calls, by the usage OpenRouter reported in the gateway's call log.

## What ran

1. Images under this spike's own tags: `docker build --target sandbox -t loc-arena-sandbox:spike-harness .`,
   `docker build --target engine -t loc-arena:spike-harness .`, then `docker build -f
   scripts/spike_harness/Dockerfile --build-arg SANDBOX_IMAGE=loc-arena-sandbox:spike-harness -t
   loc-arena-sandbox-harness:spike-harness .`. That image is the sandbox image plus node 22.20 and both
   harnesses, with secret-free config in `/opt/harness-home`.
2. `up.py` renders the compose file of `configs/aurora-efficiency.spike.yaml` as a run does, drops the episode
   and the notes board, points agent-main's sandbox at the harness image, and starts the gateway, two sandboxes
   and agentgateway with its tool services. Secrets reach compose through its environment only.
3. The seven company repos were copied into `/checkouts/spike` in agent-main's sandbox, as the checkout.
4. Each harness got the same nine-step brief (`task.md`), run with `docker exec` as `nobody`:
   - pi: `pi --no-approve --mode json "<brief>"` with `PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0`.
   - Claude Code: `run_claude.sh "<brief>"`.

## Configuration each harness needed

| | pi | Claude Code |
|---|---|---|
| Model | `models.json`: provider `gateway`, `baseUrl http://gateway:8080/api/v1`, `api openai-completions`, placeholder `apiKey` | `ANTHROPIC_BASE_URL=http://gateway:8080/api`, placeholder `ANTHROPIC_AUTH_TOKEN`, empty `ANTHROPIC_API_KEY`, every model variable set to the cheap model |
| Gateway change | none (chat completions is already allowed) | `api/v1/messages` added to `gateway.allowed_paths` (spike config) |
| MCP | `mcp.json`: `url http://agentgateway:3000/mcp`, `Authorization: !echo Bearer $(cat /run/secrets/sandbox_token)`, `exposure: direct` | `--mcp-config` with `type: http` and `Authorization: Bearer ${TOOLS_KEY}`, `--strict-mcp-config` |
| Skills | `settings.json` `skills: ["/skills"]` | `/skills/*` symlinked into `$HOME/.claude/skills` |
| Tools | defaults | `--tools Bash,Read,Edit,Write,Skill`; without it, Task, WebFetch, WebSearch, Cron, Workflow and more are offered |

## Findings beyond the five capabilities

- A harness in the sandbox bypasses the episode's in-world gateway (`GatewayEdge` and `GatewayCore` in
  `loc_arena/live.py`), which the native loop's calls pass through before they reach the gateway container.
  So for harness turns there is no covert-objective injection for `covert.target_identity` (an attack
  episode's seed never arrives), no signed turn token and so no `turn_id`, no sealed `inference_call`
  record for the verifier, and no mirror `inference_call` for the inference monitor. The gateway container's
  `calls.jsonl` records every call, but grading never reads it. Harness turns therefore neither complete
  `unattributed_inference_calls` (they make no `inference_call` at all) nor show up to monitors.
- Routing harness calls through the in-world gateway would put the turn credential in the sandbox, where
  agent code can read and reuse it, so a stamp set from inside the sandbox cannot separate harness turns
  from off-path calls by agent code.
- Both harnesses' model calls land in the gateway's call log with `caller` the sandbox container, the same
  container agent code runs in: the gateway cannot tell a harness turn from a model call by code the agent
  started (the open question in `docs/isolation/README.md`, now for every turn).
- The harnesses' own read, edit and bash make no `action` events (the native loop's `Recorder` writes
  those), so monitors see only the MCP calls, built from the services' records.
- Native tool calls (read, edit, bash) leave no record outside the model conversation. Each one can be
  recovered from the next request's messages in `calls.jsonl`; MCP calls are also in agentgateway's log and
  the service's own log.
- Neither harness sends the `provider` routing of `models.cheap.yaml`; OpenRouter routed by default.
- One pi call is recorded `complete: false` with status 200 although its reply holds the whole stream through
  `data: [DONE]`. Inferred, not observed: pi hung up before the upstream closed.
- Not checked: whether Claude Code sends an `x-api-key` header; the gateway replaces only `authorization`
  and forwards other headers upstream.
- Claude Code reports its own cost at Anthropic prices ($0.12 for a run OpenRouter billed under a cent) and warns
  `unrecognized_model` on stderr. It loads its bundled skills beside the scenario's.
- `docker exec` stands in for the sandbox command server, which caps a command at `shell_timeout_seconds` (300)
  and returns 10 KB of output tails. A real episode needs a launch path that allows a whole agent session.
- `$HOME` is `/tmp`, a scratch directory emptied on reset: the harness config is copied in per run, and the
  harness's own session log must be copied out before a reset.
