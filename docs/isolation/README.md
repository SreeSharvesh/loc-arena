# Running episodes on Docker Compose

Run every episode on Docker Compose so that agent code never holds our OpenRouter key and cannot touch the record
we score from, and so that a setting can give each agent its own sandbox and permissions and bring its own live
services. Small steps, each one closer to how LinuxArena runs, with the least code of our own that still keeps
quality and maintainability. The configuration templates stay the source of everything; two networks by default,
more only when a setting needs them.

## Today: one process

![Today: agent loops, gateway, monitors and grading in one Python process, next to the key and the logs](01-today.png)

`make run` runs every agent, the gateway, the monitors and the grader in one Python process, next to the key. The
agents' shell is off, because their code would run on the host. `STACK=1` is the safe run described below.

## After step 4: safe runs

![After step 4: one compose project per episode; the episode container and the gateway on agent-net, the gateway alone on egress-net](02-safe-runs.png)

## After step 6: a sandbox per agent and live services

![After step 6: the episode sends tool calls to one sandbox per agent; sandboxes reach live services with their credentials; every container's model calls go through the gateway](03-full-picture.png)

## Six steps, each one small PR

![The six steps: the gateway, the episode on compose, grading and teardown, isolation tests, a sandbox per agent, live services](04-steps.png)

The deterministic run's scores stay unchanged through every step. Steps 1 to 5 have shipped; step 6 describes what
comes next.

1. **The gateway.** The key leaves the host process, and every model call is sealed with the container that made
   it.
2. **The episode on compose.** Today's run moves into a container on agent-net with no key and no internet. When
   it ends, Ctrl-C included, its logs are copied out and the project is torn down; if a copy fails, the project
   is kept so nothing recorded is lost.
3. **Grading on the host, as in LinuxArena.** Play leaves a snapshot (its event logs and repo checkout), and the
   host grades it after copy-out. The grading steps that run agent-written code, the repo tests and the benchmark,
   run in a throwaway container with no network. The episode image holds no grader answers, and `bash` turns on.
4. **Isolation tests** for every row of the reach table below, including that an outside name does not resolve.
5. **A sandbox per agent, with permissions.** One `sandbox-<agent id>` per agent. The agent loop stays in the
   episode container and sends each tool call to the agent's sandbox through the execution server from the closed
   stack (#39), so agent code runs only in sandboxes. Each sandbox's execution server takes calls only with that
   sandbox's own token. (#92)
6. **One live service, behind a credential.** A service entry in the scenario pack with an `image` or `build`
   becomes its own container on agent-net; one without stays simulated, as today. The first live service is a
   dummy, `notes`, a small notes board. Agents reach it from their sandboxes at `http://notes:8000`. It answers
   only a caller that sends a credential it accepts, and the run config mounts that credential only into the
   sandboxes that hold it. The service has no route out, holds no key or token, and cannot call a sandbox. No
   container gets the Docker socket. Worker containers, per-agent networks (#45) and services that call models
   are later work.

## Sandboxes and live services in the templates

![Template examples: a sandbox block per agent with credentials, networks and volumes; a live service entry with build, command, port, networks and the credentials it accepts](05-templates.png)

Credentials are compose secrets mounted only into the sandboxes that hold them and the services that check them.
Per-agent networks and volumes are optional and later, for a setting that wants a physical wall between agents
(#45).

A service is declared in `scenarios/<pack>/scenario.yaml`. The first live one is `notes`:

```yaml
services:
  notes:
    build: services/notes          # a directory in the scenario pack, holding a Dockerfile
    command: [python, -m, notes_service]
    port: 8000
    accepts: [notes]               # the credential names it checks
    description: >
      The team's notes board. Read, write and delete short notes by key.
  ticketboard:
    port: 8090                     # no build and no image: stays simulated, the stack ignores it
```

The run config says which agent holds which credential:

```yaml
agents:
  - id: agent-main
    sandbox:
      credentials: [notes]         # mounted into this agent's sandbox only
```

`configs/aurora-efficiency.yaml` grants `notes` to every agent except `controlplane-agent`, so the default run
shows both sides of the wall. A credential that no live service of the run's scenario accepts is refused at load,
so a typo cannot pass.

For each live service the rendered compose file holds one container with these fixed properties:

- It joins agent-net and never egress-net, so it has no route out.
- It drops every capability and runs under the stack's service limits (`service_memory_limit: 512m`,
  `service_cpus: 1.0`, `service_pids_limit: 256`). The gateway's health interval and retries also pace each
  service's healthcheck.
- It mounts no volume, publishes no port and has no field that could add the Docker socket.
- Its only secrets are the credentials it accepts. The run generates each value, like the sandbox tokens, and
  passes it to compose through the environment, never on a command line or in a file.
- The episode starts only after every live service reports healthy.

Each agent's system prompt in a stack run lists the live services it may use: the address, the description and,
for each credential it holds, where to read it (`/run/secrets/credential_<name>`) and how to send it
(`Authorization: Bearer <contents>`). An agent that holds none of a service's credentials is still told the
service exists; the service refuses it. An in-process run adds nothing, so the deterministic run is unchanged.

## Time

Live services run on real time, as LinuxArena's do. The framework has no fake or simulated clock. Its
environments run real cron in their containers, and a check that needs time-dependent state writes that state
and triggers the job itself: it backdates a row, then runs the worker or the cleanup (see Sources). The same
holds here. The run's simulated clock stays for the scenario's scripted events, and a live service does not read
it.

## Bringing in another service

Another service comes in the same way, by one more entry under `services:` in the scenario pack. A ready image
needs no build directory:

```yaml
services:
  notes: ...
  database:
    image: postgres:16
    port: 5432
    healthcheck: [CMD, pg_isready, -U, postgres]
    accepts: [database]
    description: >
      The team's Postgres. Connect with the credential as the password.
```

The run config then grants `database` to the agents that may use it:

```yaml
agents:
  - id: agent-main
    sandbox:
      credentials: [notes, database]
```

Without a `healthcheck`, the stack probes the declared `port`. A service that checks no credential lists an empty
`accepts` and is open to everything on agent-net. An entry takes only the fields above; any other key is
refused. The loader names the entry when its name is not a DNS label (or is `gateway` or `episode`, or starts with
`sandbox-`), when it sets both `build` and `image`, when `build` is not a directory under the scenario pack
(outside `reference/` and `scripted/`) holding a `Dockerfile`, when a credential name is not lowercase letters,
digits and hyphens, or when a live entry has no `port`.

## The gateway: a pass-through to OpenRouter

![One model call: the caller sends an OpenRouter request with any key; the gateway swaps in the real key, records the call and the caller in the sealed log, and returns the reply unchanged](06-model-call.png)

Our own small proxy, about 100 lines of FastAPI and httpx, which the project already depends on. Anything that
works against OpenRouter works against the gateway by changing its base URL, streaming included. Our agent loop
sends the whole conversation on every call, so the sealed log also holds each tool call and its result.

## The compose file

`make run STACK=1` renders a compose file from the run config's `gateway:` and `stack:` settings into
`logs/compose/<project>.yaml`, brings it up under the episode's own project name, copies the logs and the gateway's
call log out, and tears it down. From step 3 it also grades the snapshot on the host.

<details>
<summary>A sketch of the rendered compose file (abridged)</summary>

```yaml
services:
  gateway:                  # the only container with the key and the internet
    build: .
    command: python -m loc_arena.gateway.proxy --upstream https://openrouter.ai --port 8080
    secrets: [openrouter_api_key]
    volumes: [sealed:/sealed]
    networks: [agent-net, egress-net]
  episode:                  # today's run, with the shell on
    build: .
    command: python -m loc_arena.cli run --run ${RUN} --mode ${MODE}
    environment: {LOC_ARENA_GATEWAY_URL: http://gateway:8080/api/v1/chat/completions}
    volumes: [workspace:/app/workspace, output:/app/logs]
    networks: [agent-net]
networks:
  agent-net: {internal: true}
  egress-net: {}
secrets:
  openrouter_api_key: {environment: OPENROUTER_API_KEY}
volumes: {sealed: {}, workspace: {}, output: {}}
```

</details>

## What agent code can reach

![What agent code can reach today, after step 2 and after step 5: the key, the internet, the host, the sealed log and the grader's answers go from yes to no; the agent loop and other agents' sandboxes go to no at step 5; live services only with an accepted credential](07-reach.png)

In a stack run each agent's code runs in its own sandbox, `sandbox-<agent id>`. Only the episode can call a
sandbox's command server: a sandbox refuses every other caller, even one holding its token. Code left in the
shared checkout still runs wherever another agent runs it (the shared checkout is a channel between agents by
design). No sandbox reaches the agent loop, which listens on no port. The grader's answers row also covers the grading code: no
sandbox holds the harness, a verifier, a scorer or a scenario spec. Grading's container runs the same slim
image, so it has no harness or scenarios, but it sees the sealed meter it runs. A live service is reached only with a
credential: a sandbox gets an answer from it only when it holds a credential the service accepts, the episode and
the gateway hold none, and the service has no route out and holds no key or token. It cannot run a command in a
sandbox either, since a sandbox takes calls from the episode alone. No container in the project mounts the Docker
socket. In an in-process run the agents'
tools still run their code on the host; with `stack.sandbox_agent_code` on, grading's agent-code steps run in
the slim image there too.

## Recycled and shipped on its own

From the closed stack (#37–#51), these come back inside the steps: the image build with the scenario's codebase
(#44), one compose project per episode with teardown on Ctrl-C (shipped in step 2) and on a hung Docker daemon
(#42, #51; still to come), the execution
server (#39), and per-agent networks and volumes (#45). The separate edge, core
and recorder services, the turn tokens and the control key are dropped.

These do not depend on Docker and land as their own small PRs: the run config validated with pydantic (#48), one
settings root (#37), grading by the scorer the run config names (#43), each action recorded under the agent that
made it (#47), and the agent loop's fault handling (#50).

## Open questions

- How should a model call from a process an agent started be told apart from the agent's own turns, now that the
  gateway names callers by container?

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
