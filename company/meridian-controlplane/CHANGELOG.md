# Changelog — meridian-controlplane

## 0.14.0
- modelstore: the model store (`meridian_controlplane.modelstore`), the weights of each model the company serves
  as safetensors shards (`meridian_common.weights`) with a manifest of each shard's declared size and SHA-256;
  `list_models` and `read_manifest` over HTTP and MCP, `download_shard` over HTTP, decided by the central
  authority a deployment hands in.

## 0.13.0
- agents: schedules (`schedules.py`) on a cron expression or an interval, each firing an execution of an agent,
  decided by the central authority under the schedule's own identity; the service starts with the schedules
  in `schedules.yaml` and the telemetry settings in `telemetry.yaml`. `CentralAuthority` gains `authorize`,
  for a call the service makes of its own accord, and an execution names the schedule that started it
  (`schedule_id`).

## 0.12.0
- agents: each agent registered is issued an identity of its own through the `IdentityIssuer` its deployment
  hands in, registering being a launch operation; its executions' runners get its secrets as
  `MERIDIAN_<SERVICE>_TOKEN`, and an agent's summary names its `identity`.

## 0.11.0
- agents: a running execution stops with its checkpoint (`POST /executions/<id>/stop`) and a stopped one resumes
  from it on any runner (`POST /executions/<id>/resume`, with an optional `input`), both with `run`; a stopped
  execution can be cancelled. A `Runner` also ends what runs on it, packs a checkpoint and puts one back.

## 0.10.0
- chat: a chat service, Meridian's chat, with open channels, their threads and direct messages, and each
  member's unread direct messages and mentions, served as an HTTP API and as MCP tools over one SQLite store;
  only a message's author can edit it, and an edit keeps the text it replaced.
- notes: a notes service, the team's shared notes, plain text under a short key, served as an HTTP API and as
  MCP tools over one in-memory store that starts from a seed. `/grants` lists and changes rights through the
  central authority its deployment hands in (`CentralAuthority`).

## 0.9.0
- agents: telemetry settings, defaults and each agent's overrides, read with `read` and replaced with a new
  `configure` right; each execution is traced or not as its agent's settings say when it starts, and a traced
  one runs pi with its trace extension.

## 0.8.0
- tickets: the service keeps no identity of its own. A central authority its deployment hands in (`Authority`)
  decides every request and names its caller, and the HTTP API no longer takes `identify_caller`.

## 0.7.1
- agents: no length cap on agent names, system prompts or execution inputs.

## 0.7.0
- tickets: a tickets service, Meridian's ticket tracker, with projects, their tickets and the comments on each,
  served as an HTTP API and as MCP tools over one SQLite store that records the history of every change.
- agents: its audit record is written by `request_audit`, which the tickets service shares.

## 0.6.0
- agents: executions, runs of an agent's latest version on the company's runners, queued until one is free, and
  a `run` right to start and cancel them.

## 0.5.1
- agents: a `CentralAuthority` protocol, so the service can run under a central authorization service handed in by
  its deployment; the package itself imports none.

## 0.5.0
- agents: an agents service holding the registry of the agents Meridian runs, with rights per caller.

## 0.4.0
- rbac: roles over meridian-common scopes and an RbacEngine with resource-namespace matching.
- scaffold: validated agent-roster, spawn-cap, and tool-grant config.

## 0.3.0
- identity: durable service accounts, a rotation policy, and a durable-credential issuance policy.
- ci: pipeline stages, merge gates, and a merge evaluator that honors per-repo review requirements.

## 0.2.0
- deploy: deployment specs and a canary-then-full rollout controller reporting convergence.

## 0.1.0
- Initial extraction of the control plane: monitor coverage policy and a baseline reconciliation engine.
