"""Execution sandbox service (tamperable).

Enforces the sealed-vs-tamperable isolation.

Owns the per-agent execution sandbox that runs agent code and hosts the in-sandbox inference client. On
``agent-net``/``control-net``; resource-limited so a runaway process fails one episode, not the batch.
"""
