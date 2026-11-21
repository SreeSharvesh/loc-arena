"""The insights reader's one model call, run in the reader's container: request in, the answer's JSON out.

It imports only the standard library: it runs as a file copied into the reader's home, beside its request.
Arguments: the gateway's chat URL, the file holding the reader's token, the request, the answer, and the
seconds to wait.
"""

import json
import sys
import urllib.request

url, token_file, request_file, answer_file, seconds = sys.argv[1:]
with open(token_file) as stream:
    token = stream.read().strip()
with open(request_file, "rb") as stream:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    request = urllib.request.Request(url, stream.read(), headers)
with urllib.request.urlopen(request, timeout=float(seconds)) as response:
    text = json.load(response)["choices"][0]["message"]["content"] or ""
# The answer is the reply's one JSON object, whatever words or fences the model put around it.
with open(answer_file, "w") as stream:
    stream.write(text[text.find("{") : text.rfind("}") + 1])
