"""The notes service: who it lets in, what it keeps, and that it fails closed with no credential mounted."""

import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from http import HTTPStatus
from pathlib import Path

import pytest
from notes_service import (  # ty: ignore[unresolved-import] - found through pytest's pythonpath, not ty's
    NotesServer,
    Settings,
    parse_settings,
    read_credentials,
)

TOKEN = "the-notes-credential"
EPHEMERAL_PORT = 0
MAX_NOTES = 2
MAX_NOTE_BYTES = 16
REQUEST_TIMEOUT_SECONDS = 0.2
CLIENT_TIMEOUT_SECONDS = 5.0


def _serve(credentials: dict[str, bytes]) -> Iterator[str]:
    server = NotesServer(
        Settings(EPHEMERAL_PORT, credentials, MAX_NOTES, MAX_NOTE_BYTES, REQUEST_TIMEOUT_SECONDS),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def guarded() -> Iterator[str]:
    """A notes service accepting the credential ``notes``."""
    yield from _serve({"notes": TOKEN.encode()})


@pytest.fixture
def open_service() -> Iterator[str]:
    """A notes service run ``--open``."""
    yield from _serve({})


def _call(
    url: str,
    method: str = "GET",
    *,
    token: str | None = TOKEN,
    body: bytes | None = None,
) -> HTTPStatus:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, body, headers, method=method)) as reply:
            return HTTPStatus(reply.status)
    except urllib.error.HTTPError as error:
        return HTTPStatus(error.code)


def _read(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(request) as reply:
        return reply.read()


def test_the_healthcheck_needs_no_credential(guarded: str) -> None:
    status = _call(f"{guarded}/health", token=None)

    assert status == HTTPStatus.OK


def test_a_caller_without_a_credential_is_refused(guarded: str) -> None:
    status = _call(f"{guarded}/notes", token=None)

    assert status == HTTPStatus.UNAUTHORIZED


def test_a_caller_with_a_wrong_credential_is_refused(guarded: str) -> None:
    status = _call(f"{guarded}/notes", token="a-guess")

    assert status == HTTPStatus.UNAUTHORIZED


def test_a_caller_with_the_accepted_credential_is_served(guarded: str) -> None:
    status = _call(f"{guarded}/notes")

    assert status == HTTPStatus.OK


def test_an_open_service_serves_a_caller_without_a_credential(open_service: str) -> None:
    status = _call(f"{open_service}/notes", token=None)

    assert status == HTTPStatus.OK


def test_a_note_written_reads_back(guarded: str) -> None:
    _call(f"{guarded}/notes/plan", "PUT", body=b"batch by length")

    text = _read(f"{guarded}/notes/plan")

    assert text == b"batch by length"


def test_the_list_holds_the_keys_written(guarded: str) -> None:
    _call(f"{guarded}/notes/b", "PUT", body=b"2")
    _call(f"{guarded}/notes/a", "PUT", body=b"1")

    keys = _read(f"{guarded}/notes")

    assert keys == b'["a", "b"]'


def test_a_note_over_the_size_bound_is_refused(guarded: str) -> None:
    status = _call(f"{guarded}/notes/big", "PUT", body=b"x" * (MAX_NOTE_BYTES + 1))

    assert status == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_a_note_past_the_count_bound_is_refused(guarded: str) -> None:
    for key in range(MAX_NOTES):
        _call(f"{guarded}/notes/n{key}", "PUT", body=b"x")

    status = _call(f"{guarded}/notes/one-more", "PUT", body=b"x")

    assert status == HTTPStatus.INSUFFICIENT_STORAGE


def test_a_deleted_note_is_gone(guarded: str) -> None:
    _call(f"{guarded}/notes/plan", "PUT", body=b"x")
    _call(f"{guarded}/notes/plan", "DELETE")

    status = _call(f"{guarded}/notes/plan")

    assert status == HTTPStatus.NOT_FOUND


def test_a_key_outside_the_allowed_characters_is_not_a_note(guarded: str) -> None:
    status = _call(f"{guarded}/notes/a%20b", "PUT", body=b"x")

    assert status == HTTPStatus.NOT_FOUND


def test_the_service_accepts_each_credential_mounted_by_its_name(tmp_path: Path) -> None:
    (tmp_path / "credential_notes").write_text(f"{TOKEN}\n")
    (tmp_path / "sandbox_token").write_text("not a credential")

    credentials = read_credentials(tmp_path)

    assert credentials == {"notes": TOKEN.encode()}


def test_the_service_will_not_start_open_unless_told(tmp_path: Path) -> None:
    arguments = ["--secrets-dir", str(tmp_path)]

    with pytest.raises(SystemExit):
        parse_settings(arguments)


def test_a_length_that_is_not_ascii_digits_is_refused(guarded: str) -> None:
    request = urllib.request.Request(
        f"{guarded}/notes/plan",
        b"x",
        {"Authorization": f"Bearer {TOKEN}", "Content-Length": "\u00b2"},
        method="PUT",
    )

    with pytest.raises(urllib.error.HTTPError) as refusal:
        urllib.request.urlopen(request)

    assert refusal.value.code == HTTPStatus.LENGTH_REQUIRED


def test_a_caller_that_stalls_mid_body_loses_its_connection(guarded: str) -> None:
    host, port = guarded.removeprefix("http://").split(":")
    headers = f"PUT /notes/plan HTTP/1.0\r\nAuthorization: Bearer {TOKEN}\r\nContent-Length: 16\r\n\r\n"
    with socket.create_connection((host, int(port)), timeout=CLIENT_TIMEOUT_SECONDS) as stalled:
        stalled.sendall(f"{headers}abc".encode())

        reply = stalled.recv(1)

    assert reply == b""
