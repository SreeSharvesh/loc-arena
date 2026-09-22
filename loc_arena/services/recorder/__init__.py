"""Sealed recorder and networkless evidence-reader.

Enforces the sealed-vs-tamperable isolation and fingerprinted, append-only logging.

Owns the ground-truth append-only event and generate log on a volume mounted only into the recorder and the
evidence-reader, on ``sealed-net`` (the agent is not a member). The host grader reads the sealed logs
through the networkless, read-only evidence-reader after the agents are frozen.
"""
