#!/bin/sh
# The whole demo, from this directory, after `make images`: prints what an agent sees and what is recorded.
set -eu
cd "$(dirname "$0")"
compose="docker compose"
[ "${KEEP:-}" ] || trap '$compose down -v >/dev/null 2>&1' EXIT
rm -rf shared && mkdir -p shared gmcp && chmod a+rwx shared  # each run's own tokens; IAM writes them as root
export WRITER_PASSWORD="$(openssl rand -hex 16)" READER_PASSWORD="$(openssl rand -hex 16)"
export WRITER_BASIC="$(printf 'collector:%s' "$WRITER_PASSWORD" | base64 | tr -d '\n')"
printf 'collector:wo=%s\nreader:ro=%s\n' "$WRITER_PASSWORD" "$READER_PASSWORD" > shared/greptime.users
chmod a+r shared/greptime.users
[ -d gmcp/greptimedb_mcp_server ] || uv pip install -q --python 3.12 --target gmcp greptimedb-mcp-server==0.6.1
agent() { $compose exec -T client python /demo/client.py "$@" 2>&1 | grep -v "Session termination" || true; }
$compose up -d iam victorialogs victoriatraces greptime collector client mcp-victorialogs mcp-victoriatraces mcp-greptime
waited=0
until [ -s shared/tokens.json ]; do
  [ "$waited" -lt 60 ] || { echo "IAM wrote no tokens in 60 s"; $compose logs iam | tail -5; exit 1; }
  sleep 1; waited=$((waited + 1))
done
uv run --project ../../.. python gen_gateway.py
$compose up -d agentgateway
sleep 5
$compose exec -T client python /demo/emit.py
sleep 4
echo "== controlplane-agent (holds read on telemetry) lists the tools: no tool that writes is offered"
agent controlplane-agent list query execute_sql
echo "== LogsQL through VictoriaLogs' own MCP server"
agent controlplane-agent call query '{"query":"service.name:tickets severity_text:ERROR | fields _time, _msg, trace_id, service.version","start":"2026-01-01T00:00:00Z"}'
echo "== SQL through GreptimeDB's own MCP server: the log record joined to its span"
agent controlplane-agent call execute_sql '{"query":"SELECT l.severity_text, l.body, t.span_name, t.duration_nano/1000000 AS ms FROM opentelemetry_logs l JOIN opentelemetry_traces t ON l.trace_id = t.trace_id AND l.span_id = t.span_id WHERE l.severity_number >= 17"}'
echo "== writes are refused: a tool that writes by the gateway, SQL by the MCP server, then by the read-only user"
agent controlplane-agent call delete_pipeline '{"name":"greptime_trace_v1","version":"1"}'
agent controlplane-agent call create_dashboard '{"name":"x","definition":"{}"}'
agent controlplane-agent call execute_sql '{"query":"DROP TABLE opentelemetry_logs"}'
agent controlplane-agent call execute_sql '{"query":"ADMIN flush_table(\"opentelemetry_logs\")"}'
echo "== datapipe-agent (holds nothing) is refused, asks IAM, is approved by the owner, and reads"
agent datapipe-agent call query '{"query":"severity_text:ERROR | fields _msg","start":"2026-01-01T00:00:00Z"}'
$compose exec -T client python /demo/ask.py
agent datapipe-agent call query '{"query":"severity_text:ERROR | fields _msg","start":"2026-01-01T00:00:00Z"}'
echo "== the gateway's line of the refused call (it reached no target) and of the answered one"
$compose logs agentgateway --no-log-prefix | grep -F '"iam_allow":false' | tail -1 | cut -c1-700
$compose logs agentgateway --no-log-prefix | grep -F '"iam_allow":true' | grep -F datapipe-agent | tail -1 | cut -c1-700
grep '"authorize"' shared/iam.audit.jsonl | tail -2
echo "== with IAM stopped, a granted agent's call is refused and nothing runs"
$compose stop iam >/dev/null 2>&1
agent controlplane-agent call query '{"query":"*","start":"2026-01-01T00:00:00Z","limit":1}'
$compose logs agentgateway --no-log-prefix | grep -F 'external authorization failed' | tail -1 | cut -c1-400
echo "== memory"
docker stats --no-stream --format '{{.Name}}\t{{.MemUsage}}' | grep obsdemo | sort
