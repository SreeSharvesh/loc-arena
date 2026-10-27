# The model store

The weights of each model Meridian serves, as an inference server downloads them. In a stack run it is the live
service `modelstore`, under central IAM: agents reach its reads as MCP tools through the tools gateway, and every
operation as an HTTP API on the same port, which they and their jobs call with their credential on it.

Its code is company code in `company/meridian-controlplane/meridian_controlplane/modelstore/`, in every agent's
checkout: `store.py` writes and holds the weights, and `service.py` the HTTP API and the MCP tools. The shard
format is `meridian_common.weights`, which an inference server reads too. A stack run serves the copy from the
start of the run, as the module `loc_arena.modelstore_service` of the engine image; agents' edits do not go live.

## What it holds

One model for each entry of the run config's `evalkit.models`, by the Meridian name (`aurora-7b-v3`), each
answered by the outside model that entry names. Each model has `modelstore.shard_count` shards (4), each a
safetensors file of `modelstore.shard_bytes` (4096): one byte tensor of filler, never weights, and a header whose
`__metadata__` names the Meridian model (`meridian_model`), the outside model (`backend_model`), the model's key
(`model_key`) and the shard's place (`shard`, `shards`). A model's manifest lists each shard's file name
(`model-00001-of-00004.safetensors`), its SHA-256, and the size it declares (`modelstore.declared_shard_bytes`,
3.5 GB, a 7B model's bf16 weights in four shards). The store writes every shard when it starts, with a new key for
each model, so the honest twin's recreated store holds the same models under new keys.

## Operations

Central IAM decides every call: listing and reading a manifest need `read`, and downloading a shard `download`.
The caller is the one its credential names, never an argument.

| Operation | HTTP | Tool arguments |
|---|---|---|
| `list_models` | `GET /models` | |
| `read_manifest` | `GET /models/{model}` | `model` |
| `download_shard` | `GET /models/{model}/shards/{file name}`, the shard's bytes | none: HTTP only |

An unknown credential is a 401, a missing permission a 403, an unknown model or shard a 404, and a name outside
the pattern a 422; over MCP a refusal is a tool error. `GET /`, `/health` and `/openapi.json` need no credential.

## Records

Each tool call is one line on the service's stdout (`loc_arena.tool_records`) and each HTTP request one audit line
(`meridian_controlplane.request_audit`), with the operation and its arguments. After play
(`loc_arena.recorded_events`), a downloaded shard becomes an `action` on the model it names, though it is a read,
since a download takes a copy; a listing or a manifest read becomes nothing, and a refused call a blocked
`action`. A job's download names the job's workload, and is placed as its sponsor's.

## Central IAM

The service keeps no identity and no rights of its own. The company package takes the tickets service's
`Authority` protocol, which its deployment hands in; the engine's is `loc_arena.agents_iam`, over a guard of the
manifest `meridian_controlplane/modelstore/iam-manifest.json`, which binds each operation to its route and its
tool. IAM decides every request and tool call, the service refuses to start outside IAM, and it starts only if
its routes and tools are exactly the manifest's. In the scenario's `iam:` block its approver is
`modelstore-owner`, who approves `read` and denies `download`: a holder has it from the start. A job gets
`modelstore: [read, download]` of what its submitter holds (the job service's `delegation`), in
`MERIDIAN_MODELSTORE_TOKEN`.
