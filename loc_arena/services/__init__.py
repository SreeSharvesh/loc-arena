"""Per-episode docker service apps.

Enforces the sealed-vs-tamperable isolation at the network + volume layer.

Owns the small container apps that make up one episode's compose stack. Sealed services (recorder,
evidence-reader) sit on a network the agent is not a member of, with volumes mounted only into them;
tamperable services (execution, cluster, forge, weights, siem) are agent-reachable.
"""
