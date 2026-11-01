"""The forge's repositories over HTTP: git's smart HTTP protocol, served by ``git http-backend``.

A caller clones, fetches, pulls and pushes ``/git/<repo>`` with its credential as a bearer token. The
deployment's authority decides each request before git runs: listing a repository's refs (``info_refs``),
fetching from it (``upload_pack``) and pushing to it (``receive_pack``), each named after git's own endpoint.
A push to ``main`` is refused by the repository's own hook (``meridian_forge.repositories``), and a push's
record names each ref it moved, from which commit to which. Git's server options are pinned, so a git release
that turns one on by default exposes nothing more, and each request is bounded in size and time
(``GitBounds``). The deployment records each request.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, TypedDict

import anyio
from fastapi import FastAPI, HTTPException, Request, Response

from meridian_forge.repositories import ISOLATED_GIT, REF_UPDATES_VARIABLE, REPOSITORY_SUFFIX

# Git's own configuration for the backend, over whatever a repository's says: push, no file-by-file access,
# and a fetch of what the refs reach and nothing else (no filters, no object by ID, no bundle or object info).
_SERVER_OPTIONS = {
    "http.receivepack": "true",
    "http.getanyfile": "false",
    "uploadpack.allowFilter": "false",
    "uploadpack.allowAnySHA1InWant": "false",
    "uploadpack.allowTipSHA1InWant": "false",
    "uploadpack.allowReachableSHA1InWant": "false",
    "uploadpack.allowRefInWant": "false",
    "uploadpack.advertiseBundleURIs": "false",
    "transfer.advertiseObjectInfo": "false",
}
_SERVER_CONFIG = {
    "GIT_CONFIG_COUNT": str(len(_SERVER_OPTIONS)),
    **{f"GIT_CONFIG_KEY_{index}": key for index, key in enumerate(_SERVER_OPTIONS)},
    **{f"GIT_CONFIG_VALUE_{index}": value for index, value in enumerate(_SERVER_OPTIONS.values())},
}
_HEADERS_END = re.compile(rb"\r?\n\r?\n")


class RefUpdate(TypedDict):
    """A ref a push moved: from ``old`` to ``new``, each a commit, or git's all-zero ID for none."""

    ref: str
    old: str
    new: str


@dataclass(frozen=True)
class GitBounds:
    """Where the bare repositories are, and the bounds of one request to git."""

    repositories: Path
    timeout_seconds: float
    request_limit_bytes: int


def create_git_api(
    bounds: GitBounds,
    repos: Collection[str],
    authorize: Sequence[Any],
    record: Callable[[FastAPI], None],
) -> FastAPI:
    """The HTTP API over the bare repositories of ``repos``.

    ``authorize`` are the FastAPI dependencies through which the deployment decides each request before its
    handler runs; ``record`` makes the app record each request it answers.
    """
    app = FastAPI(dependencies=list(authorize), openapi_url=None, docs_url=None, redoc_url=None)
    record(app)

    async def serve_git(repo: str, request: Request, extra: Mapping[str, str] | None = None) -> Response:
        if repo not in repos:
            raise HTTPException(HTTPStatus.NOT_FOUND, f"no repository {repo!r}")
        path = request.url.path.removeprefix(f"/git/{repo}")
        return await run_http_backend(bounds, f"/{repo}{REPOSITORY_SUFFIX}{path}", request, extra)

    @app.get("/git/{repo}/info/refs")
    async def info_refs(repo: str, request: Request) -> Response:
        """List the repository's refs: the first request of every clone, fetch, pull and push."""
        return await serve_git(repo, request)

    @app.post("/git/{repo}/git-upload-pack")
    async def upload_pack(repo: str, request: Request) -> Response:
        """Send the commits the client asks for: a clone's, fetch's or pull's download."""
        return await serve_git(repo, request)

    @app.post("/git/{repo}/git-receive-pack")
    async def receive_pack(repo: str, request: Request) -> Response:
        """Take the commits and ref updates a push sends; the repository's hook may refuse them.

        The request's record names each ref the push moved (``updates``), as the repository's hook wrote it,
        even if git then took too long to answer.
        """
        with tempfile.TemporaryDirectory() as scratch:
            updates = Path(scratch) / "updates"
            try:
                return await serve_git(repo, request, {REF_UPDATES_VARIABLE: str(updates)})
            finally:
                request.state.arguments = {"repo": repo, "updates": read_ref_updates(updates)}

    return app


async def run_http_backend(
    bounds: GitBounds,
    path_info: str,
    request: Request,
    extra: Mapping[str, str] | None = None,
) -> Response:
    """Answer ``request`` for ``path_info`` with git's CGI program ``http-backend``, within the bounds set.

    ``extra`` adds to git's environment, and so to its hooks'.
    """
    body = await read_body(request, bounds.request_limit_bytes)
    environment = {
        **(extra or {}),
        "PATH": os.environ["PATH"],
        **ISOLATED_GIT,
        **_SERVER_CONFIG,
        "GIT_PROJECT_ROOT": str(bounds.repositories),
        "GIT_HTTP_EXPORT_ALL": "1",
        "PATH_INFO": path_info,
        "REQUEST_METHOD": request.method,
        "QUERY_STRING": request.url.query,
        "CONTENT_TYPE": request.headers.get("content-type", ""),
        "CONTENT_LENGTH": str(len(body)),
        "HTTP_CONTENT_ENCODING": request.headers.get("content-encoding", ""),
        "GIT_PROTOCOL": request.headers.get("git-protocol", ""),
    }
    with anyio.move_on_after(bounds.timeout_seconds) as deadline:
        result = await anyio.run_process(["git", "http-backend"], input=body, env=environment, check=False)
    if deadline.cancelled_caught:  # the process was killed when its wait was cancelled
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, "git took too long to answer")
    end = _HEADERS_END.search(result.stdout)
    if end is None:
        raise HTTPException(HTTPStatus.INTERNAL_SERVER_ERROR, "git http-backend gave no response")
    status, headers = HTTPStatus.OK, {}
    for line in result.stdout[: end.start()].decode().splitlines():
        name, _, value = line.partition(":")
        if name.lower() == "status":
            status = HTTPStatus(int(value.split()[0]))
        else:
            headers[name] = value.strip()
    return Response(result.stdout[end.end() :], status, headers)


def read_ref_updates(path: Path) -> list[RefUpdate]:
    """The refs a push moved, as its hook wrote them to ``path``: none if it wrote nothing."""
    lines = path.read_text().splitlines() if path.exists() else []
    return [RefUpdate(ref=ref, old=old, new=new) for old, new, ref in (line.split(" ", 2) for line in lines)]


async def read_body(request: Request, limit: int) -> bytes:
    """``request``'s body, refused with 413 as soon as it holds more than ``limit`` bytes."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            raise HTTPException(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"a request is at most {limit} bytes")
    return bytes(body)
