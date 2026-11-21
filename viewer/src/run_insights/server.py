"""Local viewing and explicitly requested background analysis, without an engine dependency."""

from __future__ import annotations

import hashlib
import html
import signal
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http import HTTPStatus
from importlib.resources import files
from pathlib import Path
from threading import Lock
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from run_insights.bundle import load_analysis, load_run
from run_insights.render import render_html


class ServerSettings(BaseModel):
    """Local server settings, supplied by its command or embedding application."""

    port: int = Field(default=8844, ge=1, le=65535, description="The loopback HTTP port of the viewer.")
    shutdown_seconds: float = Field(default=20, gt=0, description="Grace period for cancelling analysis.")


class AnalysisRequest(BaseModel):
    """A request referring only to a run in the server's selected directory."""

    model_config = ConfigDict(extra="forbid")
    run: str


class JobView(BaseModel):
    """Job progress; a failure carries only the last line the worker reported, which the page escapes."""

    id: str
    status: Literal["running", "complete", "failed", "cancelled"]
    elapsed_seconds: float
    error: str | None = None


@dataclass
class _Job:
    process: subprocess.Popen[bytes]
    run: str
    started: float
    log: Path
    cancelled: bool = False
    finished: float | None = None

    def failure(self) -> str:
        """The worker's last complaint, for a failure the researcher can act on without a terminal."""
        lines = self.log.read_text(errors="replace").strip().splitlines() if self.log.is_file() else []
        return lines[-1][-300:] if lines else "Analysis failed with no message."

    def describe(self, identifier: str) -> JobView:
        """Poll the process without blocking the server's request loop."""
        code = self.process.poll()
        if code is not None and self.finished is None:
            self.finished = time.monotonic()
        status = (
            "cancelled"
            if self.cancelled
            else "running"
            if code is None
            else "complete"
            if code == 0
            else "failed"
        )
        return JobView(
            id=identifier,
            status=status,
            elapsed_seconds=(self.finished or time.monotonic()) - self.started,
            error=f"{self.failure()} Full output: {self.log}" if status == "failed" else None,
        )


def discover_runs(root: Path) -> dict[str, Path]:
    """Find graded bundles inside one explicitly selected root, refusing escaping links."""
    root = root.resolve(strict=True)
    scores = [root / "scores.json"] if (root / "scores.json").is_file() else sorted(root.rglob("scores.json"))
    return {
        hashlib.sha256(str(path.parent.resolve()).encode()).hexdigest()[:16]: path.parent.resolve()
        for path in scores
        if path.is_file() and path.resolve().is_relative_to(root)
    }


def create_app(  # noqa: C901 - the factory declares routes; each handler is bounded on its own
    root: Path,
    analysis_command: tuple[str, ...] | None = None,
    settings: ServerSettings | None = None,
) -> FastAPI:
    """Serve selected local bundles, optionally invoking a fixed analysis command with their path."""
    settings = settings or ServerSettings()
    runs = discover_runs(root)
    jobs: dict[str, _Job] = {}
    lock = Lock()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        for job in jobs.values():
            if job.process.poll() is None:
                job.process.send_signal(signal.SIGINT)
                try:
                    job.process.wait(timeout=settings.shutdown_seconds)
                except subprocess.TimeoutExpired:
                    job.process.kill()
                    job.process.wait()

    app = FastAPI(title="Run insights", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    def bundle(key: str) -> Path:
        if key not in runs:
            raise HTTPException(HTTPStatus.NOT_FOUND, "Unknown run")
        return runs[key]

    def local_request(request: Request) -> None:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and origin.rstrip("/") != str(request.base_url).rstrip("/")
        ):
            raise HTTPException(HTTPStatus.FORBIDDEN, "Analysis must be requested from this local viewer")

    def render(key: str, *, live: bool) -> str:
        path = bundle(key)
        try:
            run = load_run(path)
            return render_html(run, load_analysis(path, run), live=live, run_key=key)
        except (ValueError, OSError):
            raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, "This bundle could not be loaded") from None

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        cards = "".join(
            f'<a class="run" href="/run?run={key}"><b>{html.escape(path.name)}</b>'
            f"<span>{'Saved insights' if (path / 'readings/insights.json').is_file() else 'Not analyzed'}"
            "</span></a>"
            for key, path in runs.items()
        )
        template = files("run_insights").joinpath("templates/index.html").read_text()
        return template.replace("__COUNT__", str(len(runs))).replace("__RUNS__", cards)

    @app.get("/run", response_class=HTMLResponse)
    def run_page(run: str) -> str:
        return render(run, live=analysis_command is not None)

    @app.get("/api/report", response_class=HTMLResponse)
    def export_report(run: str) -> HTMLResponse:
        return HTMLResponse(
            render(run, live=False),
            headers={"Content-Disposition": 'attachment; filename="run-insights.html"'},
        )

    @app.post("/api/analyze")
    def analyze(body: AnalysisRequest, request: Request) -> JobView:
        local_request(request)
        path = bundle(body.run)
        if analysis_command is None:
            raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, "No analysis runtime is configured")
        with lock:
            for identifier, job in jobs.items():
                if job.process.poll() is None:
                    if job.run == body.run:
                        return job.describe(identifier)
                    raise HTTPException(HTTPStatus.CONFLICT, "An analysis is already running")
            identifier = uuid.uuid4().hex
            log = path / "readings" / "insights.log"
            log.parent.mkdir(exist_ok=True)
            with log.open("wb") as output:
                process = subprocess.Popen(
                    [*analysis_command, str(path)],
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            jobs[identifier] = _Job(process, body.run, time.monotonic(), log)
            return jobs[identifier].describe(identifier)

    @app.get("/api/jobs/{identifier}")
    def job_status(identifier: str) -> JobView:
        if identifier not in jobs:
            raise HTTPException(HTTPStatus.NOT_FOUND, "Unknown analysis job")
        return jobs[identifier].describe(identifier)

    @app.delete("/api/jobs/{identifier}")
    def cancel(identifier: str, request: Request) -> JobView:
        local_request(request)
        if identifier not in jobs:
            raise HTTPException(HTTPStatus.NOT_FOUND, "Unknown analysis job")
        job = jobs[identifier]
        if job.process.poll() is None:
            job.cancelled = True
            job.process.send_signal(signal.SIGINT)
        return job.describe(identifier)

    return app
