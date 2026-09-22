"""Multi-agent scaffold package.

Enforces mandatory inference routing and fingerprinted logging of every action, spawn, and close.

Owns the scaffold-hosted message bus, the agent and orchestrator loops, the sub-agent registry and
lifecycle, and the scoped tool set. Every action dispatches through here so it is parsed by the monitor
before it runs and written to the sealed log.
"""
