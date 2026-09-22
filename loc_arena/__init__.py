"""LOC-Arena: an AI-control evaluation setting for internal rogue-deployment detection.

This package is the standalone Inspect-AI harness plus multi-agent scaffold that runs an untrusted agent
team on real R&D tasks and measures whether the platform's own control points catch an attempt to establish
a rogue internal deployment. It owns nothing on its own; every submodule enforces one or more of the eight
invariants that keep the evaluation sealed, auditable, and reproducible.
"""
