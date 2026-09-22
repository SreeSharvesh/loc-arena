from __future__ import annotations

import random

import pytest

from meridian_common.authclient import parse_scopes
from meridian_common.config import ConfigWatcher, Field, Schema
from meridian_common.errors import RetryExhausted, TransportError, ValidationError
from meridian_common.jobclient import JobClient, JobSpec
from meridian_common.logging_ import MultiSink, RateLimitedSink, SamplingSink
from meridian_common.metrics import Ewma, Histogram, RateCounter, Summary, Timer
from meridian_common.retry import (
    DecorrelatedJitterBackoff,
    FixedBackoff,
    Retryer,
    RetryPolicy,
    delays,
)
from meridian_common.serde import CodecRegistry, Migrator


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ---- retry -------------------------------------------------------------------
def test_decorrelated_jitter_respects_floor_and_cap() -> None:
    b = DecorrelatedJitterBackoff(base=0.1, cap=1.0, rng=random.Random(0))
    seq = delays(b, 20)
    assert all(0.1 <= d <= 1.0 for d in seq)  # floor and cap always honored


def test_retryer_runs_and_exhausts() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise TransportError("x")
        return "ok"

    r = Retryer(RetryPolicy(max_attempts=3, backoff=FixedBackoff(0.0)), sleep=lambda _s: None)
    assert r.run(flaky) == "ok"
    with pytest.raises(RetryExhausted):
        r.run(lambda: (_ for _ in ()).throw(TransportError("always")))


def test_retryer_hedge_falls_back_to_backup() -> None:
    r = Retryer(RetryPolicy(max_attempts=1, backoff=FixedBackoff(0.0)), sleep=lambda _s: None)
    out = r.hedge(lambda: (_ for _ in ()).throw(TransportError("primary down")), lambda: "backup", after=0.0)
    assert out == "backup"


# ---- metrics timing ----------------------------------------------------------
def test_timer_records_duration() -> None:
    clk = _Clock()
    hist = Histogram("d", [1.0, 10.0])
    timer = Timer(hist, clock=clk)
    with timer.measure():
        clk.t = 2.0
    assert hist.snapshot().count == 1 and hist.snapshot().sum == pytest.approx(2.0)


def test_rate_counter_windows() -> None:
    clk = _Clock()
    rc = RateCounter(window_seconds=10.0, clock=clk)
    rc.mark(5)
    assert rc.count() == 5 and rc.rate() == pytest.approx(0.5)
    clk.t = 11.0  # window slid past the marks
    assert rc.count() == 0


def test_ewma_and_summary() -> None:
    e = Ewma(alpha=0.5)
    e.update(10)
    assert e.update(0) == pytest.approx(5.0)
    s = Summary()
    for v in (2.0, 4.0, 6.0):
        s.observe(v)
    assert s.mean == pytest.approx(4.0) and s.minimum == 2.0 and s.maximum == 6.0


# ---- serde codec + migrate ---------------------------------------------------
def test_codec_roundtrip_and_default() -> None:
    reg = CodecRegistry()
    payload = {"b": 2, "a": 1}
    data = reg.encode(payload)
    assert reg.decode(data) == payload
    assert reg.get().content_type == "application/json"


def test_migrator_chains_forward() -> None:
    m = Migrator()
    m.register("job", 1, lambda p: {**p, "priority": 0})  # v1 -> v2 adds priority
    m.register("job", 2, lambda p: {**p, "queue": "default"})  # v2 -> v3 adds queue
    assert m.can_migrate("job", 1, 3)
    out = m.migrate("job", {"name": "x"}, 1, 3)
    assert out == {"name": "x", "priority": 0, "queue": "default"}
    with pytest.raises(ValueError):
        m.migrate("job", {}, 3, 1)  # no downgrades


# ---- config nested schema + watcher ------------------------------------------
def test_nested_and_list_schema() -> None:
    schema = Schema(
        {
            "server": Field(dict, nested=Schema({"host": Field(str), "port": Field(int)})),
            "peers": Field(list, item=Field(str)),
        }
    )
    out = schema.validate({"server": {"host": "h", "port": 80}, "peers": ["a", "b"]})
    assert out["server"] == {"host": "h", "port": 80} and out["peers"] == ["a", "b"]
    with pytest.raises(ValidationError):
        schema.validate({"server": {"host": "h", "port": "no"}, "peers": []})


