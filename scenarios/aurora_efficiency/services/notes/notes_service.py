"""In-memory notes HTTP service behind bearer-token credential authentication."""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import re
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

DEFAULT_PORT: int = 8000
DEFAULT_SECRETS_DIR: Path = Path("/run/secrets")
DEFAULT_MAX_NOTES: int = 100
DEFAULT_MAX_NOTE_BYTES: int = 65536
BEARER_PREFIX: str = "Bearer "
CREDENTIAL_PREFIX: str = "credential_"
HEALTH_PATH: str = "/health"
NOTES_PATH: str = "/notes"
NOTES_PREFIX: str = "/notes/"
NONE_CREDENTIAL: str = "none"
TEXT_CONTENT_TYPE: str = "text/plain; charset=utf-8"
JSON_CONTENT_TYPE: str = "application/json"
KEY_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

logger: logging.Logger = logging.getLogger("notes_service")


@dataclass(frozen=True)
class NotesServiceSettings:
    """Configuration settings for the notes HTTP service."""

    port: int = DEFAULT_PORT
    secrets_dir: Path = DEFAULT_SECRETS_DIR
    accepted_credentials: tuple[str, ...] = ()
    max_notes: int = DEFAULT_MAX_NOTES
    max_note_bytes: int = DEFAULT_MAX_NOTE_BYTES

    def __post_init__(self) -> None:
        """Normalize collection and path field types."""
        if not isinstance(self.accepted_credentials, tuple):
            object.__setattr__(self, "accepted_credentials", tuple(self.accepted_credentials))
        if not isinstance(self.secrets_dir, Path):
            object.__setattr__(self, "secrets_dir", Path(self.secrets_dir))


class NotesServer(ThreadingHTTPServer):
    """Threading HTTP server holding in-memory notes and auth state."""

    def __init__(
        self,
        server_address: tuple[str, int],
        request_handler_class: type[BaseHTTPRequestHandler],
        settings: NotesServiceSettings,
        credentials: dict[str, bytes],
    ) -> None:
        """Initialize the notes server with settings and loaded credentials."""
        super().__init__(server_address, request_handler_class)
        self.settings: NotesServiceSettings = settings
        self.credentials: dict[str, bytes] = credentials
        self.notes: dict[str, str] = {}
        self.notes_lock: threading.Lock = threading.Lock()


def validate_content_length(raw_length: str | None, max_bytes: int) -> tuple[HTTPStatus | None, int]:
    """Validate Content-Length header against missing, negative or excessive size."""
    if raw_length is None:
        return HTTPStatus.LENGTH_REQUIRED, 0
    try:
        length = int(raw_length)
        if length < 0:
            return HTTPStatus.BAD_REQUEST, 0
    except ValueError:
        return HTTPStatus.BAD_REQUEST, 0
    if length > max_bytes:
        return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 0
    return None, length


def try_persist_note(server: NotesServer, key: str, text: str) -> HTTPStatus:
    """Persist note in server memory dictionary obeying maximum capacity."""
    with server.notes_lock:
        if key not in server.notes and len(server.notes) >= server.settings.max_notes:
            return HTTPStatus.INSUFFICIENT_STORAGE
        is_created = key not in server.notes
        server.notes[key] = text
    return HTTPStatus.CREATED if is_created else HTTPStatus.OK


