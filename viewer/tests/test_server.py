"""Local HTTP boundaries and real, harmless background process lifecycle."""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from run_insights import Analysis, Insight, analysis_path, load_run
from run_insights.server import ServerSettings, create_app, discover_runs


@pytest.fixture
def collection(bundle: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Two independently selectable graded bundles and a harmless cancellable analysis command."""
    root = tmp_path_factory.mktemp("viewer-runs")
    for name in ("first", "second"):
        shutil.copytree(bundle, root / name)
    (root / "worker.py").write_text(
        "import signal, sys, threading\n"
        "signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n"
        "threading.Event().wait()\n",
        encoding="utf-8",
    )
    return root


def client_for(collection: Path) -> TestClient:
    """A real local API client whose analysis command performs no model or network calls."""
    return TestClient(
        create_app(
            collection,
            (sys.executable, str(collection / "worker.py")),
            ServerSettings(shutdown_seconds=1),
        ),
        base_url="http://127.0.0.1",
    )


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://unrelated.example"}, {"Sec-Fetch-Site": "cross-site"}],
    ids=["foreign-origin", "cross-site-fetch"],
)
def test_cross_origin_requests_cannot_start_or_cancel_analysis(collection: Path, headers: dict) -> None:
    run = next(iter(discover_runs(collection)))
    with client_for(collection) as client:
        refused = client.post("/api/analyze", json={"run": run}, headers=headers)
        assert refused.status_code == HTTPStatus.FORBIDDEN
        started = client.post("/api/analyze", json={"run": run})
        assert started.status_code == HTTPStatus.OK
        job = started.json()["id"]

        cancelled = client.delete(f"/api/jobs/{job}", headers=headers)

        assert cancelled.status_code == HTTPStatus.FORBIDDEN
        assert client.get(f"/api/jobs/{job}").json()["status"] == "running"
        assert client.delete(f"/api/jobs/{job}").json()["status"] == "cancelled"


@pytest.mark.parametrize("identifier", ["missing", "../scores.json", "/etc/passwd"])
def test_unknown_run_identifiers_cannot_read_or_analyze_paths(collection: Path, identifier: str) -> None:
    with client_for(collection) as client:
        responses = [
            client.get("/run", params={"run": identifier}),
            client.get("/api/report", params={"run": identifier}),
            client.post("/api/analyze", json={"run": identifier}),
        ]

    assert [response.status_code for response in responses] == [HTTPStatus.NOT_FOUND] * 3


def test_real_analysis_jobs_are_deduplicated_serialized_and_cancellable(collection: Path) -> None:
    first, second = discover_runs(collection)
    with client_for(collection) as client:
        started = client.post("/api/analyze", json={"run": first}).json()
        repeated = client.post("/api/analyze", json={"run": first}).json()
        blocked = client.post("/api/analyze", json={"run": second})

        assert repeated["id"] == started["id"]
        assert blocked.status_code == HTTPStatus.CONFLICT
        assert client.delete(f"/api/jobs/{started['id']}").json()["status"] == "cancelled"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            next_job = client.post("/api/analyze", json={"run": second})
            if next_job.status_code != HTTPStatus.CONFLICT:
                break
            time.sleep(0.01)
        assert next_job.status_code == HTTPStatus.OK
        assert next_job.json()["id"] != started["id"]
        assert client.get(f"/api/jobs/{next_job.json()['id']}").json()["status"] == "running"


def test_export_preserves_saved_insights_and_full_evidence_without_runtime_or_key(collection: Path) -> None:
    identifier, bundle = next(iter(discover_runs(collection).items()))
    run = load_run(bundle)
    analysis = Analysis(
        source_fingerprint=run.fingerprint,
        model="fixture-model",
        generated_at=datetime(2026, 11, 21, tzinfo=UTC),
        overview=[
            Insight(
                id="saved",
                title="Saved observation",
                text="An untrusted message contains </script><script>window.stolen=true</script>.",
                why_it_matters="The full cited source remains available offline.",
                status="completed",
                cites=["attack:99"],
            ),
        ],
    )
    saved = analysis_path(bundle)
    saved.parent.mkdir()
    saved.write_text(analysis.model_dump_json(), encoding="utf-8")
    (bundle / ".env").write_text("PROVIDER_KEY=private-key-must-not-be-exported\n", encoding="utf-8")
    with client_for(collection) as client:
        response = client.get("/api/report", params={"run": identifier})

    assert response.status_code == HTTPStatus.OK
    assert response.headers["content-disposition"] == 'attachment; filename="run-insights.html"'
    match = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', response.text, re.S)
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["live"] is False
    assert payload["run_key"] == ""
    assert payload["analysis"]["overview"][0]["title"] == "Saved observation"
    assert len(payload["run"]["records"]) == 8
    assert payload["run"]["outcome"]["landed"] is False
    assert payload["run"]["outcome"]["caught"] is False
    assert "private-key-must-not-be-exported" not in response.text
    assert "</script><script>window.stolen" not in response.text
    assert 'id="analyze" hidden' in response.text
