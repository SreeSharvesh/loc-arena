"""Multi-agent scaffold package.

Owns the dual-capture recorder (every observable event goes to the sealed and the mirror log), the agent loop,
the tool layer with its native tools, and each agent's MCP client. A native action is logged here; a call to a
service is recorded by the service, and its events are built after play.
"""
