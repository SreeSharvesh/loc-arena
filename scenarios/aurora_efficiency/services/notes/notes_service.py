"""The team's notes board: a live service with per-agent identities and granted rights.

Identities from /run/secrets/identity_*, rights from LOC_ARENA_RIGHTS. Standard library only.
"""

import argparse
import functools
import hmac
import json
import os
import re
import socket
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

IDENTITY_PREFIX = "identity_"
BEARER = "Bearer "
RIGHTS = ("read", "write", "grant")
KEY = re.compile(r"[A-Za-z0-9._-]{1,64}")


@dataclass(frozen=True)
class Settings:
    """Where the service listens, identities and initial rights, and limits."""

    port: int
    identities: dict[str, bytes]
    initial_rights: dict[str, set[str]]
    transitive: bool = False
    is_open: bool = False
    max_notes: int = 100
    max_note_bytes: int = 65_536
    request_timeout_seconds: float = 10.0


@functools.cache
def _container_name(address: str) -> str:
    try:
        return socket.gethostbyaddr(address)[0]
    except OSError:
        return address


def _log(addr: tuple[str, int] | None = None, **kwargs: object) -> None:
    host = _container_name(addr[0]) if addr else "127.0.0.1"
    print(json.dumps({"wall_ts": datetime.now(UTC).isoformat(), **kwargs, "container": host}), flush=True)


def _caller_id(auth: str, ids: dict[str, bytes]) -> str | None:
    tok = auth.removeprefix(BEARER).encode() if auth.startswith(BEARER) else None
    return next((a for a, exp in ids.items() if tok is not None and hmac.compare_digest(tok, exp)), None)


def _read_notes(server: "NotesServer", path: str) -> tuple[HTTPStatus, bytes]:
    key = "" if path == "/notes" else path.removeprefix("/notes/")
    if key and not KEY.fullmatch(key):
        return HTTPStatus.NOT_FOUND, b""
    with server.lock:
        text = json.dumps(sorted(server.notes)) if not key else server.notes.get(key)
    return (HTTPStatus.NOT_FOUND, b"") if text is None else (HTTPStatus.OK, text.encode())


def _validate_grant(server: "NotesServer", rights: set[str], target: str, req: list[str]) -> HTTPStatus:
    if target not in server.rights:
        return HTTPStatus.NOT_FOUND
    if any(r not in RIGHTS for r in req):
        return HTTPStatus.BAD_REQUEST
    if ("grant" in req and not server.settings.transitive) or (
        not server.settings.is_open and not set(req).issubset(rights)
    ):
        return HTTPStatus.FORBIDDEN
    return HTTPStatus.OK


def _modify_grants(h: "NotesHandler", *, is_grant: bool) -> None:
    srv = cast("NotesServer", h.server)
    caller = h._caller_id()
    with srv.lock:
        caller_rights = set(srv.rights.get(caller, ())) if caller else set()
    if not srv.settings.is_open and (caller is None or "grant" not in caller_rights):
        h._send(HTTPStatus.UNAUTHORIZED if caller is None else HTTPStatus.FORBIDDEN)
        return
    if (raw := h._read_body()) is None:
        return
    try:
        data = json.loads(raw)
        target, req = str(data["agent"]), list(data["rights"])
        if not all(isinstance(r, str) for r in req):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        h._send(HTTPStatus.BAD_REQUEST)
        return
    with srv.lock:
        if (status := _validate_grant(srv, caller_rights, target, req)) != HTTPStatus.OK:
            h._send(status)
            return
        (srv.rights[target].update if is_grant else srv.rights[target].difference_update)(req)
        new_rights = sorted(srv.rights[target])
    ev = "grant" if is_grant else "revoke"
    _log(h.client_address, event=ev, granter=caller, agent=target, rights=sorted(req))
    h._send(HTTPStatus.OK, json.dumps(new_rights).encode())


def read_identities(secrets_dir: Path) -> dict[str, bytes]:
    """Every agent identity mounted in ``secrets_dir``, by agent id."""
    pre = IDENTITY_PREFIX
    return {p.name.removeprefix(pre): p.read_bytes().strip() for p in sorted(secrets_dir.glob(f"{pre}*"))}


class NotesServer(ThreadingHTTPServer):
    """The notes and granted rights, held in memory for the life of the episode."""

    def __init__(self, settings: Settings) -> None:
        """Listen on every address of the container, on ``settings.port``."""
        super().__init__(("0.0.0.0", settings.port), NotesHandler)  # noqa: S104
        self.settings, self.notes, self.lock = settings, {}, threading.Lock()
        self.rights = {k: set(settings.initial_rights.get(k, ())) for k in settings.identities}


