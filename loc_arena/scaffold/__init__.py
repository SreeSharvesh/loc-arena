"""What an episode gives its agents' sessions to work with.

Owns the dual-capture recorder (every observable event goes to the sealed and the mirror log) and the code
tools over each agent's checkout, which the workspace service serves. A call to a service is recorded by the
service, and its events are built after play.
"""
