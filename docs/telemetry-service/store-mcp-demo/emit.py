"""A service's telemetry over OTLP/HTTP (JSON) to the company's Collector: three log records and one trace."""
import json, time, urllib.request, secrets
now = time.time_ns(); trace = secrets.token_hex(16); span = secrets.token_hex(8); root = secrets.token_hex(8)
resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "tickets"}},
                           {"key": "service.version", "value": {"stringValue": "deploy-1"}}]}
def post(path, body):
    request = urllib.request.Request(f"http://collector:4318{path}", json.dumps(body).encode(), {"content-type": "application/json"})
    print(path, urllib.request.urlopen(request).status)
records = [("INFO", 9, "comment added to CONTROLPLANE-19"),
           ("ERROR", 17, "could not queue the notification of serving-agent about CONTROLPLANE-20: PermissionError: [Errno 13] Permission denied: 'spool'"),
           ("WARNING", 13, "slow store call: 412 ms")]
post("/v1/logs", {"resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": [
    {"timeUnixNano": str(now + i), "severityText": s, "severityNumber": n, "body": {"stringValue": b},
     "traceId": trace, "spanId": span} for i, (s, n, b) in enumerate(records)]}]}]})
spans = [{"traceId": trace, "spanId": root, "name": "tools/call comment_on_ticket", "kind": 2,
          "startTimeUnixNano": str(now - 450_000_000), "endTimeUnixNano": str(now), "status": {"code": 2, "message": "could not add the comment"}},
         {"traceId": trace, "spanId": span, "parentSpanId": root, "name": "POST /mcp", "kind": 2,
          "startTimeUnixNano": str(now - 420_000_000), "endTimeUnixNano": str(now), "status": {"code": 2}}]
post("/v1/traces", {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": spans}]}]})
print("trace", trace)
