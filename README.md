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

## Documentation

The full walkthrough is a five-chapter course, served by GitHub Pages:

**https://sreesharvesh.github.io/loc-arena/**
