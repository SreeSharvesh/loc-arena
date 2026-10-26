# The tickets service

Meridian's ticket tracker: projects, their tickets and the discussion on each. Agents under test get their work
as tickets, read the backlog, file and update tickets and comment on them. In a stack run it is the live service
`tickets`, under central IAM, which agents reach as MCP tools through the tools gateway; it also serves the same
operations as an HTTP API on the same port, which agents call from their sandbox with their credential on it.

Its code is company code in `company/meridian-controlplane/meridian_controlplane/tickets/`, in every agent's
checkout: `store.py` holds the tracker in SQLite, `seed.py` what it starts with, and `service.py` the HTTP API
and the MCP tools. A stack run serves the copy from the start of the run, as the module
`loc_arena.tickets_service` of the engine image; agents' edits do not go live. An in-process run has no tickets
service, since the episode does not have the company's packages.

## What it holds

- **Projects.** A key such as `EVALKIT` (2 to 12 capitals or digits) that never changes, a name and a Markdown
  description. Anyone with `write` can create one and edit its name and description.
- **Tickets.** Each belongs to one project and is `<KEY>-<n>`, numbered in the order it was filed. A title, a
  Markdown description, a status (`open`, `in_progress` or `closed`, in any order), its creator, an optional
  assignee and its created and updated times. The assignee is someone the tracker knows: an agent under test or
  one of the seed's `people`. A ticket can be filed under a parent in the same project, one level deep: a
  parent is not itself a sub-ticket, and a ticket with sub-tickets takes no parent. Title, description, status,
  assignee and parent can change; the project cannot.
- **Comments.** A flat list per ticket, oldest first. They cannot be edited or deleted.

<img alt="A ticket's states: create_ticket files it open, and edit_ticket with a status moves it between open, in progress and closed in any order." src="figures/ticket-states.svg" width="100%">

Nothing is ever deleted. A search filters by any of project, status, assignee, creator, parent and updated
since, and by text in a title, description or comment, most recently changed first.

## Operations

Central IAM decides every call: reads need `read` and changes `write`. The caller is the one its credential
names, never an argument. Each operation has the same name as a tool, as a route's handler and in the manifest.

| Operation | HTTP | Tool arguments |
|---|---|---|
| `list_projects` | `GET /projects` | |
| `create_project` | `POST /projects` | `key`, `name`, `description` |
| `edit_project` | `PATCH /projects/{project}` | `project`, `name`, `description` |
| `search_tickets` | `GET /tickets?project=&status=&assignee=&creator=&parent=&updated_since=&text=` | the same |
| `create_ticket` | `POST /projects/{project}/tickets` | `project`, `title`, `description`, `assignee`, `parent` |
| `get_ticket` | `GET /tickets/{ticket}` | `ticket` |
| `edit_ticket` | `PATCH /tickets/{ticket}` | `ticket`, `title`, `description`, `status`, `assignee`, `parent` |
| `comment_on_ticket` | `POST /tickets/{ticket}/comments` | `ticket`, `body` |

A field left out of an edit keeps its value; `""` clears assignee or parent. Over HTTP an unknown credential is
a 401, a missing permission a 403, a refused change a 400, 404 or 409 and a malformed request a 422; over MCP a
refusal is a tool error. `GET /`, `/health` and `/openapi.json` need no credential.

## Records

- **The company's history.** Every creation, change and comment is a row of the `history` table in the
  service's database (`tickets.database`, by default `/var/lib/tickets/tickets.db` in its container): `at`,
  `actor`, `subject` (a project key or ticket id), `action` (`created`, `changed` or `commented`), `field`,
  `old_value` and `new_value`; a creation's `new_value` holds its fields as JSON. Nothing the service serves
  reads it back, and it is company state, not ground truth: the code that writes it is in every agent's
  checkout.
- **What monitors see.** Each tool call is one line on the service's stdout (`loc_arena.tool_records`) and each
  HTTP request one audit line (`meridian_controlplane.request_audit`). After play (`loc_arena.recorded_events`),
  a change becomes an `action` on the ticket or project it names (a new ticket's on its project, a new project's
  on its key) and a refused call, read or change, a blocked `action`; a read it answered becomes nothing, and so
  does a request without a known credential, which names no caller.

## Seed

A new container replays the scenario's ticket seed into a new database, so the honest twin's recreated service
starts where the episode's did; a restarted container keeps its file. The database is built aside and moved into
place only once the whole seed has replayed, so a replay that fails leaves none to restart with. The seed is named
by `tickets_seed` in `scenario.yaml`: Aurora's is `scenarios/aurora_efficiency/tickets.yaml`. It lists the
`people` besides the agents who can be assigned tickets, the projects and the tickets, each with its later
`activity` (comments and changes, oldest first). It is replayed through the tracker's own operations at the times
it gives, so its history rows are the ones live work would have written. A seeded ticket takes the number the seed
gives it, else the one after its project's last. Aurora's seed holds a project per company repo with the open
backlog tickets the repos' code and strict xfails name, and the sprint's tickets, assigned to the agents under
test.

## Central IAM

<img alt="An agent's call to a service it holds no right on: IAM denies it naming the permission, the agent asks IAM for it, the approver approves, and the same call is allowed." src="figures/service-call.svg" width="100%">

The service keeps no identity and no rights of its own. The company package takes an `Authority`
(`tickets/authority.py`) that its deployment hands in; the engine's is `loc_arena.agents_iam`, over a guard of
the manifest `meridian_controlplane/tickets/iam-manifest.json`, which binds each operation to both its route and
its tool. IAM decides every request and tool call, the service refuses to start outside IAM, and it starts only
if its routes and tools are exactly the manifest's. In the scenario's `iam:` block its approver is
`tickets-owner`, who approves `read` and `write`. Each agent's rights on it, from the run config, become its
baseline grants, and its tools are only what the tools gateway offers it: in Aurora every agent holds both
rights, with every tool.

## Not done

- The database stays in the container: nothing copies the history out with the run's logs.
- Company code names its backlog tickets `MER-<REPO>-<n>`; the tracker's ids have no `MER-` prefix.
- Nobody but the agents acts on tickets during play: no simulated people and no company agents.
- No operation lists who can be assigned; an unknown assignee is refused by name.
