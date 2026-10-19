"""A stand-in OpenAI-compatible upstream for the spike: replies with what it received.

Each reply's content is a JSON echo of the request's path, its Authorization header and its body, so a test
can see what agentgateway forwarded (the header as its SHA-256, so no log ever holds the key). A request whose model ends in ``-stream-slow`` streams one chunk a second
for 30 seconds (for hang-up tests); ``"big": n`` in the body makes the reply ``n`` bytes long.
"""

import hashlib
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            self._reply(200, json.dumps({"data": [{"id": "mock/model"}]}).encode())
        else:
            self._reply(404, b'{"error":"no such path"}')

    def do_POST(self) -> None:
        raw = self.rfile.read(int(self.headers.get("content-length", 0)))
        request = json.loads(raw or b"{}")
        echo = {
            "path": self.path,
            "authorization_sha256": hashlib.sha256(self.headers.get("authorization", "").encode()).hexdigest(),
            "x_headers": {k: v for k, v in self.headers.items() if k.lower().startswith("x-")},
            "body": request,
        }
        content = json.dumps(echo)
        if big := request.get("big"):
            content = content + "x" * (int(big) - len(content))
        model = request.get("model", "")
        if not request.get("stream"):
            reply = {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }
            self._reply(200, json.dumps(reply).encode())
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        pieces = [content[i : i + 40] for i in range(0, len(content), 40)]
        if model.endswith("-stream-slow"):
            pieces = [f"tick {i} " for i in range(30)]
        for piece in pieces:
            chunk = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                     "choices": [{"index": 0, "delta": {"content": piece}}]}
            self._chunk(f"data: {json.dumps(chunk)}\n\n".encode())
            if model.endswith("-stream-slow"):
                time.sleep(1)
        final = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
        self._chunk(f"data: {json.dumps(final)}\n\n".encode())
        self._chunk(b"data: [DONE]\n\n")
        self._chunk(b"")

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