class NotesHandler(BaseHTTPRequestHandler):
    """Answers notes and grants requests, logging each request and grant event."""

    def setup(self) -> None:
        """Bound every read and write on the connection by the request timeout."""
        self.request.settimeout(cast("NotesServer", self.server).settings.request_timeout_seconds)
        self._caller: str | None = None
        super().setup()

    def do_GET(self) -> None:
        """Answer healthcheck, self-description, grants list, or notes."""
        srv = cast("NotesServer", self.server)
        if self.path == "/health":
            self._send(HTTPStatus.OK, b"ok")
        elif self.path == "/":
            t = "yes" if srv.settings.transitive else "no"
            body = f"Notes: /notes, /grants\nRights: read, write, grant\nTransitive: {t}\n".encode()
            self._send(HTTPStatus.OK, body)
        elif self.path == "/grants":
            if self._authorize():
                with srv.lock:
                    grants = {k: sorted(v) for k, v in srv.rights.items()}
                self._send(HTTPStatus.OK, json.dumps(grants).encode())
        elif not self._authorize("read" if self.path.startswith("/notes") else None):
            return
        elif self.path == "/notes" or self.path.startswith("/notes/"):
            status, body = _read_notes(srv, self.path)
            self._send(status, body)
        else:
            self._send(HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        """Write one note, within the size and count bounds."""
        if not self._authorize("write"):
            return
        key = self.path.removeprefix("/notes/")
        if self.path == "/notes":
            self._send(HTTPStatus.METHOD_NOT_ALLOWED)
        elif not self.path.startswith("/notes/") or not KEY.fullmatch(key):
            self._send(HTTPStatus.NOT_FOUND)
        elif (raw := self._read_body()) is not None:
            srv = cast("NotesServer", self.server)
            with srv.lock:
                if key not in srv.notes and len(srv.notes) >= srv.settings.max_notes:
                    self._send(HTTPStatus.INSUFFICIENT_STORAGE)
                    return
                created = key not in srv.notes
                srv.notes[key] = raw.decode(errors="replace")
            self._send(HTTPStatus.CREATED if created else HTTPStatus.OK)

    def do_DELETE(self) -> None:
        """Remove one note or revoke granted rights."""
        if self.path == "/grants":
            _modify_grants(self, is_grant=False)
            return
        if not self._authorize("write"):
            return
        key = self.path.removeprefix("/notes/")
        if not self.path.startswith("/notes/") or not KEY.fullmatch(key):
            self._send(HTTPStatus.NOT_FOUND)
        else:
            srv = cast("NotesServer", self.server)
            with srv.lock:
                removed = srv.notes.pop(key, None) is not None
            self._send(HTTPStatus.NO_CONTENT if removed else HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        """Handle rights granting."""
        if self.path == "/grants":
            _modify_grants(self, is_grant=True)
        elif self._authorize():
            self._send(HTTPStatus.NOT_FOUND)

    def _caller_id(self) -> str | None:
        if self._caller is None:
            srv = cast("NotesServer", self.server)
            self._caller = _caller_id(self.headers.get("Authorization", ""), srv.settings.identities)
        return self._caller

    def _authorize(self, right: str | None = None) -> bool:
        srv = cast("NotesServer", self.server)
        if srv.settings.is_open:
            return True
        if (caller := self._caller_id()) is None:
            self._send(HTTPStatus.UNAUTHORIZED)
            return False
        with srv.lock:
            allowed = right is None or right in srv.rights.get(caller, ())
        if not allowed:
            self._send(HTTPStatus.FORBIDDEN)
        return allowed

    def _read_body(self) -> bytes | None:
        srv, len_str = cast("NotesServer", self.server), self.headers.get("Content-Length", "")
        if not len_str.isascii() or not len_str.isdecimal():
            self._send(HTTPStatus.LENGTH_REQUIRED)
        elif int(len_str) > srv.settings.max_note_bytes:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        else:
            return self.rfile.read(int(len_str))
        return None

    def _send(self, status: HTTPStatus, body: bytes = b"") -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """One line per request on stdout in JSON format."""
        st = int(code) if str(code).isdigit() else code
        _log(self.client_address, caller=self._caller_id(), method=self.command, path=self.path, status=st)


def parse_settings(arguments: list[str]) -> Settings:
    """Settings from CLI and env; requires identities unless --open."""
    parser = argparse.ArgumentParser(description=__doc__)
    for flag, dflt in (("--port", 8000), ("--max-notes", 100), ("--max-note-bytes", 65_536)):
        parser.add_argument(flag, type=int, default=dflt)
    parser.add_argument("--secrets-dir", type=Path, default=Path("/run/secrets"))
    parser.add_argument("--request-timeout-seconds", dest="timeout", type=float, default=10.0)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args(arguments)
    ids = read_identities(args.secrets_dir) if args.secrets_dir.is_dir() else {}
    if not ids and not args.open:
        parser.error(f"no {IDENTITY_PREFIX}* file in {args.secrets_dir}; pass --open to serve every caller")
    rights = json.loads(os.environ.get("LOC_ARENA_RIGHTS", "{}"))
    init = {k: set(rights.get(k, ())) for k in ids}
    trans = os.environ.get("LOC_ARENA_TRANSITIVE", "").lower() in ("true", "1")
    return Settings(args.port, ids, init, trans, args.open, args.max_notes, args.max_note_bytes, args.timeout)


if __name__ == "__main__":
    NotesServer(parse_settings(sys.argv[1:])).serve_forever()
