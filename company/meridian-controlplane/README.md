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

Depends on: meridian-common.

    pytest
