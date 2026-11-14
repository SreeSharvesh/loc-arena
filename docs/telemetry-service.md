# The company's telemetry

What a running service of Meridian's says of itself: its traces and its logs, in standard OpenTelemetry from
the service to the store. In a stack run it is three things: the services that report, the company's own
Collector (`telemetry-collector`), and the telemetry store (GreptimeDB), the scenario's one `services:` entry
with `collects:`. The lab wires them with `loc_arena/telemetry_stack.py`.

Telemetry is for debugging. It is never evidence: nothing that measures a run reads the store or the
Collector, so no verdict, score or event depends on it ([why](#what-a-release-can-do-to-its-telemetry)).

## What reports

Each service the store's entry lists (`collects:`, in `aurora_efficiency` the seven services agents deploy)
runs under OpenTelemetry's zero-code instrumentation: `opentelemetry-instrument` in front of its command,
configured by the standard `OTEL_*` variables the stack renders from the run config's `telemetry:` settings
([docs/run-config.md](run-config.md)). No service holds telemetry code of ours. It exports:

- **Spans**: one of each HTTP request it serves, one of each tool call (`tools/call <tool>`, made by the MCP
  SDK it is built on), one of each call it makes to IAM, and one of each SQL statement.
- **Log records**: each record of Python's `logging`, with the trace and span it was written in, its source
  location, and an exception's type and stack.

Every signal's resource names the service (`service.name`, `service.namespace=meridian`) and its release:
`service.version` is `image` on the code the run started on, then `deploy-<n>`, with `deployment.id`. The
release supervisor of a deployed service sets them as it starts the service's process
([docs/deploy-service.md](deploy-service.md)).

The supervisor also exports what the service's process writes to stderr, each read as one log record with
`log.iostream=stderr` and the release in the record's attributes (`service.version`, `deployment.id`). The
traceback of a release that could not start is nowhere else in the telemetry. The supervisor runs under the
same instrumentation, so the hand-over of a release (`/release`) is a span under the service's name, with no
release on its resource.

## The tools gateway's span of every tool call

The tools gateway (agentgateway) exports a span of each request it serves, under `service.name`
`tools-gateway`, with the calling agent (`agent`), the tool (`gen_ai.tool.name`), the target service and how
the call ended, and a child span of its call to the service. It passes the span's context on to the service,
in the request's `traceparent` header and in the MCP message, and the service's spans continue it: one tool
call and the service's work on it are one trace, with no code of ours.

The gateway traces every request, one whose caller asks not to be traced included, and no release reaches
the gateway, so a release cannot drop these spans. It can forge one: anything on agent-net can post a span
under the gateway's name, told apart only by the address the Collector stamps on it. The gateway's spans hold
neither a call's arguments nor a credential. They do show every agent's calls, so an agent that reads the
telemetry sees which agent called which tool, and when.

## The path

A service, and the tools gateway, send OTLP over HTTP to the company's Collector, on agent-net. The Collector stamps each record
with the address it came from (`client.address`), which a sender cannot choose, drops the span of each
message of a request, batches, and writes the store as a user that can only write. The store is on
telemetry-net, which no sandbox, runner or node joins, and has a second user that can only read
([docs/isolation/README.md](isolation/README.md#containers)). This Collector is not the one that writes the
sealed record of the model calls, which takes nothing from it.

## Best effort

A store or a Collector that is down never fails a service or a run.

- A service exports from a background thread, from a bounded queue (`queue_size`), every
  `schedule_milliseconds`, each export within `export_seconds`. What does not fit or is not taken is dropped.
- The Collector holds `queue_size` batches for the store, retries one for `retry_seconds`, then drops it.
- With the Collector down, a service answers as before. Two things change: its SDK writes a line to its
  stderr for each export that fails, a few hundred bytes a second, and its process ends up to twice
  `export_seconds` later, as it flushes. The settings refuse an `export_seconds` that is not less than half
  of `deploy.stop_seconds`.
- The honest twin's renewal recreates the store and the Collector empty, with new passwords.

## What a release can do to its telemetry

A deployed release is agent-written code running as the service, so it writes its own signals. It can stop
exporting, export false spans and logs under its service's name, or flood. Anything on agent-net, a sandbox
included, can also post OTLP to the Collector under any service's name, the gateway's among them. The
gateway continues a `traceparent` its caller sends, so an agent can put its calls in a trace of its choosing:
that changes how a trace reads, not who made the call.

What a release cannot touch: the tools gateway's own spans, which it can neither stop nor change, the
address the Collector stamps on what it receives, the store, which only the
Collector writes, and every record that measures the run. So a signal is what a service said of itself, never
what happened.
