"""What the engine gives the company's forge (``meridian_forge``, a platform package agents never see).

Enforces the sealed-vs-tamperable isolation (the forge and the whole seeded world are AGENT-FACING and
tamperable, never sealed: nothing here writes the sealed layer or holds an oracle value), config over code
(the hosted repos, the ticket count, and the world overlay come from config and the scenario pack), and
reproducibility from config plus seed (the world is a pure function of ``(config, seed)``, so it is identical
across an attack run and its honest twin). The subpackage owns: ``world`` (the deterministic world generator),
``reviewer`` (the trusted control-plane reviewer's judgment, the forge's reviewer), and ``service`` (the forge
as a live service under IAM, and in memory for an in-process run). Recording stays at the caller boundary (the
service's record for agent calls, the NPC drivers for coworkers and reviewers) so every forge action is a
scoped, fingerprinted event exactly like any other.
"""
