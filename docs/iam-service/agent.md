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
| `loc_arena/agents_iam.py` | the engine's implementation of the agents service's `CentralAuthority` |
| `scenarios/aurora_efficiency/services/notes/notes_iam.py` | the notes service under IAM |

## Rules that are easy to break

- **`meridian-iam` is not one of the work repos.** `COMPANY_REPOS` excludes it, so it is never copied into the
  agents' checkout or graded. Anything in a work repo (such as `meridian-controlplane`) must not import it; the
  engine passes an implementation in instead. A test in the agents' own suite would fail to import it otherwise.
- **Engine modules the episode imports must not import `meridian_iam`.** The episode container has no company
  packages on its path. Only `loc_arena/iam_wiring.py` reaches them, and only when a service starts.
- **The legacy path stays byte-identical** when `iam.enabled` is off (`tests/unit/test_episode_stack.py`).
- **Authority is per resource.** A member's standing authority maps resource to permissions; owning a team that
  owns two resources gives no authority over one by virtue of the other.
- **A resource service holds a resource-bound credential.** An agent's credential on a managed service has the
  audiences `<service>` and `iam:<service>`; only its own `iam` credential manages everything. Never widen it to
  `iam` for convenience: that lets a compromised service act for the agent on every other resource.
- **A service delegates only what its seed lists.** Issuing workloads needs a manifest operation marked
  `launch` and an `allow_delegation` for the issuing service. Never widen a delegation for convenience: a
  compromised issuer can mint everything listed for any caller who holds it.
- **Fail closed.** An unavailable or inconsistent IAM is a 503 that runs nothing, never a missing grant.

## Putting a service under IAM

1. Write its manifest (`schema_version`, `resource`, `operations`, `http` and/or `tools`). Every route and tool
   is bound to exactly one operation; a public operation says so; a covert or unrequestable capability is
   `requestable: false`. The manifest's `resource` equals the service's name in the scenario.
2. Check it at startup: `Guard.verify_routes(...)` against the real routes (FastAPI: `registered_routes(app)`) or
   tool names. A route you forgot, or one you removed, stops the service.
3. Enforce before work: the FastAPI dependency (`integration.asgi.dependency`), the MCP middleware, or
   `Guard.check_http` for a stdlib server. Handlers receive the principal from the decision, never from the body.
4. Name it in the scenario's `iam:` block with its owner policy. Its starting rights and tools in the run config
   become baseline grants; an agent holding `grant` there becomes its approver.
5. Test the way `tests/unit/test_notes_iam.py`, `test_agents_iam.py` and `test_mcp_iam.py` do: a real IAM server,
   real SQLite, and the cases in spec section 10. Break each new protection once and watch its test fail.

## Checks

`uv run pytest company/meridian-iam -q` for the package; `tests/unit/test_iam_stack.py` for the rendering and the
seed; `tests/integration/test_iam_docker.py` (needs Docker) for real containers. `make gates` runs the rest.
