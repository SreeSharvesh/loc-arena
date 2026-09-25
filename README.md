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

## Per-agent logs

Every run bundle (`logs/<run_slug>/<run_name>/`) also holds a per-agent view of the episode:

| File | What it shows |
|---|---|
| `<run_name>.eval` | a real Inspect log: one sample for the graded run and one for the honest twin, one span per agent with its turns nested inside; open with `inspect view --log-dir <bundle>` (`make view` prints the command) |
| `transcript.html` / `.txt` | one column per agent plus World, one row per round: each agent's prompts, replies, tool calls and the events its turns caused |

Set `logging.agent_transcript: false` in a run config to skip them. How attribution works: `loc_arena/logging_/spec.md`.

## Documentation

The full walkthrough is a five-chapter course, served by GitHub Pages:

**https://sreesharvesh.github.io/loc-arena/**
