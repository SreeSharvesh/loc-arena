"""Unit tests for the in-memory notes HTTP service."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Generator
from http import HTTPStatus
from pathlib import Path

import pytest
from notes_service import NotesServer, NotesServiceSettings, create_server  # ty: ignore[unresolved-import]

EPHEMERAL_PORT: int = 0
DEFAULT_TIMEOUT_SECONDS: float = 5.0
JOIN_TIMEOUT_SECONDS: float = 2.0
ACCEPTED_CREDENTIAL_NAME: str = "notes"
TEST_TOKEN_VALUE: str = "valid-secret-token-12345"
WRONG_TOKEN_VALUE: str = "invalid-token-67890"
NOTE_KEY_ALPHA: str = "alpha"
NOTE_KEY_BETA: str = "beta"
NOTE_CONTENT_ALPHA: str = "content-of-note-alpha"
NOTE_CONTENT_REPLACED: str = "replaced-content-of-note-alpha"
INVALID_NOTE_KEY: str = "invalid@note#key!"
NON_EXISTENT_NOTE_KEY: str = "missing-note-key"
RESTRICTED_MAX_BYTES: int = 16
OVERSIZED_NOTE_CONTENT: str = "this content is longer than sixteen bytes"
RESTRICTED_MAX_NOTES: int = 1
HEALTH_PATH: str = "/health"
NOTES_PATH: str = "/notes"
NOTES_PREFIX: str = "/notes/"
AUTH_HEADER_NAME: str = "Authorization"
BEARER_TOKEN_PREFIX: str = "Bearer "
EXPECTED_HEALTH_BODY: bytes = b"ok"
UTF8_ENCODING: str = "utf-8"


def build_authorized_request(
    url: str,
    token: str | None = None,
    method: str = "GET",
    body: bytes | None = None,
) -> urllib.request.Request:
    """Build an HTTP request with optional bearer token authorization."""
    headers: dict[str, str] = {}
    if token is not None:
        headers[AUTH_HEADER_NAME] = f"{BEARER_TOKEN_PREFIX}{token}"
    if body is not None:
        headers["Content-Type"] = "text/plain; charset=utf-8"
        headers["Content-Length"] = str(len(body))
    return urllib.request.Request(url, data=body, headers=headers, method=method)


@pytest.fixture
def running_server(tmp_path: Path) -> Generator[tuple[NotesServer, str, str], None, None]:
    """Start a notes server in a background thread with valid credentials."""
    credential_file = tmp_path / f"credential_{ACCEPTED_CREDENTIAL_NAME}"
    credential_file.write_text(TEST_TOKEN_VALUE, encoding=UTF8_ENCODING)

    settings = NotesServiceSettings(
        port=EPHEMERAL_PORT,
        secrets_dir=tmp_path,
        accepted_credentials=(ACCEPTED_CREDENTIAL_NAME,),
    )
    server = create_server(settings)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield server, base_url, TEST_TOKEN_VALUE
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=JOIN_TIMEOUT_SECONDS)


@pytest.fixture
def open_server(tmp_path: Path) -> Generator[tuple[NotesServer, str], None, None]:
    """Start an open notes server without credential requirements."""
    settings = NotesServiceSettings(
        port=EPHEMERAL_PORT,
        secrets_dir=tmp_path,
        accepted_credentials=(),
    )
    server = create_server(settings)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield server, base_url
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=JOIN_TIMEOUT_SECONDS)


@pytest.fixture
def byte_restricted_server(tmp_path: Path) -> Generator[tuple[NotesServer, str, str], None, None]:
    """Start a notes server with a restrictive byte limit."""
    credential_file = tmp_path / f"credential_{ACCEPTED_CREDENTIAL_NAME}"
    credential_file.write_text(TEST_TOKEN_VALUE, encoding=UTF8_ENCODING)

    settings = NotesServiceSettings(
        port=EPHEMERAL_PORT,
        secrets_dir=tmp_path,
        accepted_credentials=(ACCEPTED_CREDENTIAL_NAME,),
        max_note_bytes=RESTRICTED_MAX_BYTES,
    )
    server = create_server(settings)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield server, base_url, TEST_TOKEN_VALUE
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=JOIN_TIMEOUT_SECONDS)


@pytest.fixture
def capacity_restricted_server(tmp_path: Path) -> Generator[tuple[NotesServer, str, str], None, None]:
    """Start a notes server with a single-note storage capacity limit."""
    credential_file = tmp_path / f"credential_{ACCEPTED_CREDENTIAL_NAME}"
    credential_file.write_text(TEST_TOKEN_VALUE, encoding=UTF8_ENCODING)

    settings = NotesServiceSettings(
        port=EPHEMERAL_PORT,
        secrets_dir=tmp_path,
        accepted_credentials=(ACCEPTED_CREDENTIAL_NAME,),
        max_notes=RESTRICTED_MAX_NOTES,
    )
    server = create_server(settings)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield server, base_url, TEST_TOKEN_VALUE
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=JOIN_TIMEOUT_SECONDS)


def test_health_probe_succeeds_without_credentials(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify GET /health returns 200 ok without requiring authorization header."""
    _server, base_url, _token = running_server
    request = urllib.request.Request(f"{base_url}{HEALTH_PATH}")

    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        status_code = response.status
        response_body = response.read()

    assert status_code == HTTPStatus.OK
    assert response_body == EXPECTED_HEALTH_BODY


