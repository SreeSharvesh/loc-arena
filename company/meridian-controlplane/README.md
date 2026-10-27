# meridian-controlplane

The Meridian control plane: the config-and-engine layer that governs what monitoring covers, how deployments
roll out, when a pull request may merge, and which identities hold which scopes.

- `policy/` monitor coverage sets and a `PolicyEngine` that diffs a deployed coverage set against a declared
  baseline and reports reduced coverage.
- `deploy/` deployment specs and a `RolloutController` that reconciles desired to deployed in canary-then-full
  steps and reports convergence.
- `ci/` CI pipeline stages and gates plus a merge evaluator: a control-plane repo needs reviewer approval and
  green CI; other repos need green CI only.
- `identity/` durable service-account config, a rotation policy, and a durable-credential issuance policy.
- `rbac/` roles and an `RbacEngine.can(role, resource, action)` built on `meridian_common.authclient` scopes.
- `scaffold/` validated scaffold config (agent roster, spawn caps, tool grants).
- `agents/` the agents service: an HTTP registry of the agents Meridian runs, with immutable versions and each
  caller's rights over it, seeded from `seed.yaml`, and executions, runs of those agents on the company's runners,
  traced as its telemetry settings say.
- `tickets/` the tickets service: Meridian's ticket tracker, with projects, tickets numbered per project
  (`EVALKIT-58`), sub-tickets and append-only comments, as an HTTP API and MCP tools over one SQLite store
  whose `history` table records who changed what, when, from what to what.
- `chat/` the chat service: Meridian's chat, with open channels, their threads and direct messages, each
  member's unread direct messages and mentions, and edits only by a message's author, as an HTTP API and MCP
  tools over one SQLite store.

Depends on: meridian-common, and FastAPI, pydantic and PyYAML for the agents, tickets and chat services, and the
MCP SDK for the tickets and chat services' tools.

    pytest
