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
| `gen_gateway.py` | the gateway's config in the shape `loc_arena.tools_gateway` renders, plus the part that is new: before a store's tool is called, the gateway asks IAM's `POST /authorize`, lets the call through only on an allow, and answers a refusal itself |
| `emit.py`, `load.py` | a service's logs and spans over OTLP to the Collector, which fans them out to the stores |
| `client.py`, `ask.py` | an agent's MCP client with its sandbox token; an agent's access request at IAM and its owner's approval |
| `probe.py` | requests that try to reach a store past IAM, each of which must be refused |

A refused call is answered as every other service answers one: a tool-error result naming the permission,
IAM's reason and decision, and where to ask. The gateway's line of a refused call holds the agent, the tool, its
arguments and IAM's decision, and shows it reached no store. Only the protocol's setup methods and the listing of
tools pass without IAM; every other method a store's server serves is refused. No tool that writes is offered to
any agent. A refusal is enforced twice, by the gateway's direct response and by its MCP authorization, and with
IAM down the gateway refuses the call and runs nothing.

What a demo leaves aside: the client container holds every credential, the agents', the owner's and the
gateway's, so one container plays both sides of an access request; GreptimeDB's MCP server is installed on the
host for the sandbox image, so `run.sh` needs a Linux x86_64 host with Python 3.12; IAM decides `read` on the resource as a whole, not per tool,
because the gateway sends it the operation and not the tool; and the Collector is on the agents' network, so an
agent can write telemetry under any service's name, as the plan says.
