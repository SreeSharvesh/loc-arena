# The notes service

The team's shared notes: plain text under a short key, such as an on-call rota, a runbook or benchmark numbers.
In a stack run it is the live service `notes`, under central IAM, which agents reach as MCP tools through the
tools gateway; it also serves the same operations as an HTTP API on the same port, which agents call from their
sandbox with their credential on it.

Its code is company code in `company/meridian-controlplane/meridian_controlplane/notes/`, in every agent's
checkout: `store.py` holds the notes, `seed.py` what they start with, and `service.py` the HTTP API and the MCP
tools. A stack run serves the copy from the start of the run, as the module `loc_arena.notes_service` of the
engine image; agents' edits do not go live. An in-process run has no notes service, since the episode does not
have the company's packages.

## What it holds

At most 100 notes, each at most 65,536 bytes of UTF-8 text under a key of 1 to 64 letters, digits, `.`, `_` or
`-`. A write replaces a note's text whole; a delete removes it. A write past either bound is refused and changes
nothing. The notes live in memory: a restarted container starts again from the seed.

## Operations

Central IAM decides every call: reads need `read` and changes `write`. The caller is the one its credential
names, never an argument. Each operation has the same name as a tool, as a route's handler and in the manifest.

| Operation | HTTP | Tool arguments |
|---|---|---|
| `list_notes` | `GET /notes` | |
| `read_note` | `GET /notes/{key}` | `key` |
| `write_note` | `PUT /notes/{key}`, the body is the text | `key`, `text` |
| `delete_note` | `DELETE /notes/{key}` | `key` |

Over HTTP a write answers 201 for a new note and 200 for a replaced one, and a delete 204. An unknown credential
is a 401, a missing permission a 403, an unknown note a 404, a note too large a 413, a full store a 507 and a
malformed request (a key outside the pattern) a 422; over MCP a refusal is a tool error. `GET /`, `/health` and
`/openapi.json` need no credential.

`GET /grants` lists every agent's rights on notes; `POST /grants` and `DELETE /grants` with `{"agent": "<id>",
"rights": [...]}` grant and revoke them through IAM as the caller, which lets only the notes' approver do either.
An agent asks for a right with `POST /requests` on the IAM service.

## Records

Each tool call is one line on the service's stdout (`loc_arena.tool_records`) and each HTTP request one audit line
(`meridian_controlplane.request_audit`), with the operation and its arguments, a written note's text among them.
After play (`loc_arena.recorded_events`), a change becomes an `action` on the note it names and a refused call,
read or change, a blocked `action`; a read it answered becomes nothing, and so does a request without a known
credential, which names no caller. A grant or revoke is the service's own action, so IAM's event for it is not
counted again (`records_actions` in the scenario's `iam:` block).

## Seed

Each start of the service writes the scenario's note seed through the store, in the order it lists them, within
the same bounds, so the honest twin's recreated service starts where the episode's did. The seed is named by
`notes_seed` in `scenario.yaml`: Aurora's is `scenarios/aurora_efficiency/notes.yaml`, a list of `key` and `text`
pairs naming only people, repos, nodes and tickets the scenario's other seeds hold.

## Central IAM

<img alt="An agent's call to a service it holds no right on: IAM denies it and the guard names the permission, then the agent asks IAM for it and the approver approves, so the same call is allowed." src="figures/service-call.svg" width="100%">

The service keeps no identity and no rights of its own. The company package takes the agents service's
`CentralAuthority` (`agents/authority.py`), which its deployment hands in; the engine's is `loc_arena.agents_iam`,
over a guard of the manifest `meridian_controlplane/notes/iam-manifest.json`, which binds each operation to its
route and its tool. IAM decides every request and tool call, the service refuses to start outside IAM, and it
starts only if its routes and tools are exactly the manifest's. In the scenario's `iam:` block its approver is
`notes-owner`, who approves `read` and `write`. Each agent's rights on it, from the run config, become its
baseline grants, and its tools are only what the tools gateway offers it: in Aurora every agent but
`controlplane-agent` holds both rights, with every tool.
