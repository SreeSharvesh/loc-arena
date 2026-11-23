"""20,000 log records and 4,000 spans over OTLP to the Collector, in batches of 1,000 and 200."""
import json, time, urllib.request, secrets
resource = {"attributes": [{"key": "service.name", "value": {"stringValue": "notes"}}]}
def post(path, body):
    urllib.request.urlopen(urllib.request.Request(f"http://collector:4318{path}", json.dumps(body).encode(), {"content-type": "application/json"})).read()
for batch in range(20):
    now = time.time_ns()
    post("/v1/logs", {"resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": [
        {"timeUnixNano": str(now + i), "severityText": "INFO", "severityNumber": 9, "traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8),
         "body": {"stringValue": f"request {batch}-{i} handled in {i % 400} ms for agent-{i % 7} " + "x" * 200}} for i in range(1000)]}]}]})
    post("/v1/traces", {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": [
        {"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "name": f"GET /notes/{i}", "kind": 2,
         "startTimeUnixNano": str(now - 5_000_000), "endTimeUnixNano": str(now)} for i in range(200)]}]}]})
print("sent")
