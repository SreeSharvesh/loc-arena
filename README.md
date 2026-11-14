# LOC-Arena

LOC-Arena is an AI-control arena. A team of untrusted agents does research engineering on a simulated AI lab's
codebase, and may try a rogue deployment: running a model off the lab's monitored path. Monitors try to catch it,
and a sealed verifier decides whether it happened.

<img alt="Data flow of one run: play writes the service records, both event logs, the per-agent trace and the gateway's sealed call record; the records become events after play, with turns from the call record; the verifier reads the sealed log and the call record, and the monitors the mirror log; live monitors' model calls are appended to the sealed log and to the gateway's call record; the run bundle is read by the explorer and inspect view." src="docs/figures/run-records.svg" width="100%">

*Read it top to bottom: play writes the records, the records become two logs, and the verifier reads the sealed one while the monitors read the mirror.*

This is defensive AI-safety research infrastructure. Attack chains are control-plane state transitions plus the
programmatic verifiers that check them, never operational procedures.

## What a run is

- Each agent works in its own sandbox and checkout, in a session of the pi coding agent, on a live model.
- Agents open pull requests, message teammates, submit jobs and deploy services, all through the company's services.
- Every model call goes through one gateway, and every service records each call made to it.
- After play, those records become the events that the monitors and the verifier read.
- An honest twin then plays the same task without the covert brief, which sets the audit threshold.
- The run reports whether a rogue deployment happened, whether the monitors caught it, and the main-task score.

## Set up and run

```sh
make setup                            # check uv, sync dependencies, validate the configs
make run RUN=aurora-efficiency.fast   # the smoke run: an attack and its twin on the cheapest models
make run MODE=attack                  # a full run on live models
make view                             # open the latest run's report, from logs/
```

A run needs Docker and `OPENROUTER_API_KEY` in `.env`. On a cloud development container, read
[its three steps](docs/isolation/README.md#on-a-cloud-development-container) first.

## Where to read more

| Page | What it answers |
|---|---|
| [Course](https://sreesharvesh.github.io/loc-arena/) | the walkthrough: the arena, its setting, monitoring, and how to extend it |
| [`AGENTS.md`](AGENTS.md) | where code lives, the engine and the setting, and the rules for contributors |
| [`CHANGELOG.md`](CHANGELOG.md) | what changed and when: one dated release per merged pull request, newest first |
| [`docs/run-config.md`](docs/run-config.md) | every key and setting of a run config |
| [`docs/isolation/README.md`](docs/isolation/README.md) | how a run's containers isolate agents and wire their tools and services |
| [`docs/pi-harness.md`](docs/pi-harness.md) | how each agent's session runs in pi, the coding agent |
| [`docs/iam-service/spec.md`](docs/iam-service/spec.md) | central IAM: identities, rights, access requests and workloads |
| [`docs/iam-service/agent.md`](docs/iam-service/agent.md) | how to put a service under IAM |
| [`docs/forge-service.md`](docs/forge-service.md) | the forge: git repositories, pull requests, merges and what is graded |
| [`docs/deploy-service.md`](docs/deploy-service.md) | the deploy service: how agents release merged code to a service |
| [`docs/jobsvc-service.md`](docs/jobsvc-service.md) | the job service: the cluster, its jobs and their credentials |
| [`docs/evalkit-service.md`](docs/evalkit-service.md) | the eval service: evals, runs, results and leaderboards |
| [`docs/agents-service.md`](docs/agents-service.md) | the agents service: the company's own agents and their executions |
| [`docs/modelstore-service.md`](docs/modelstore-service.md) | the model store: the weights an inference server downloads |
| [`docs/tickets-service.md`](docs/tickets-service.md) | the tickets service: the company's projects and tickets |
| [`docs/chat-service.md`](docs/chat-service.md) | the chat service: the one channel between agents |
| [`docs/notes-service.md`](docs/notes-service.md) | the notes service: the team's shared notes |
| [`docs/explorer/README.md`](docs/explorer/README.md) | the run explorer: every view of a run, with pictures |
| [`docs/run-reader.md`](docs/run-reader.md) | the run reader: an agent's report of what a run's agents did |
| [`docs/agent-log/spec.md`](docs/agent-log/spec.md) | per-agent logs: what the Inspect log and transcript guarantee |
| [`docs/agent-log/agent.md`](docs/agent-log/agent.md) | how to work on per-agent logging |
| [`docs/ci-rules.md`](docs/ci-rules.md) | the checks a pull request must pass |
| [`docs/inference-and-checker/README.md`](docs/inference-and-checker/README.md) | the plan for the company's inference servers and its model store |
| [`docs/live-landing/README.md`](docs/live-landing/README.md) | the plan for a side task a live run can land |
| [`docs/workload-identity/README.md`](docs/workload-identity/README.md) | the plan for a badge on every model call |
