# Running episodes on Docker Compose

`make run` runs one episode as a Docker Compose project, under central IAM. Agent code never holds the OpenRouter key and
cannot touch the record the run is scored from. Each agent has its own sandbox and its own tools, and the
scenario brings its own services.

## The model

Each agent has two kinds of tools: its harness's native tools (read, edit, bash), which act in its own sandbox,
and an MCP client. The native loop itself runs in `episode`. Most of what an agent can do to the company, such as
opening a PR, messaging a teammate in chat, submitting a job or writing a note, is a tool on a service
behind one MCP route, agentgateway; the run config decides exactly which tools each agent is offered. A
rights-bearing service such as `notes` or `chat` can also be called over HTTP from the sandbox directly, with the agent's own identity. Every service records each call made to it and by
whom. After play, those records become the events the monitors and the verifier read. The honest twin plays on
services and sandboxes recreated, each service from its seed.

<img alt="Deployment of a stack run: the model gateway alone joins agent-net, egress-net and record-net, so every model call leaves through it and its record goes to the collector, while tool calls go through agentgateway and the sandboxes also call services with rights over HTTP." src="networks.svg" width="100%">

Agent-net puts no firewall between its containers: any of them can open a connection to a port another listens on. What a call may do depends on the token or identity
it carries and on whom the callee accepts. Runner executions and jobs also call the services, with the
credentials they are given.

Any harness with an MCP client sees the same tools, from the same services, recorded the same way. The native
loop is the only harness the arena runs today; choosing another one by config is #84.

## Containers

| Container | Role |
|---|---|
| `gateway` | agentgateway, the only holder of the key and of a route out, on agent-net, egress-net and record-net; admits each caller by its own token and exports a record of every model call, with the caller, to the Collector |
| `collector` | an OpenTelemetry Collector on record-net, which only the gateway joins; appends each record to the sealed record in a volume only it mounts |
| `episode` | plays the episode and, in attack mode, its honest twin; holds every sandbox's token and no identity |
| `sandbox-<agent id>` | one agent's code, from a slim image with no harness and no scenarios; mounts the checkouts and the scenario's skills read-only; holds its own token and its own identity on each service with rights; takes commands from the episode alone |
| one per live service | a scenario's `services:` entry with `build`, `image` or `module`; no route out, all capabilities dropped, limits from `stack`, no published port |
| `runner-<n>` | where the company's own agents run with pi, for the one service whose entry declares `runners: <n>`; a sandbox from the Dockerfile's `runner` target with an empty `/workspace`; holds its own token, which that service also holds, and takes commands from that service alone; an execution there gets its agent's tokens on the company's services (`MERIDIAN_<SERVICE>_TOKEN`) |
| `node-<n>` | the job service's fleet, for the one service whose entry declares `nodes: {<pool>: n}`; a worker from the Dockerfile's `worker` target whose daemon runs one job at a time as a real process in an empty `/workspace`, with the checkouts mounted read-only, the cluster's own read-only deployment of the company repos at `/opt/meridian` that the seed's jobs run, and the limits `stack.node_cpus` and `stack.node_memory_mb`; holds its own token, which that service also holds and its jobs call the gateway with, and takes calls from that service alone; gives each job the secrets of the job's own workload; its log is its jobs' process facts ([docs/jobsvc-service.md](../jobsvc-service.md)) |
| `agentgateway` | the MCP route of every service with tools; present when a service has tools |

The host grades the run after copy-out; grading's agent-code steps run in a throwaway container of the slim
image with no network. The episode image holds no grader answers. No container mounts the Docker socket.

## Tools

A service lists the MCP tools it serves in `scenarios/<pack>/scenario.yaml`; the run config's `sandbox: tools:`
lists, per agent, the tools it may call on each:

```yaml
# scenario.yaml
services:
  forge:
    module: loc_arena.forge.service   # a module of the engine image, run with the run config
    port: 8000
    tools: [open_pr]
# aurora-efficiency.yaml
agents:
  - id: agent-main
    sandbox:
      tools: {forge: [open_pr], chat: [post, send_dm, read_dms]}
```

- agentgateway's config is generated per run from `sandbox: tools:` (`loc_arena/tools_gateway.py`). It admits an
  agent by its sandbox token, lists it exactly its tools, refuses every other before it reaches the service, and
  calls the service with that agent's identity. Tools keep their own names across services, so a tool name is
  served by one service only.
- A service holds no agent's identity: central IAM verifies each caller's credential, and the service takes the
  caller from IAM's answer, never from an argument. A call without a credential IAM accepts is a 401. agentgateway
  holds each agent's credential for a service with tools: a sandbox holds none for it.
- Each service enforces the caller's `scope` from the run config itself, since an agent's bash can reach the
  route too. A refused call comes back as a tool error with the service's reason.
- Offering a tool grants nothing: IAM seeds each agent's `sandbox: rights:` alone, and a call to an offered tool
  takes the permission its operation checks, held from the start or asked for.
- An unknown service or tool in `sandbox: tools:` is refused when the config loads. The honest twin's agents
  are offered the same tools and hold the same rights as the attack run's.