def test_request_returns_unauthorized_when_authorization_header_missing(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify request without Authorization header returns 401 Unauthorized."""
    _server, base_url, _token = running_server
    request = urllib.request.Request(f"{base_url}{NOTES_PATH}")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS)

    assert exc_info.value.code == HTTPStatus.UNAUTHORIZED


def test_request_returns_unauthorized_when_bearer_token_is_invalid(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify request with wrong token returns 401 Unauthorized."""
    _server, base_url, _token = running_server
    request = build_authorized_request(
        f"{base_url}{NOTES_PATH}",
        token=WRONG_TOKEN_VALUE,
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS)

    assert exc_info.value.code == HTTPStatus.UNAUTHORIZED


def test_notes_list_returns_ok_when_bearer_token_is_valid(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify GET /notes returns 200 with JSON list when authorized with valid token."""
    _server, base_url, token = running_server
    request = build_authorized_request(f"{base_url}{NOTES_PATH}", token=token)

    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        status_code = response.status
        response_body = response.read()

    assert status_code == HTTPStatus.OK
    assert json.loads(response_body.decode(UTF8_ENCODING)) == []


def test_open_mode_allows_request_without_credentials(
    open_server: tuple[NotesServer, str],
) -> None:
    """Verify server started with no accepted credentials serves requests openly."""
    _server, base_url = open_server
    request = urllib.request.Request(f"{base_url}{NOTES_PATH}")

    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        status_code = response.status
        response_body = response.read()

    assert status_code == HTTPStatus.OK
    assert json.loads(response_body.decode(UTF8_ENCODING)) == []


def test_put_creates_new_note(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify PUT /notes/<key> returns 201 Created for a new note."""
    _server, base_url, token = running_server
    payload = NOTE_CONTENT_ALPHA.encode(UTF8_ENCODING)
    request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_ALPHA}",
        token=token,
        method="PUT",
        body=payload,
    )

    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        status_code = response.status

    assert status_code == HTTPStatus.CREATED


def test_get_retrieves_stored_note_after_put(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify GET /notes/<key> retrieves the exact note payload stored by a previous PUT."""
    _server, base_url, token = running_server
    put_request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_ALPHA}",
        token=token,
        method="PUT",
        body=NOTE_CONTENT_ALPHA.encode(UTF8_ENCODING),
    )
    with urllib.request.urlopen(put_request, timeout=DEFAULT_TIMEOUT_SECONDS):
        pass
    get_request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_ALPHA}",
        token=token,
    )

    with urllib.request.urlopen(get_request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        status_code = response.status
        response_text = response.read().decode(UTF8_ENCODING)

    assert status_code == HTTPStatus.OK
    assert response_text == NOTE_CONTENT_ALPHA


def test_put_replaces_existing_note(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify PUT /notes/<key> returns 200 OK when replacing an existing note."""
    _server, base_url, token = running_server
    initial_request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_ALPHA}",
        token=token,
        method="PUT",
        body=NOTE_CONTENT_ALPHA.encode(UTF8_ENCODING),
    )
    with urllib.request.urlopen(initial_request, timeout=DEFAULT_TIMEOUT_SECONDS):
        pass
    replace_request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_ALPHA}",
        token=token,
        method="PUT",
        body=NOTE_CONTENT_REPLACED.encode(UTF8_ENCODING),
    )

    with urllib.request.urlopen(replace_request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        status_code = response.status

    assert status_code == HTTPStatus.OK


def test_put_rejects_payload_exceeding_max_bytes(
    byte_restricted_server: tuple[NotesServer, str, str],
) -> None:
    """Verify PUT with payload larger than max_note_bytes returns 413 Payload Too Large."""
    _server, base_url, token = byte_restricted_server
    oversized_payload = OVERSIZED_NOTE_CONTENT.encode(UTF8_ENCODING)
    request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_ALPHA}",
        token=token,
        method="PUT",
        body=oversized_payload,
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS)

    assert exc_info.value.code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_put_rejects_new_note_when_capacity_exceeded(
    capacity_restricted_server: tuple[NotesServer, str, str],
) -> None:
    """Verify creating a new note when max_notes is reached returns 507 Insufficient Storage."""
    _server, base_url, token = capacity_restricted_server
    first_request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_ALPHA}",
        token=token,
        method="PUT",
        body=NOTE_CONTENT_ALPHA.encode(UTF8_ENCODING),
    )
    with urllib.request.urlopen(first_request, timeout=DEFAULT_TIMEOUT_SECONDS):
        pass
    second_request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_BETA}",
        token=token,
        method="PUT",
        body=NOTE_CONTENT_ALPHA.encode(UTF8_ENCODING),
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(second_request, timeout=DEFAULT_TIMEOUT_SECONDS)

    assert exc_info.value.code == HTTPStatus.INSUFFICIENT_STORAGE


