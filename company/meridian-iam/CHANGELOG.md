# Changelog — meridian-iam

## 0.6.0
- workloads: a service issues the work its caller launches a principal of its own (`POST /workloads`,
  `IamClient.issue_workload`), through a manifest operation marked `launch`, with only the grants the seed lets it
  delegate (`allow_delegation`) and the caller holds, and one secret per audience. Every decision counts a
  workload's grant only while its issuer is live and its sponsor still holds the permission through a grant it
  had at issuance, so a revocation or a disable stops it and a re-grant does not revive it; a disabled sponsor or
  issuer also stops its credential authenticating.
- store: a store from an older release is upgraded in place when it opens.
- service: a malformed request body is answered 400 `invalid` without echoing it, rather than FastAPI's 422.

## 0.5.0
- Wired into the stack: an `iam` service, per-play bootstrap and credentials, and IAM audit lines joined to each
  resource's own record by decision ID. See `docs/iam-service/spec.md` section 11 for what is and is not under IAM.

## 0.4.0
- mcp: IAM verifies who connects to an MCP service and checks the named operation before every tool call; a refusal
  is an ordinary tool error the agent can read, still recorded. Each tool-call record can carry the IAM decision
  it was made under.

## 0.3.0
- service, client, integration: an HTTP API over the core, a stdlib-only client, and a guard that asks IAM online
  before any handler runs, with a FastAPI dependency and a stdlib-server adapter. A service's routes and its
  manifest must agree exactly at startup. IAM unavailable, or an answer that does not match the call, is a 503
  that runs nothing.

## 0.2.0
- core: a transactional SQLite store behind one `Iam` class: principals, teams with per-resource standing authority,
  credentials with generations, labels and renewal, access requests, approvals, direct grants, overlapping revocation,
  idempotency keys and an audit trail whose events fire only once their transaction commits.
- lifecycle: an agent renews its own credential with a secret it generates (the old one works for a grace window
  and can never replace the newer one), an approver adds members or widens their authority within the authority it
  holds (additively), a member may leave, and an operator disables a principal; the operator team is reserved.
- credentials: a credential bound to one resource (`iam:<resource>`) manages that resource and no other.
- seeding: a seed commits whole or not at all, and an explicit marker says it completed.
- requests: one request is fetched by ID, without the queue's row limit; an approver cannot grant to itself.

## 0.1.0
- contracts: a strict, immutable operation manifest (HTTP routes, MCP tools, targets) with a startup coverage check,
  the typed `Decision`, and the 401/403/400/409/503 failure types.
