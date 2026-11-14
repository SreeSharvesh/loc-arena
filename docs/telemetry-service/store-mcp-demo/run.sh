#!/bin/sh
# The whole demo, from this directory, after `make images`: prints what an agent sees and what is recorded.
set -eu
cd "$(dirname "$0")"
export WRITER_PASSWORD="$(openssl rand -hex 16)" READER_PASSWORD="$(openssl rand -hex 16)"
export WRITER_BASIC="$(printf 'collector:%s' "$WRITER_PASSWORD" | base64 -w0)"
mkdir -p shared gmcp
[ -d gmcp/greptimedb_mcp_server ] || uv pip install -q --python 3.12 --target gmcp greptimedb-mcp-server==0.6.1
compose="docker compose"
agent() { $compose exec -T client python /demo/client.py "$@" 2>&1 | grep -v "Session termination"; }
$compose up -d iam victorialogs victoriatraces greptime collector client mcp-victorialogs mcp-victoriatraces mcp-greptime
until [ -s shared/tokens.json ]; do sleep 1; done
uv run --project ../../.. python gen_gateway.py
$compose up -d agentgateway
sleep 5
$compose exec -T client python /demo/emit.py
sleep 4
echo "== controlplane-agent (holds read on telemetry) lists the tools"
agent controlplane-agent list query execute_sql
echo "== LogsQL through VictoriaLogs' own MCP server"
agent controlplane-agent call query '{"query":"service.name:tickets severity_text:ERROR | fields _time, _msg, trace_id, service.version","start":"2026-01-01T00:00:00Z"}'
echo "== SQL through GreptimeDB's own MCP server: the log record joined to its span"
agent controlplane-agent call execute_sql '{"query":"SELECT l.severity_text, l.body, t.span_name, t.duration_nano/1000000 AS ms FROM opentelemetry_logs l JOIN opentelemetry_traces t ON l.trace_id = t.trace_id AND l.span_id = t.span_id WHERE l.severity_number >= 17"}'
echo "== writes are refused: by the MCP server, then by the store's read-only user"
agent controlplane-agent call execute_sql '{"query":"DROP TABLE opentelemetry_logs"}'
agent controlplane-agent call execute_sql '{"query":"ADMIN flush_table(\"opentelemetry_logs\")"}'
agent controlplane-agent call delete_pipeline '{"name":"greptime_trace_v1","version":"1"}'
echo "== datapipe-agent (holds nothing) is refused, asks IAM, is approved by the owner, and reads"
agent datapipe-agent call query '{"query":"severity_text:ERROR | fields _msg","start":"2026-01-01T00:00:00Z"}'
$compose exec -T client python /demo/ask.py
agent datapipe-agent call query '{"query":"severity_text:ERROR | fields _msg","start":"2026-01-01T00:00:00Z"}'
echo "== the gateway's record of the last call, and IAM's audit of its decisions"
$compose logs agentgateway --no-log-prefix | grep '"tools/call"' | tail -1 | cut -c1-900
grep '"authorize"' shared/iam.audit.jsonl | tail -3
echo "== memory"
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' | grep obsdemo | sort
[ "${KEEP:-}" ] || $compose down -v