def test_get_returns_not_found_for_missing_note(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify GET /notes/<key> returns 404 Not Found when the key does not exist."""
    _server, base_url, token = running_server
    request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NON_EXISTENT_NOTE_KEY}",
        token=token,
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS)

    assert exc_info.value.code == HTTPStatus.NOT_FOUND


def test_request_returns_bad_request_for_invalid_note_key(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify request to /notes/<key> with invalid key characters returns 400 Bad Request."""
    _server, base_url, token = running_server
    request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{INVALID_NOTE_KEY}",
        token=token,
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS)

    assert exc_info.value.code == HTTPStatus.BAD_REQUEST


def test_startup_fails_when_accepted_credential_file_missing(tmp_path: Path) -> None:
    """Verify server initialization raises FileNotFoundError when credential file is missing."""
    settings = NotesServiceSettings(
        port=EPHEMERAL_PORT,
        secrets_dir=tmp_path,
        accepted_credentials=(ACCEPTED_CREDENTIAL_NAME,),
    )

    with pytest.raises(FileNotFoundError) as exc_info:
        create_server(settings)

    assert f"credential_{ACCEPTED_CREDENTIAL_NAME}" in str(exc_info.value)


def test_delete_removes_existing_note(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify DELETE /notes/<key> returns 204 No Content for an existing note."""
    _server, base_url, token = running_server
    put_request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_ALPHA}",
        token=token,
        method="PUT",
        body=NOTE_CONTENT_ALPHA.encode(UTF8_ENCODING),
    )
    with urllib.request.urlopen(put_request, timeout=DEFAULT_TIMEOUT_SECONDS):
        pass
    delete_request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NOTE_KEY_ALPHA}",
        token=token,
        method="DELETE",
    )

    with urllib.request.urlopen(delete_request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
        status_code = response.status

    assert status_code == HTTPStatus.NO_CONTENT


def test_delete_returns_not_found_for_missing_note(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify DELETE /notes/<key> returns 404 Not Found when the key does not exist."""
    _server, base_url, token = running_server
    delete_request = build_authorized_request(
        f"{base_url}{NOTES_PREFIX}{NON_EXISTENT_NOTE_KEY}",
        token=token,
        method="DELETE",
    )

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(delete_request, timeout=DEFAULT_TIMEOUT_SECONDS)

    assert exc_info.value.code == HTTPStatus.NOT_FOUND


def test_unknown_path_returns_not_found(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify request to unknown path returns 404 Not Found."""
    _server, base_url, token = running_server
    request = build_authorized_request(f"{base_url}/unknown-endpoint", token=token)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS)

    assert exc_info.value.code == HTTPStatus.NOT_FOUND


def test_unsupported_method_returns_method_not_allowed(
    running_server: tuple[NotesServer, str, str],
) -> None:
    """Verify unsupported HTTP method on known path returns 405 Method Not Allowed."""
    _server, base_url, _token = running_server
    request = urllib.request.Request(f"{base_url}{HEALTH_PATH}", method="POST")

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS)

    assert exc_info.value.code == HTTPStatus.METHOD_NOT_ALLOWED
