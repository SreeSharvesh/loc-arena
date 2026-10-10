"""The team's notes board: a dummy live service that proves agents reach a service on agent-net.

It accepts the credentials compose mounts into it, every ``credential_<name>`` file in the secrets directory,
and nothing else. With none mounted it refuses to start unless told to run open, so a missing mount fails
closed. Standard library only: the image holds this one file.
"""

import argparse
import hmac
import json
import re
import sys
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

CREDENTIAL_PREFIX = "credential_"  # compose mounts the credential `<name>` as this prefix plus the name
BEARER = "Bearer "
HEALTH_PATH = "/health"  # the one path open to every caller, for the healthcheck
NOTES_PATH = "/notes"
KEY = re.compile(r"[A-Za-z0-9._-]{1,64}")
NO_CREDENTIAL = "none"


@dataclass(frozen=True)
class Settings:
    """Where the service listens, the credentials it accepts by name, and how much it holds."""

    port: int
    credentials: dict[str, bytes]
    max_notes: int
    max_note_bytes: int


class NotesServer(ThreadingHTTPServer):
    """The notes, held in memory for the life of the episode."""

    def __init__(self, settings: Settings) -> None:
        """Listen on every address of the container, on ``settings.port``."""
        super().__init__(("0.0.0.0", settings.port), NotesHandler)  # noqa: S104 - agent-net is its only network
        self.settings = settings
        self.notes: dict[str, str] = {}
        self.lock = threading.Lock()


class NotesHandler(BaseHTTPRequestHandler):
    """``GET /notes`` lists keys; ``GET``, ``PUT`` and ``DELETE /notes/<key>`` act on one note."""

    def do_GET(self) -> None:
        """Answer the healthcheck, list the keys, or read one note."""
        if self.path == HEALTH_PATH:
            self._send(HTTPStatus.OK, b"ok")
            return
        server, caller, key = self._admit()
        if caller is None:
            return
        with server.lock:
            text = server.notes.get(key) if key else json.dumps(sorted(server.notes))
        if text is None:
            self._send(HTTPStatus.NOT_FOUND)
            return
        self._send(HTTPStatus.OK, text.encode())

    def do_PUT(self) -> None:
        """Write one note, within the size and count bounds."""
        server, caller, key = self._admit()
        if caller is None:
            return
        if not key:
            self._send(HTTPStatus.METHOD_NOT_ALLOWED)
            return
        declared = self.headers.get("Content-Length", "")
        if not declared.isdigit():
            self._send(HTTPStatus.LENGTH_REQUIRED)
            return
        if int(declared) > server.settings.max_note_bytes:
            self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        text = self.rfile.read(int(declared)).decode(errors="replace")
        with server.lock:
            created = key not in server.notes
            if created and len(server.notes) >= server.settings.max_notes:
                status = HTTPStatus.INSUFFICIENT_STORAGE
            else:
                server.notes[key] = text
                status = HTTPStatus.CREATED if created else HTTPStatus.OK
        self._send(status)

    def do_DELETE(self) -> None:
        """Remove one note."""
        server, caller, key = self._admit()
        if caller is None:
            return
        with server.lock:
            removed = server.notes.pop(key, None) is not None
        self._send(HTTPStatus.NO_CONTENT if removed else HTTPStatus.NOT_FOUND)

    def _admit(self) -> tuple[NotesServer, str | None, str]:
        """The server, the caller's credential name (``None`` once refused), and the note key."""
        server = cast("NotesServer", self.server)
        caller = _credential_name(self.headers.get("Authorization", ""), server.settings.credentials)
        if caller is None:
            self._send(HTTPStatus.UNAUTHORIZED)
            return server, None, ""
        if self.path == NOTES_PATH:
            return server, caller, ""
        key = self.path.removeprefix(f"{NOTES_PATH}/")
        if key == self.path or not KEY.fullmatch(key):
            self._send(HTTPStatus.NOT_FOUND)
            return server, None, ""
        return server, caller, key

    def _send(self, status: HTTPStatus, body: bytes = b"") -> None:
        """Send ``status`` and ``body``; ``log_request`` logs it."""
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """One line per request on stdout: method, path, status, and the credential's name."""
        caller = _credential_name(
            self.headers.get("Authorization", ""),
            cast("NotesServer", self.server).settings.credentials,
        )
        print(self.command, self.path, code, caller or NO_CREDENTIAL, flush=True)


def _credential_name(authorization: str, credentials: dict[str, bytes]) -> str | None:
    """The name of the accepted credential ``authorization`` carries; any caller when none is accepted."""
    if not credentials:
        return NO_CREDENTIAL
    if not authorization.startswith(BEARER):
        return None
    sent = authorization.removeprefix(BEARER).encode()
    return next((name for name, value in credentials.items() if hmac.compare_digest(sent, value)), None)


def read_credentials(secrets_directory: Path) -> dict[str, bytes]:
    """Every credential mounted in ``secrets_directory``, by name."""
    return {
        path.name.removeprefix(CREDENTIAL_PREFIX): path.read_bytes().strip()
        for path in sorted(secrets_directory.glob(f"{CREDENTIAL_PREFIX}*"))
    }


def parse_settings(arguments: list[str]) -> Settings:
    """The settings from the command line; a service with no credential mounted must be told ``--open``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--secrets-dir", type=Path, default=Path("/run/secrets"))
    parser.add_argument(
        "--open",
        action="store_true",
        help="Serve every caller when no credential is mounted.",
    )
    parser.add_argument("--max-notes", type=int, default=100)
    parser.add_argument("--max-note-bytes", type=int, default=65_536)
    parsed = parser.parse_args(arguments)
    credentials = read_credentials(parsed.secrets_dir) if parsed.secrets_dir.is_dir() else {}
    if not credentials and not parsed.open:
        parser.error(
            f"no {CREDENTIAL_PREFIX}* file in {parsed.secrets_dir}; pass --open to serve every caller",
        )
    return Settings(parsed.port, credentials, parsed.max_notes, parsed.max_note_bytes)


if __name__ == "__main__":
    NotesServer(parse_settings(sys.argv[1:])).serve_forever()