class NotesRequestHandler(BaseHTTPRequestHandler):
    """Request handler implementing notes API with token authentication."""

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default BaseHTTPRequestHandler stderr logging."""

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        """Send plain error response and log to stdout."""
        status = HTTPStatus(code)
        body = f"{status.value} {status.phrase}\n".encode()
        self.send_response_bytes(status, body, NONE_CREDENTIAL)

    def send_response_bytes(
        self,
        status: HTTPStatus,
        body: bytes,
        credential_name: str,
        content_type: str = TEXT_CONTENT_TYPE,
    ) -> None:
        """Send an HTTP response with headers and log one line to stdout."""
        self.send_response(status.value)
        if status != HTTPStatus.NO_CONTENT:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        logger.info("%s %s %d %s", self.command, self.path, status.value, credential_name)

    def authenticate_request(self) -> tuple[bool, str]:
        """Authenticate bearer token against accepted credentials."""
        server = cast(NotesServer, self.server)
        if not server.credentials:
            return True, NONE_CREDENTIAL

        auth_header = self.headers.get("Authorization")
        if not auth_header or not auth_header.startswith(BEARER_PREFIX):
            return False, NONE_CREDENTIAL

        token_bytes = auth_header[len(BEARER_PREFIX) :].strip().encode()
        for cred_name, expected_token_bytes in server.credentials.items():
            if hmac.compare_digest(token_bytes, expected_token_bytes):
                return True, cred_name

        return False, NONE_CREDENTIAL

    def route_health(self) -> None:
        """Serve health check probe."""
        if self.command != "GET":
            self.send_response_bytes(HTTPStatus.METHOD_NOT_ALLOWED, b"Method Not Allowed\n", NONE_CREDENTIAL)
            return
        self.send_response_bytes(HTTPStatus.OK, b"ok", NONE_CREDENTIAL)

    def route_notes_collection(self, credential_name: str) -> None:
        """Serve listing of all note keys."""
        if self.command != "GET":
            self.send_response_bytes(HTTPStatus.METHOD_NOT_ALLOWED, b"Method Not Allowed\n", credential_name)
            return

        server = cast(NotesServer, self.server)
        with server.notes_lock:
            keys = sorted(server.notes.keys())
        body = json.dumps(keys).encode()
        self.send_response_bytes(HTTPStatus.OK, body, credential_name, JSON_CONTENT_TYPE)

    def store_note_content(self, key: str, credential_name: str) -> None:
        """Read and persist note payload obeying byte and capacity bounds."""
        server = cast(NotesServer, self.server)
        error_status, content_length = validate_content_length(
            self.headers.get("Content-Length"),
            server.settings.max_note_bytes,
        )
        if error_status is not None:
            self.send_response_bytes(error_status, b"Invalid length\n", credential_name)
            return

        with server.notes_lock:
            if key not in server.notes and len(server.notes) >= server.settings.max_notes:
                self.send_response_bytes(
                    HTTPStatus.INSUFFICIENT_STORAGE,
                    b"Max notes reached\n",
                    credential_name,
                )
                return

        body_bytes = self.rfile.read(content_length)
        try:
            note_text = body_bytes.decode()
        except UnicodeDecodeError:
            self.send_response_bytes(HTTPStatus.BAD_REQUEST, b"Invalid UTF-8 payload\n", credential_name)
            return

        status = try_persist_note(server, key, note_text)
        self.send_response_bytes(status, b"", credential_name)

    def route_note_item(self, key: str, credential_name: str) -> None:
        """Dispatch requests targeted at a specific note key."""
        if not KEY_PATTERN.fullmatch(key):
            self.send_response_bytes(HTTPStatus.BAD_REQUEST, b"Invalid note key\n", credential_name)
            return

        server = cast(NotesServer, self.server)
        if self.command == "GET":
            with server.notes_lock:
                note_content = server.notes.get(key)
            if note_content is None:
                self.send_response_bytes(HTTPStatus.NOT_FOUND, b"Note not found\n", credential_name)
                return
            self.send_response_bytes(HTTPStatus.OK, note_content.encode(), credential_name)
        elif self.command == "PUT":
            self.store_note_content(key, credential_name)
        elif self.command == "DELETE":
            with server.notes_lock:
                existed = server.notes.pop(key, None) is not None
            if existed:
                self.send_response_bytes(HTTPStatus.NO_CONTENT, b"", credential_name)
            else:
                self.send_response_bytes(HTTPStatus.NOT_FOUND, b"Note not found\n", credential_name)
        else:
            self.send_response_bytes(HTTPStatus.METHOD_NOT_ALLOWED, b"Method Not Allowed\n", credential_name)

    def dispatch_request(self) -> None:
        """Route parsed request to corresponding handler endpoint."""
        clean_path = urllib.parse.urlparse(self.path).path
        if clean_path == HEALTH_PATH:
            self.route_health()
            return

        is_authenticated, credential_name = self.authenticate_request()
        if not is_authenticated:
            self.send_response_bytes(HTTPStatus.UNAUTHORIZED, b"Unauthorized\n", NONE_CREDENTIAL)
            return

        if clean_path == NOTES_PATH:
            self.route_notes_collection(credential_name)
        elif clean_path.startswith(NOTES_PREFIX):
            key = clean_path[len(NOTES_PREFIX) :]
            self.route_note_item(key, credential_name)
        else:
            self.send_response_bytes(HTTPStatus.NOT_FOUND, b"Not Found\n", credential_name)

    def handle_one_request(self) -> None:
        """Parse one HTTP request and dispatch to routes."""
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = ""
            self.request_version = ""
            self.command = ""
            self.send_response_bytes(HTTPStatus.REQUEST_URI_TOO_LONG, b"URI Too Long\n", NONE_CREDENTIAL)
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        if not self.parse_request():
            return
        self.dispatch_request()
        self.wfile.flush()


def configure_logging() -> None:
    """Configure stdout logging for the notes service."""
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)


def read_credentials(secrets_dir: Path, accepted_credentials: tuple[str, ...]) -> dict[str, bytes]:
    """Read and strip accepted credential files from secrets directory."""
    credentials: dict[str, bytes] = {}
    for credential_name in accepted_credentials:
        credential_file = secrets_dir / f"{CREDENTIAL_PREFIX}{credential_name}"
        if not credential_file.is_file():
            raise FileNotFoundError(
                f"Missing credential file for accepted credential {credential_name!r}: {credential_file}",
            )
        token = credential_file.read_text(encoding="utf-8").strip()
        credentials[credential_name] = token.encode()
    return credentials


def create_server(settings: NotesServiceSettings) -> NotesServer:
    """Build a configured NotesServer instance from settings."""
    configure_logging()
    credentials = read_credentials(settings.secrets_dir, settings.accepted_credentials)
    server_address = ("0.0.0.0", settings.port)
    return NotesServer(server_address, NotesRequestHandler, settings, credentials)


build_server = create_server


def parse_arguments(arguments: list[str] | None = None) -> NotesServiceSettings:
    """Parse command-line arguments into NotesServiceSettings."""
    parser = argparse.ArgumentParser(description="In-memory notes HTTP service.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--secrets-dir", type=Path, default=DEFAULT_SECRETS_DIR)
    parser.add_argument("--accept", action="append", default=[], dest="accepted_credentials")
    parser.add_argument("--max-notes", type=int, default=DEFAULT_MAX_NOTES)
    parser.add_argument("--max-note-bytes", type=int, default=DEFAULT_MAX_NOTE_BYTES)
    parsed = parser.parse_args(arguments)
    return NotesServiceSettings(
        port=parsed.port,
        secrets_dir=parsed.secrets_dir,
        accepted_credentials=tuple(parsed.accepted_credentials),
        max_notes=parsed.max_notes,
        max_note_bytes=parsed.max_note_bytes,
    )


def main() -> None:
    """Run the notes service CLI."""
    settings = parse_arguments()
    try:
        server = create_server(settings)
    except Exception as error:
        sys.stderr.write(f"Startup error: {error}\n")
        sys.exit(1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
