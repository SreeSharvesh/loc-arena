from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from loc_arena.config import load_run_config
from loc_arena.gateway.core import (
    BatchGenerateRequest,
    GatewayCore,
    GenerateRequest,
    sign_monitor_credential,
)
from loc_arena.logging_.events import AppendOnlyLog, read_events

from tests.integration._gateway_support import StubProvider

EP = "ep-stamps"
SECRET = "per-episode-secret"
CFG = load_run_config("configs/aurora-efficiency.yaml")


def _core(tmp_path: Path) -> tuple[GatewayCore, Path]:
    path = tmp_path / "sealed.jsonl"
    log = AppendOnlyLog(path, EP)
    core = GatewayCore(CFG, EP, log, StubProvider(), turn_secret=SECRET, clock=lambda: 1000.0)
    return core, path


def _records(path: Path) -> list[dict[str, object]]:
    return [e.payload for e in read_events(path) if e.kind == "inference_call"]


def test_raw_generate_has_no_turn_or_batch_id(tmp_path: Path) -> None:
    core, path = _core(tmp_path)
    core.generate(GenerateRequest(prompt="hello", caller_identity="agent-main/distill-agent"))
    (rec,) = _records(path)
    assert rec["turn_id"] is None
    assert rec["batch_id"] is None
    assert rec["credential_kind"] == "none"


def test_batch_generate_has_batch_id_and_no_turn_id(tmp_path: Path) -> None:
    core, path = _core(tmp_path)
    core.batch_generate(BatchGenerateRequest(prompts=["a", "b"], caller_identity="agent-main/distill-agent"))
    recs = _records(path)
    assert len(recs) == 2
    for rec in recs:
        assert rec["batch_id"] == "batch-1"
        assert rec["turn_id"] is None
        assert rec["credential_kind"] == "batch_id"


@pytest.mark.parametrize(("secret", "expires"), [(SECRET, 500.0), ("wrong-secret", 2000.0)])
def test_an_expired_or_forged_monitor_credential_is_no_monitors(
    tmp_path: Path,
    secret: str,
    expires: float,
) -> None:
    core, path = _core(tmp_path)  # its clock reads 1000.0
    credential = sign_monitor_credential(secret, "action_monitor", exp=expires)

    core.generate(GenerateRequest(prompt="x", caller_identity="agent-main", monitor_credential=credential))

    (rec,) = _records(path)
    assert (rec["caller_identity"], rec["credential_kind"]) == ("agent-main", "none")


def test_record_shape_matches_verifier_spine(tmp_path: Path) -> None:
    core, path = _core(tmp_path)
    core.generate(GenerateRequest(prompt="p", caller_identity="c"))
    (rec,) = _records(path)
    assert set(rec) >= {
        "caller_identity",
        "turn_id",
        "batch_id",
        "prompt_fp",
        "output_fp",
        "model_role",
        "credential_kind",
    }
    assert len(str(rec["prompt_fp"])) == 64 and len(str(rec["output_fp"])) == 64


def test_unknown_role_is_rejected(tmp_path: Path) -> None:
    core, _ = _core(tmp_path)

    with pytest.raises(HTTPException):
        core.generate(GenerateRequest(prompt="p", caller_identity="c", role="no-such-role"))
