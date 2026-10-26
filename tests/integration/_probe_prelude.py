"""The prelude every probe of ``test_stack_reach`` runs first, in whichever container runs the probe.

Its helpers find what exists, try connections, resolve names, search environments, and list mounts and routes.
"""

CONNECT_TIMEOUT_SECONDS = 3
# Mounts the kernel provides in any container, which a probe of what is mounted leaves out.
KERNEL_MOUNT_ROOTS = ("/proc", "/dev", "/sys")
PRELUDE = f"""
import errno, glob, hashlib, importlib.util, json, os, socket, time, urllib.error, urllib.request
getaddrinfo_error_names = {{code: name for name, code in vars(socket).items() if name.startswith("EAI_")}}
def list_paths(pattern):
    return json.dumps(sorted(glob.glob(pattern)))
def attempt(host, port):
    try:
        socket.create_connection((host, port), {CONNECT_TIMEOUT_SECONDS}).close()
    except socket.gaierror as error:
        return "gaierror:" + getaddrinfo_error_names[error.args[0]]
    except TimeoutError:
        return "timeout"
    except OSError as error:
        return errno.errorcode[error.errno]
    return "connected"
def resolve(name):
    try:
        socket.gethostbyname(name)
    except socket.gaierror as error:
        return "gaierror:" + getaddrinfo_error_names[error.args[0]]
    return "resolved"
def post(url, body, token):
    headers = {{"content-type": "application/json", "idempotency-key": os.urandom(8).hex()}}  # IAM needs one
    if token is not None:
        headers["authorization"] = "Bearer " + token
    request = urllib.request.Request(url, body.encode(), headers)
    try:
        return urllib.request.urlopen(request, timeout={CONNECT_TIMEOUT_SECONDS}).status
    except urllib.error.HTTPError as error:
        return error.code
def fetch(url, token):
    headers = {{}} if token is None else {{"authorization": "Bearer " + token}}
    try:
        request = urllib.request.Request(url, headers=headers)
        return urllib.request.urlopen(request, timeout={CONNECT_TIMEOUT_SECONDS}).status
    except urllib.error.HTTPError as error:
        return error.code
def fetch_until(url, token, wanted, seconds):
    deadline = time.monotonic() + seconds
    while (status := fetch(url, token)) != wanted and time.monotonic() < deadline:
        time.sleep(0.5)
    return status
def search_environments(needle):
    environments = []
    for path in glob.glob("/proc/[0-9]*/environ"):
        try:
            environments.append(open(path, "rb").read())
        except OSError:
            pass
    if any(needle in environment for environment in environments):
        return "found"
    return "absent" if any(environments) else "unreadable"
def list_mounts():
    mounts = {{}}
    for line in open("/proc/self/mountinfo"):
        fields = line.split()
        if not any(fields[4] == root or fields[4].startswith(root + "/") for root in {KERNEL_MOUNT_ROOTS!r}):
            mounts[fields[4]] = fields[3]
    return mounts
def find_harness():
    harness = ("loc_arena", "scenarios")
    paths = []
    for directory, subdirectories, names in os.walk("/"):
        if directory == "/":
            subdirectories[:] = [name for name in subdirectories if name not in ("proc", "sys")]
        paths += [os.path.join(directory, name) for name in subdirectories + names if name in harness]
        subdirectories[:] = [name for name in subdirectories if name not in harness]
    modules = [name for name in harness if importlib.util.find_spec(name)]
    return json.dumps({{"modules": modules, "paths": sorted(paths)}})
# An MCP session at url with key, then one request: its JSON-RPC reply, or its HTTP status and error.
def call_mcp(url, key, method, params):
    session = {{}}
    def send(body):
        headers = {{"content-type": "application/json", "accept": "application/json, text/event-stream"}}
        if key is not None:
            headers["authorization"] = "Bearer " + key
        request = urllib.request.Request(url, json.dumps(body).encode(), {{**headers, **session}})
        try:
            response = urllib.request.urlopen(request, timeout={CONNECT_TIMEOUT_SECONDS})
        except urllib.error.HTTPError as error:
            text = error.read().decode()
            error_body = json.loads(text).get("error") if text[:1] == "{{" else None
            return {{"status": error.code, "error": error_body}}
        if response.headers.get("mcp-session-id"):
            session["mcp-session-id"] = response.headers["mcp-session-id"]
        text = response.read().decode()
        data = [line[len("data:"):] for line in text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1]) if data else json.loads(text) if text else {{}}
    client = {{"name": "probe", "version": "1"}}
    started = {{"protocolVersion": "2025-06-18", "capabilities": {{}}, "clientInfo": client}}
    begun = send({{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": started}})
    if "status" in begun:
        return begun
    send({{"jsonrpc": "2.0", "method": "notifications/initialized"}})
    return send({{"jsonrpc": "2.0", "id": 2, "method": method, "params": params}})
def list_routes():
    routes = []
    for line in open("/proc/net/route").read().splitlines()[1:]:
        fields = line.split()
        destination = socket.inet_ntoa(int(fields[1], 16).to_bytes(4, "little"))
        routes.append("%s/%d" % (destination, bin(int(fields[7], 16)).count("1")))
    return routes
"""
