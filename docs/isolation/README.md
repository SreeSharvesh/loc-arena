# Running episodes on Docker Compose

Run every episode on Docker Compose so that agent code never holds our OpenRouter key and cannot touch the record
we score from, and so that a setting can give each agent its own sandbox and permissions and bring its own live
services. Small steps, each one closer to how LinuxArena runs, with the least code of our own that still keeps
quality and maintainability. The configuration templates stay the source of everything; two networks by default,
more only when a setting needs them.

## Today: one process

![Today: agent loops, gateway, monitors and grading in one Python process, next to the key and the logs](01-today.png)

`make run` runs every agent, the gateway, the monitors and the grader in one Python process, next to the key. The
agents' shell is off, because their code would run on the host. `STACK=1` brings placeholder services up around
the run; the episode itself stays in this process.

## After step 4: safe runs

![After step 4: one compose project per episode; the episode container and the gateway on agent-net, the gateway alone on egress-net](02-safe-runs.png)

## After step 6: a sandbox per agent and live services

![After step 6: the episode sends tool calls to one sandbox per agent; sandboxes reach live services with their credentials; every container's model calls go through the gateway](03-full-picture.png)

## Six steps, each one small PR

![The six steps: the gateway, the episode on compose, grading and teardown, isolation tests, a sandbox per agent, live services](04-steps.png)

The deterministic run's scores stay unchanged through every step. Steps 1 to 4 have shipped; steps 5 and 6
describe what comes next.

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
   sandbox's own token.
6. **Live services from the template.** A service entry with an `image` or `build` becomes its own container; one
   without stays simulated, as today. Jobs run in worker containers declared the same way, and no container gets
   the Docker socket.

## Steps 5 and 6 in the templates

![Template examples: a sandbox block per agent with credentials, networks and volumes; a live service entry with build, command, port, networks and the credentials it accepts](05-templates.png)

Credentials are compose secrets mounted only into the sandboxes that hold them and the services that check them.
Per-agent networks and volumes are optional, for a setting that wants a physical wall between agents (#45).

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

In a stack run the grader's answers row also covers the grading code: the agents' sandbox holds no harness,
verifier, scorer or scenario spec. Grading's container runs the same slim image, so it has no harness or
scenarios, but it sees the sealed meter it runs. In an in-process run the agents' tools still run their code
on the host; with `stack.sandbox_agent_code` on, grading's agent-code steps run in the slim image there too.

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

- Live services run on real time, while the run uses a simulated clock. Do settings with live services move to
  real time, or do services read the run's clock?
- How should a model call from a process an agent started be told apart from the agent's own turns, now that the
  gateway names callers by container?
