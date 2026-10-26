# Working on central IAM

How to change `company/meridian-iam/` and how to put a service under it. The contract is
[`spec.md`](spec.md); this is the practical side.

## Where things are

| Path | What it holds |
|---|---|
| `company/meridian-iam/meridian_iam/contracts/` | the operation manifest, the `Decision`, the 401/403/400/409/503 error types |
| `company/meridian-iam/meridian_iam/core/` | the SQLite store and the `Iam` class: every rule about principals, credentials, requests and grants |
| `company/meridian-iam/meridian_iam/service/` | the FastAPI app over `Iam`; handlers are `async` so one event loop serializes the store |
| `company/meridian-iam/meridian_iam/client.py` | the stdlib client a service uses; stdlib so a service image needs nothing extra |
| `company/meridian-iam/meridian_iam/integration/` | `Guard` (one decision per call), `asgi.py` (FastAPI dependency), `mcp.py` (verifier and tool middleware) |
| `loc_arena/iam_service.py`, `iam_bootstrap.py`, `iam_seed.py`, `iam_wiring.py` | the launcher, the per-play seed document, the scenario's `iam:` block, how a service joins by environment |
| `loc_arena/agents_iam.py` | the engine's implementation of the agents service's `CentralAuthority`, and its `IdentityIssuer`, which has IAM issue each registered agent and each schedule a workload |
| `company/meridian-controlplane/meridian_controlplane/notes/`, `loc_arena/notes_service.py` | the notes service, under IAM |

## Rules that are easy to break

- **`meridian-iam` is not one of the work repos.** `COMPANY_REPOS` excludes it, so it is never copied into the
  agents' checkout or graded. Anything in a work repo (such as `meridian-controlplane`) must not import it; the
  engine passes an implementation in instead. A test in the agents' own suite would fail to import it otherwise.
- **Engine modules the episode imports must not import `meridian_iam`.** The episode container has no company
  packages on its path. Only `loc_arena/iam_wiring.py` reaches them, and only when a service starts.
- **Authority is per resource.** A member's standing authority maps resource to permissions; owning a team that
  owns two resources gives no authority over one by virtue of the other.
- **A resource service holds a resource-bound credential.** An agent's credential on a managed service has the
  audiences `<service>` and `iam:<service>`; only its own `iam` credential manages everything. Never widen it to
  `iam` for convenience: that lets a compromised service act for the agent on every other resource.
- **A service delegates only what its seed lists.** Issuing workloads needs a manifest operation marked
  `launch` and an `allow_delegation` for the issuing service. Never widen a delegation for convenience: a
  compromised issuer can mint everything listed for any caller who holds it.
- **Fail closed.** An unavailable or inconsistent IAM is a 503 that runs nothing, never a missing grant.
- **A denial records nothing granted.** `decide` stores no permissions or deadline for a denial, and every
  listing reads an empty decision as nothing approved; that holds only while an approval is never empty.

## Building a service on IAM

Every agent-facing service must be guarded. `iam.enabled` is on unless a config turns it off, a config with it on
plays in a stack, a stack run refuses a config with it off, and a config whose scenario lets agents reach a
service outside its `iam:` block does not load. No service keeps rights of its own, and each refuses to start
outside IAM. A service that starts work for its caller (a
job, an eval run, an agent run) also issues that work an identity of its own. A service calling another
service is not specified yet.

| The service | It does | Status |
|---|---|---|
| is guarded | declares its operations, asks IAM before each protected call, runs only on allow | built in IAM mode: agents (HTTP); jobsvc, evalkit, tickets, notes, chat (HTTP and MCP); forge, jobs, coverage (MCP) |
| launches work | also issues each piece of work a workload principal with a subset of its caller's rights | built: jobsvc issues one per job and one per schedule; agents one per registered agent and one per schedule |
| calls another service | acts at another resource for its caller | not specified: do not reuse a caller's credential or share a secret |

### Every service

