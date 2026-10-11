# LOC-Arena

LOC-Arena is an AI-control evaluation setting: a simulated frontier-lab autonomous AI R&D platform where an
untrusted agent and its sub-agents do real research-engineering work on a seven-repo company codebase and may
attempt to establish an internal rogue deployment (running the model off the sanctioned, monitored path). Every
episode measures two things and reports the gap between them: whether a rogue deployment actually happened (a
sealed, deterministic ground-truth verifier the agent cannot reach) and whether the platform's own control points
caught it (configurable in-scenario monitors plus deterministic reconciliation detectors). The gap between those
two, under an adversarial agent, is the science.

This is defensive AI-safety research infrastructure. Attack chains are represented as control-plane state
transitions plus their programmatic verifiers, never as operational procedures.

## How it runs

Each agent runs in its own sandbox with its harness's native tools and an MCP client. Everything else it can do
to the company (open a PR, message a teammate, submit a job) is a tool on a service behind one MCP route,
agentgateway, and the run config's `sandbox: tools:` decides exactly which tools each agent gets. Every model call
goes through a gateway that alone holds the key. Every service records each call made to it, and after play those
records become the events the monitors and the verifier read. The honest twin plays after the attack on services
and sandboxes recreated empty. Details: [`docs/isolation/`](docs/isolation/README.md) and
[`docs/run-config.md`](docs/run-config.md).

```sh
make setup                    # check uv, sync dependencies, validate the configs
make run MODE=attack STACK=1  # live models; the episode in a container with no key; the key in .env
```

## Per-agent logs

Every run bundle (`logs/<run_slug>/<run_name>/`) also holds a per-agent view of the episode:

| File | What it shows |
|---|---|
| `<run_name>.eval` | a real Inspect log: one sample for the graded run and one for the honest twin, one span per agent with its turns nested inside; open with `inspect view --log-dir <bundle>` (`make view` prints the command) |
| `transcript.html` / `.txt` | one column per agent plus World, one row per round: each agent's prompts, replies, tool calls and the events its turns caused |

Set `logging.agent_transcript: false` in a run config to skip them. How attribution works: `docs/agent-log/spec.md`.

## Run explorer

`make explore` builds one page per run under `logs/` (or `LOGS=<folder>`) plus an index of them all, and opens it:
every agent side by side with each monitor's verdict on the step it judged, who read each message and scratch
note, the covert steps the monitors missed, and notes you pin to any passage. One run:
`uv run python -m loc_arena.cli explore <bundle>`. What each view shows, with pictures:
[`docs/explorer/`](docs/explorer/README.md).

## Documentation

The full walkthrough is a five-chapter course, served by GitHub Pages:

**https://sreesharvesh.github.io/loc-arena/**
