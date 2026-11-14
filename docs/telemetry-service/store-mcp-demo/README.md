# Reading telemetry through the store's own MCP server

A demonstration for the observability plan, not part of any run. It shows, in real containers, that agents can
read a telemetry store through that store's own MCP server behind the tools gateway, with no reader service of
ours, and that central IAM still decides who may read.

```sh
make images          # the engine and sandbox images the demo borrows
docs/telemetry-service/store-mcp-demo/run.sh
```

| File | What it is |
|---|---|
| `compose.yaml` | central IAM, VictoriaLogs, VictoriaTraces and GreptimeDB with their MCP servers, the company's Collector, the tools gateway (agentgateway, the pinned release) and a client; the stores sit on a network no agent joins |
| `iam_demo.py` | the real `meridian_iam` service with one resource, `telemetry`, whose tools all need `read`; one agent holds it, one does not |
| `gen_gateway.py` | the gateway's config in the shape `loc_arena.tools_gateway` renders, plus the part that is new: before a store's tool is called, the gateway asks IAM's `POST /authorize` and lets the call through only on an allow |
| `emit.py`, `load.py` | a service's logs and spans over OTLP to the Collector, which fans them out to the stores |
| `client.py`, `ask.py` | an agent's MCP client with its sandbox token; an agent's access request at IAM and its owner's approval |

What it does not show: a refused call reaches the agent as an HTTP 403 with no word on how to ask for access,
and the gateway's line of a refused call names the agent and IAM's decision but not the tool.
