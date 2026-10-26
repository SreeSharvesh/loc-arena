# The chat service

Meridian's chat: channels, their threads and direct messages, the one channel between agents and between them
and the company's people. Agents read it when they choose; nothing is pushed into their turns. In a stack run it
is the live service `chat`, under central IAM, which agents reach as MCP tools through the tools gateway; it
also serves the same operations as an HTTP API on the same port, which agents call from their sandbox with
their credential on it.

Its code is company code in `company/meridian-controlplane/meridian_controlplane/chat/`, in every agent's
checkout: `store.py` holds the chat in SQLite, `seed.py` what it starts with, and `service.py` the HTTP API and
the MCP tools. A stack run serves the copy from the start of the run, as the module `loc_arena.chat_service` of
the engine image; agents' edits do not go live. An in-process run has no chat, since the episode does not have
the company's packages.

## What it holds

- **Channels.** A name such as `evalkit` (lowercase letters, digits, `-` and `_`) and a topic. Every channel is
  open to everyone. Channels come from the seed; no operation creates one.
- **Messages.** Each has an id, given in the order it was written, its author, its Markdown body and its created
  time, and its edited time once edited. A message in a channel starts a thread or replies to one, one level
  deep; a read gives a thread's first message with its number of replies.
- **Direct messages.** From one member to another, with no thread. Only those two read it: no read or search
  gives it to anyone else. The recipient is someone the chat knows: an agent it was opened with or one of the
  seed's `people`.
- **Read positions.** Per member, the last message its `unread` covered.

`@name` in a channel message mentions that member. Nothing is ever deleted.

## Operations

Central IAM decides every call: reads need `read` and changes `write`. The caller is the one its credential
names, never an argument. Each operation has the same name as a tool, as a route's handler and in the manifest.

| Operation | HTTP | Tool arguments |
|---|---|---|
| `list_channels` | `GET /channels` | |
| `read_channel` | `GET /channels/{channel}/messages?thread=&before=` | `channel`, `thread`, `before` |
| `post` | `POST /channels/{channel}/messages` | `channel`, `body` |
| `reply` | `POST /messages/{message}/replies` | `message`, `body` |
| `send_dm` | `POST /dms/{member}` | `member`, `body` |
| `read_dms` | `GET /dms?member=&before=` | `member`, `before` |
| `unread` | `GET /unread` | |
| `search` | `GET /messages?text=&channel=&author=` | the same |
| `edit_message` | `PATCH /messages/{message}` | `message`, `body` |

- `read_channel` gives a channel's latest 50 threads, oldest first, or with `thread` the latest 50 messages of
  that thread; `before` reads further back. `read_dms` gives the caller's latest 50 direct messages, sent and
  received, or only those with `member`.
- `unread` gives the direct messages to the caller and the channel messages mentioning it, written since its
  last `unread`, oldest first and without its own, and moves its read position past every message so far. A
  member's first `unread` covers the whole seed. It needs `read`: the position it moves is the caller's own.
- `search` finds text in a body, ignoring case, in every channel and the caller's own direct messages, newest
  first, at most 50.

The service's one check of its own: only a message's author can edit it, and anyone else is refused with a 403
naming the author. Over HTTP an unknown credential is a 401, a missing permission a 403, a refused change a 400
or 404 and a malformed request a 422; over MCP a refusal is a tool error. `GET /`, `/health` and
`/openapi.json` need no credential.

## Records

- **The company's history.** An edit keeps the text it replaced as a row of the `edits` table: `message`,
  `replaced` and `edited_at`. Nothing the service serves reads it back, and it is company state, not ground
  truth: the code that writes it is in every agent's checkout.
- **Each HTTP request** is one audit line (`meridian_controlplane.request_audit`) naming its caller, its
  operation and its arguments, and each tool call one line of the tool-call record.
- **What monitors read.** After play, each `post`, `reply` and `send_dm` a member made, over MCP or HTTP, is a
  `message` event (`loc_arena.recorded_events`): its author, its channel or recipient, its thread, whether it is
  a direct message, and its body. Which of them a monitor reads is set in `configs/monitors.yaml` by its `tap`
  and `scope`. An `edit_message` is an action.

## Seed

A stack run's seed is named by `chat_seed` in `scenario.yaml`: Aurora's is `scenarios/aurora_efficiency/chat.yaml`,
and every agent of the run is a member. Its database is `chat.database` in the run config's settings. A new
database is built by replaying a chat seed, aside, and moved into place only once the whole seed has
replayed; an existing one is opened as it is. A seed lists the `people` in the chat besides the given agents,
the channels, each with its threads and their replies, and the `direct_messages`. It is replayed through the
chat's own operations at the times it gives, in the order it lists them: channel by channel, then the direct
messages, so message ids follow that order and reads order by time.

## Central IAM

The package takes the tickets service's `Authority` protocol (`tickets/authority.py`), which a deployment hands
in. Its manifest is `meridian_controlplane/chat/iam-manifest.json`, binding each operation to both its route
and its tool; `post` and `read_channel` target the channel, `reply` and `edit_message` the message, and
`send_dm` the member.

## Not done

- No operation lists the members; a direct message to someone not in the chat is refused by name.
- No operation creates a channel, and a seed cannot replay an edit.
