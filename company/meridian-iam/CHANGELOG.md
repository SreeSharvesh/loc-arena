# Changelog — meridian-iam

## Unreleased
- requests: a request for ongoing access a resource does not allow, or past its deadline limit, is told to
  send `until` as a unix time within the resource's `max_seconds`, so it can ask again correctly.
- requests: filing says the approvers decide from their own inbox and the outcome reaches the requester's, and
  no longer tells the requester to ask them.
- integration: an MCP service wrapped with `answer_outages` answers 503 with a JSON-RPC error when
  `IamVerifier` cannot reach IAM, not 401: an outage is not a bad credential, and an MCP client shows why.
- service: `GET /` describes the API to anyone: how to see access, ask for more and decide requests.
- service: a malformed request's 400 names each bad field and its problem (`body.reason: missing`) but never
  what it held, not even an unexpected key's name; the stdlib client keeps the full list on the error.
- requests: a decision's answer names the requester, so its audit line joins to the request it decides.
- audit: a refused mutation's line says what it tried, each field cut to 500 characters; a caller that never
  authenticated adds nothing of its own.
- requests: `inbox` (and `GET /inbox`) lists the pending requests the caller may decide and the outcomes of
  its own; an `iam-inbox` credential reads it and nothing else. A decision's reason is at most 500 characters,
  stripped like a request's, and a denial grants nothing, so it takes no permissions or deadline.
- requests: filing answers with the `approvers` who can decide the request now (active approvers holding
  any of it, never the requester) and a `next` step saying whom to ask, or that nobody can approve it now.
- service: a body IAM cannot read as a JSON object (most often JSON sent without `Content-Type:
  application/json`) is answered with how to send it.
- service: an MCP tool, `access_inbox`, reads the caller's inbox; it admits only a credential that may read an
  inbox (`iam-inbox` or `iam`) and takes no argument. IAM serves it at `/mcp` beside its HTTP API.

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