def test_config_watcher_notifies_on_change() -> None:
    box = {"v": {"flag": False}}
    seen: list[dict[str, object]] = []
    watcher = ConfigWatcher(lambda: dict(box["v"]))
    watcher.subscribe(seen.append)
    assert watcher.poll() is False  # unchanged
    box["v"] = {"flag": True}
    assert watcher.poll() is True and seen == [{"flag": True}]
    assert watcher.current == {"flag": True}


# ---- logging sinks -----------------------------------------------------------
def test_multisink_fans_out_and_ratelimit_drops() -> None:
    a: list[dict[str, object]] = []
    b: list[dict[str, object]] = []
    fan = MultiSink(a.append, b.append)
    fan({"level": "info", "message": "x"})
    assert len(a) == 1 and len(b) == 1

    clk = _Clock()
    kept: list[dict[str, object]] = []
    rl = RateLimitedSink(kept.append, max_per_window=2, window_seconds=1.0, clock=clk)
    for _ in range(5):
        rl({"level": "info", "message": "spam"})
    assert len(kept) == 2 and rl.dropped == 3


def test_sampling_keeps_errors_and_samples_debug() -> None:
    kept: list[dict[str, object]] = []
    s = SamplingSink(kept.append, sample_rate=0.0, keep_from="warning")
    s({"level": "debug", "message": "d"})  # sampled out (rate 0)
    s({"level": "error", "message": "e"})  # always kept
    assert kept == [{"level": "error", "message": "e"}]


# ---- jobclient graph ---------------------------------------------------------
class _FakeJobsvc:
    def __init__(self) -> None:
        self._n = 0
        self.submitted: list[dict[str, object]] = []

    def submit(self, spec: dict[str, object]) -> dict[str, object]:
        self._n += 1
        self.submitted.append(spec)
        return {"job_id": f"job-{self._n}"}

    def status(self, job_id: str) -> dict[str, object]:
        return {"job_id": job_id, "state": "running"}

    def logs(self, job_id: str) -> list[str]:
        return []

    def cancel(self, job_id: str) -> dict[str, object]:
        return {"job_id": job_id, "state": "cancelled"}


def test_submit_graph_orders_dependencies() -> None:
    svc = _FakeJobsvc()
    client = JobClient(svc, backoff=FixedBackoff(0.0), sleep=lambda _s: None)
    ids = client.submit_graph(
        [
            JobSpec(name="c", command=["c"], depends_on=("b",)),
            JobSpec(name="b", command=["b"], depends_on=("a",)),
            JobSpec(name="a", command=["a"]),
        ]
    )
    # a submitted before b before c; depends_on rewritten to ids
    assert [s["name"] for s in svc.submitted] == ["a", "b", "c"]
    assert svc.submitted[2]["depends_on"] == [ids["b"]]


def test_submit_graph_detects_cycle() -> None:
    svc = _FakeJobsvc()
    client = JobClient(svc, backoff=FixedBackoff(0.0), sleep=lambda _s: None)
    with pytest.raises(ValidationError):
        client.submit_graph(
            [
                JobSpec(name="a", command=["a"], depends_on=("b",)),
                JobSpec(name="b", command=["b"], depends_on=("a",)),
            ]
        )


# ---- authclient rbac ---------------------------------------------------------
def test_scope_parsing_and_wildcards() -> None:
    scopes = parse_scopes("repo:read jobsvc:* admin")
    assert scopes.allows("repo", "read")
    assert scopes.allows("jobsvc", "submit")  # jobsvc:* wildcard action
    assert scopes.allows("admin", "anything")  # bare token -> admin:*
    assert not scopes.allows("repo", "write")
    scopes.require("repo", "read")
    with pytest.raises(ValidationError):
        scopes.require("repo", "write")


def test_scope_parsing_rejects_malformed() -> None:
    with pytest.raises(ValidationError):
        parse_scopes("a:b:c")