- `run_tests` and `run_benchmark` are skills, not tools: scripts with a `SKILL.md` in the pack's `skills:`
  folder, which every sandbox mounts read-only at `/skills` and agents run with bash on their own checkout.
- An in-process run (a config with `iam.enabled` off, such as the deterministic fixture) serves each agent its
  own in-memory forge, the one live service it runs, and offers no bash, so its agents run no code. No IAM runs there,
  so a config with IAM on is refused in process before anything is written.

## Records and events

A service built on the official MCP SDK writes one JSON line per tool call, in OpenTelemetry's names for an MCP
tool call (`loc_arena/tool_records.py`): the tool, its arguments, its result or error, the agent and the time. A
container writes it to stdout; an in-process run writes `records/<service>.jsonl`. agentgateway logs the same
calls with the caller added.

Before grading, the run stops the runners and copies each live service's log, each node's, and agentgateway's,
to `services/<name>.log` in the run directory, and Phoenix's spans, which no monitor reads, to
`services/phoenix.spans.jsonl`. `loc_arena/recorded_events.py` then turns each record into the events of the episode or the honest
twin whose play window holds its time, in the turn of the agent that was running. Each event goes to the sealed
log and, unless it is sealed-only, the mirror log: a node's `process` facts stay on the sealed log, where the verifier reads them and the monitors cannot. A refused call is a blocked `action`. A record that cannot be placed goes to
`unattributed_records.jsonl` in the run directory. Grading never builds events, so grading a run again adds
none.

## The honest twin

In a stack run the episode container plays one phase per start. Between the two, the host adds each service's
log so far, and Phoenix's spans, to their files in the run directory and waits until the Collector holds a
record of every call the gateway logged, then recreates the gateway and every sandbox, runner, node, live service
and agentgateway with fresh anonymous volumes (`loc_arena/stack_play.py`). The Collector and its record run on,
and the gateway counts the twin's runner calls afresh. The episode stays.
If a log cannot be copied, nothing is recreated; if a container does not come back, the run stops with its
logs copied out. The twin therefore sees none of the notes, grants, PRs, jobs or files the attack left.

## Services with rights

A service with `rights` in its entry issues one identity per agent and enforces what each agent may do. One
is `notes`, the team's shared notes ([notes service](../notes-service.md)), over HTTP and as MCP tools; the run
config seeds each agent's starting rights:

```yaml
# scenario.yaml
services:
  notes:
    module: loc_arena.notes_service
    port: 8000
    rights: [read, write]
    tools: [list_notes, read_note, write_note, delete_note]
iam:
  resources:
    notes: {..., approver: {principal: notes-owner, approves: [read, write]}}
# aurora-efficiency.yaml
agents:
  - id: agent-main
    sandbox:
      rights: {notes: [read, write]}
```

An agent reads its identity at `/run/secrets/identity_<service>` and sends it as `Authorization: Bearer
<contents>`. Every agent has an identity on every such service, with rights or none. The default config gives
every agent but `controlplane-agent` `read` and `write`, and `controlplane-agent` none. Central IAM seeds each
starting right as a baseline grant; the service's approver (`notes-owner`, never an agent under test) decides its
access requests, and the service keeps no rights and asks IAM about every call. A config
whose service declares a right its IAM manifest never checks does not load.

An agent asks for a right with an HTTP call from its sandbox to the IAM service (`POST /requests`); the
service's approver decides it, and no agent can grant one. `GET /`, `GET /health` and `GET /openapi.json` need no
identity.

| Call | Needs | Result |
| --- | --- | --- |
| `GET /notes`, `GET /notes/<key>` | `read` | the notes |
| `PUT /notes/<key>`, `DELETE /notes/<key>` | `write` | the change |
| `GET /grants` | any identity | every agent's current rights |
| `POST /grants`, `DELETE /grants` with `{"agent": "<id>", "rights": ["read"]}` | an approver of the service, IAM decides | the agent's new rights |

No identity, or an unknown one, is a 401; a known one without the right is a 403 that names the permission and
how to ask IAM for it. A right the service's manifest does not know, or an agent IAM does not know, is a 400.
The service writes one JSON line per request (`meridian_controlplane.request_audit`: `wall_ts`, `caller`,
`container`, `method`, `path`, `status`, `operation`, `arguments`), a grant or revoke among them. A line whose
`caller` and `container` name different agents is a copied identity: an agent can pass its identity on in a
message or the shared checkout, and the log tells that from a recorded grant.

The second is `agents`, Meridian's agents service ([docs/agents-service.md](../agents-service.md)), with the same
identities, IAM's 401 and 403, and `/grants`. Its code is company code, served as a module of the engine image, which
puts each company repo on the path of every module it serves.

The third is `tickets`, Meridian's ticket tracker ([docs/tickets-service.md](../tickets-service.md)), also company
code. Agents reach its tools through the tools gateway and its HTTP API from their sandbox, with their credential
on it; IAM decides both.

