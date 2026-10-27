"""The service tests' fixtures: an app served on a local port for one test."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator

import pytest
import uvicorn
from fastapi import FastAPI

STARTUP_SECONDS = 10.0


@pytest.fixture(scope="module")
def stopping() -> Iterator[list[threading.Thread]]:
    """The threads of the servers this module's tests are done with, each joined once the module ends.

    A uvicorn server takes up to 0.2 s to stop (it checks ``should_exit`` every 0.1 s, then gives its
    connections 0.1 s), so a test does not wait for its own: it stops while the next tests run.
    """
    threads: list[threading.Thread] = []
    yield threads
    for thread in threads:
        thread.join()


@pytest.fixture
def serve(stopping: list[threading.Thread]) -> Iterator[Callable[[FastAPI], str]]:
    """Serve an app on a local port until the test ends; each call returns the URL the app answers on."""
    servers: list[tuple[uvicorn.Server, threading.Thread]] = []

    def start(app: FastAPI) -> str:
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
        listener = socket.create_server(("127.0.0.1", 0))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + STARTUP_SECONDS
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        servers.append((server, thread))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"

    yield start
    for server, thread in servers:
        server.should_exit = True
        stopping.append(thread)