1. **Declare.** Write the manifest (`schema_version`, `resource`, `operations`, `http` and/or `tools`).
   `resource` equals the service's name in the scenario. Every route and tool maps to exactly one operation,
   and each operation is exactly one of: a `permission`, `public: true`, or `authenticated: true` (any valid
   credential, no permission). The seed grants each agent its run-config rights alone, and a tool it is offered
   takes the permission its operation checks; a tool of an `authenticated` operation, such as the job service's
   `cancel_job`, needs none. A covert or unrequestable capability is `requestable: false`.
2. **Join.** For a service named in the scenario's `iam:` block, in a run with IAM on (the default),
   the renderer sets `LOC_ARENA_IAM_URL`, `LOC_ARENA_IAM_MANIFEST`, `LOC_ARENA_IAM_CREDENTIAL`,
   `LOC_ARENA_IAM_TIMEOUT_SECONDS` and `LOC_ARENA_IAM_RETRIES` (`iam_wiring.service_environment`); a service
   missing one, or with a timeout or retries outside the run config's bounds, does not start. The credential is
   the service's own: it carries the `iam` audience that `authorize` and `issue_workload` need, and
   its principal is bound to the service's resource, so it decides for that resource only. An engine-module
   service builds its `Guard` once with `iam_wiring.guard_from_environment` and hands it to both `iam_options`
   and `build_service_app`; a service built from a scenario directory has no `loc_arena`, so its `Dockerfile`
   copies in `meridian_iam` from the `meridian-iam` build context the stack passes.
3. **Check at startup.** FastAPI: `integration.asgi.verify_app(guard, app)`, which also refuses a route
   without the guard's dependency. MCP: `iam_wiring.build_service_app`. Both on one port (the job service):
   `build_service_app(guard, server, service, api)`, which checks the routes and the tools together against
   one manifest. Stdlib: `Guard.verify_routes(routes)`. A route you forgot, or one you removed, stops the
   service.
4. **Enforce before work.** FastAPI: `integration.asgi.dependency(guard)` on every route; it answers each
   refusal itself (401, 403 with the guidance, 503 when IAM is down). MCP: IAM verifies the credential of
   every request, so an outage is a 503 with a JSON-RPC error saying so (`build_service_app`), and a refusal of
   the tool is a tool-error result (`iam_options`); no tool runs either way. Stdlib:
   `Guard.check_http` raises, so the server maps `Unauthenticated` to 401, `AccessDenied` to 403 with its
   `guidance`, and `Unavailable` to 503 with nothing run. Handlers take the principal from the decision, never
   from the body, and record its `decision_id` beside the outcome.
5. **Seed.** Name it in the scenario's `iam:` block with its manifest, owner policy (`owner_team`,
   `approver_role`, `max_seconds`, `allow_ongoing`) and approver: `approver: {principal: <service>-owner,
   approves: [...]}`, a principal of the company that IAM makes a member of the owner team. It is required,
   never an agent under test, and approves only permissions the manifest makes requestable; the config does not
   load otherwise. Each agent's run-config rights on the service become baseline grants, and a run config
   whose rights name a permission the manifest never checks does not load. Its tools grant nothing: the tools
   gateway offers them, and a call takes the permission its operation checks. A permission nobody may request, such as an operator's, is held by
   a principal of the company the entry names in `holders: {<principal>: [...]}` (the job service's `releng`
   holds `operate`): never an agent of the run or a service's own principal, and only permissions the manifest
   checks.
6. **Let agents ask.** A denied caller's refusal (a 403 body over HTTP, a tool error over MCP) names the
   resource, the permission and `POST /requests` on IAM when the operation is requestable. Filing answers with
   the `approvers` who can decide it now and a `next` step: ask one of them, who decides with
   `POST /requests/<id>/decision`, and the caller retries and is checked again. The service's approver
   (`loc_arena.iam_approvers`, running in the IAM service) reads its inbox every `iam.approver_poll_seconds`:
   it approves a request when it approves every permission asked, and denies the rest, saying what it
   approves; the requester sees the outcome in its inbox. On an MCP tool service an approval makes callable
   the tools the gateway already offers the agent (its `sandbox: tools:`), and nothing else. Do not add a local
   queue.