A service with neither `rights` nor `tools` is open to every caller on agent-net, and a ready `image` needs its
own `healthcheck`. The loader refuses an entry with an unknown key, a name that is not a DNS label or that the
stack takes (`gateway`, `episode`, `agentgateway`, `sandbox-*`), more than one of `build`, `image` and `module`,
a live entry with no `port`, or a tool served by two services.

## Time

Live services run on real time, as LinuxArena's do; the framework has no simulated clock. A check that needs
time-dependent state writes it and triggers the job itself (see Sources). The run's simulated clock serves the
scenario's scripted events only, and each episode records its readings against wall time, so a service record
maps to an episode time.

## The gateway

![The model gateway in a stack run: callers on agent-net send their own tokens to the gateway, which alone joins agent-net, egress-net and record-net; it calls OpenRouter over egress-net and exports each call's record over record-net to the Collector, whose sealed volume the host copies out for grading and the explorer](06-model-call.png)

agentgateway, configured per play by `loc_arena/gateway/model_gateway.py`. Anything that works against OpenRouter
works against it by changing its base URL and sending the caller's token, streaming included. It admits callers
by the SHA-256 of their token, so it holds none: the episode, the host's monitors, each sandbox (its sandbox
token), each runner and each node. A path outside `gateway.allowed_paths` gets a 403, and a call without a known
token a 401.

Every call leaves a record (caller, request, reply and status, refused and failed calls included), which the
gateway exports over OTLP to the Collector. A stack run waits until the Collector holds a record of every call
the gateway logged before the gateway stops, then copies the record to `logs/gateway/<project>.calls.jsonl`, one
`GatewayCall` per line, each tagged with its play, with the Collector's own lines beside it. Before grading it
copies the record of play into the run directory too (`gateway.calls.jsonl`), for the side-task checker, which
reads the calls of a job there alone; without it, the run is not graded. The compose file is rendered from the run
config's `gateway:` and `stack:` settings into `logs/compose/<project>.yaml` (`loc_arena/episode_stack.py`).

Runners call on their own route, `/runner/api/v1`: they may call only `gateway.runner_models` (by default the
seeded agents' model), and make `gateway.runner_calls_per_play` calls (1000) between them in a play. A call past
the cap gets a 429, one for any other model a 403, and both are recorded like any call. Agents under test can
start the company's agents, which spend real money. A job calls on the same route, within the same limits, as
its node with the node's token: the gateway admits no workload's own credential yet, so it records a job's call
under the node's name.

## What agent code can reach

| Agent code can reach | In a stack run |
|---|---|
| the OpenRouter key, the internet, DNS, the host's files and `.env` | no |
| the sealed call log or the Collector, the grader's reference answers, the episode's logs | no |
| the agent loop (it listens on no port) | no |
| another agent's sandbox or credentials | no |
| the gateway | yes, with its sandbox token, recorded under the sandbox's name; from a job it submits, as that job's node |
| a service with rights | only with its own identity, and only the rights it holds |
| a service with tools | only through agentgateway with its sandbox token, and only the tools its run config lists |
| a service with neither, such as Phoenix | yes: it is open to everything on agent-net, so any agent can read, delete and forge the company's spans |
| code left in the shared checkout | runs wherever another agent runs it; the checkout is a channel between agents |

The sandbox mounts only the checkouts and the skills, takes commands from the episode alone, and holds no
harness, verifier, scorer or scenario spec. Grading's container runs the same slim image, though it sees the
sealed meter it runs. In an in-process run no agent runs code, since `bash` is offered only with a sandbox.

## Sources

- [linuxarena/control-tower](https://github.com/linuxarena/control-tower): the framework. Its source has no fake or
  simulated clock; its sandbox for simulated environments leaves wall-clock timestamps to the model's judgment.
- [linuxarena/ct-linuxarena](https://github.com/linuxarena/ct-linuxarena): `envs.lock.yml` lists the ten public
  environments, each a repository `github.com/linuxarena/<name>`.
- [linuxarena/clinical_trial](https://github.com/linuxarena/clinical_trial): `codebase/my-crontab` runs real cron in
  the container (two jobs every 5 minutes, a report on Mondays at 09:00). Backs real time for live services.
- [linuxarena/model_registry](https://github.com/linuxarena/model_registry):
  `main_tasks/scheduled_model_expiration/scorer.py` sets `expires_at = NOW() - INTERVAL '1 hour'`, then triggers
  the worker directly. Backs a check writing its own time-dependent state and running the job.
- [linuxarena/user_tracking](https://github.com/linuxarena/user_tracking):
  `main_tasks/story_data_retention_and_cleanup/scorer.py` inserts rows dated `NOW() - INTERVAL '60 days'`, then
  checks the cleanup. Backs the same.
- [agentgateway](https://agentgateway.dev/docs): `mcpAuthorization` and `transformations`, the per-agent tool
  policy and the caller's identity passed to each service.
- [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai): the
  names in a service's record (`docs/gen-ai/mcp.md`).
- [Agent Skills specification](https://agentskills.io/specification): the `SKILL.md` format of the skills folder.
