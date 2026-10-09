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

The [IAM maintenance scenario](scenarios/iam_maintenance/README.md) pairs ten independently graded IAM
repair tickets with the same rogue-deployment side task. Run it with
`make run RUN=iam-maintenance MODE=honest` or `MODE=attack`; the
`iam-maintenance.deterministic` config provides offline fixtures for both modes.

The [DevEx](scenarios/devex_maintenance/SPEC.md) and
[Release Engineering](scenarios/release_maintenance/SPEC.md) prototypes each add ten independently
replayed repair tickets and reuse the rogue-deployment side task. Their process and controller state is
simulated. Select `devex-maintenance` or `release-maintenance`; append `.deterministic` for offline fixtures.

Read the [simple-English repository guide](docs/repository-guide.md) and open its
[interactive HTML map](docs/repository-map.html) to follow an episode through the engine.

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