7. **Test** the way `tests/unit/test_tickets_iam.py`, `test_agents_iam.py` and `test_mcp_iam.py` do: a real IAM
   server, real SQLite, and the cases in spec section 10. Break each new protection once and watch its test fail.

### A service that launches work

1. **Mark the operation.** The operation that starts work is `launch: true` in the manifest, names a
   permission and has no target. Only a caller holding that permission can launch.
2. **Seed what it may hand on.** `Iam.allow_delegation("<its resource>", {resource: [permission]})`, named by
   the service's resource (not its `<name>-service` principal), is a trusted seed call listing every
   permission its workloads may get, its own resource included only if listed. The scenario's `iam:` entry for
   the service lists it as `delegation` (`ResourceSeed`), which `render_bootstrap` carries and
   `iam_service.seed_iam` seeds once every resource is registered. A delegation on a service the seed does not
   manage, or of a permission that service never checks, or from a service whose manifest marks no operation
   `launch`, stops the config loading.
3. **Make the work visible.** A workload calls as `job-<id>`, which is no agent of the run:
   `loc_arena/recorded_events.py` makes each record a workload made an event of its sponsor, from IAM's
   `workload_issued` audit line, which names both, and the event names the `workload`.
4. **Issue per piece of work**, before starting it. `caller_token` is the bearer the caller presented (over
   HTTP the `Authorization` header; in an MCP tool, the SDK's `get_access_token()`). Ask IAM what the work could
   get, which is what the service may delegate and the caller holds now, and issue exactly that:

   ```python
   grants = guard.client.delegable(caller_token, "submit_job")  # {"notes": ["read"]}
   secrets_by_audience = {resource: secrets.token_urlsafe(32) for resource in grants}  # one per audience
   workload = guard.client.issue_workload(
       caller_token,
       "submit_job",
       reference=job_id,
       purpose="nightly eval of run 12",
       grants=grants,
       ttl=3600,
       credentials=secrets_by_audience,
       key=job_id,
   )
   ```

   IAM refuses unless the caller holds the launch permission, every grant is delegable and held by the caller
   now, `ttl` is within every granted resource's `max_seconds`, and the secrets are new and cover exactly the
   audiences of the granted resources (never `iam`). Each grant ends at the earlier of `ttl` and the caller's
   grant it copies. On a lost reply, retry with the same `key`, caller credential and secrets: a new key with
   the same secrets is a conflict (409), and a new key with new secrets issues a second workload.
5. **Hand each secret to that work only.** The work presents its own secret to each service; it never gets the
   caller's credential. Keep the secrets in memory until IAM answers and the work has them; never return, log
   or write them to disk. IAM keeps only digests.
6. **Expect it to stop.** Revoking the caller's grant that the workload copied denies the workload's next
   call (revoking only the caller's launch permission does not), and disabling the caller or the issuing
   service stops its credential authenticating; stop the work on that 401 or 403. Nothing stops work already
   running or ends a workload when its job finishes yet.
7. **Test** the refusals `company/meridian-iam/tests/test_workloads.py` checks on the core (a caller without
   the launch permission, a grant outside the delegation, a grant the caller lacks, a revoked or disabled
   caller, a disabled issuer, a retry under the same key with different content) through a real IAM server, as
   `tests/unit/test_tickets_iam.py` does for guarding.

## Checks

`uv run pytest company/meridian-iam -q` for the package; `tests/unit/test_iam_compose.py`, `test_iam_seed.py` and
`test_iam_launcher.py` for the rendering, the seed and the launcher; `tests/integration/test_iam_docker.py` (needs
Docker) for real containers. `make gates` runs the rest.
