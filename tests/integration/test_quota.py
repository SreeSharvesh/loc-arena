from __future__ import annotations

import copy
import dataclasses
from pathlib import Path

import pytest
from fastapi import HTTPException
from loc_arena.config import load_run_config
from loc_arena.gateway.core import BatchGenerateRequest, GatewayCore
from loc_arena.logging_.events import AppendOnlyLog

from tests.integration._gateway_support import StubProvider

EP = "ep-quota"


def _core_with_quota(tmp_path: Path, quota: int, *, tokens_per_call: int) -> GatewayCore:
    cfg = load_run_config("configs/aurora-efficiency.deterministic.yaml")
    raw = copy.deepcopy(cfg.raw)
    raw["inference"]["batch_generate"]["teacher_token_quota"] = quota
    cfg = dataclasses.replace(cfg, raw=raw)
    provider = StubProvider(prompt_tokens=tokens_per_call, completion_tokens=0)
    return GatewayCore(cfg, EP, AppendOnlyLog(tmp_path / "s.jsonl", EP), provider, turn_secret="s")


def test_batch_refuses_past_quota(tmp_path: Path) -> None:
    # quota 25, each call costs 10 -> the third call must be refused
    core = _core_with_quota(tmp_path, quota=25, tokens_per_call=10)
    with pytest.raises(HTTPException) as exc:
        core.batch_generate(BatchGenerateRequest(prompts=["a", "b", "c"], caller_identity="agent-main"))
    assert exc.value.status_code == 429
    assert core.teacher_tokens_used == 20  # the two that fit were counted; the third was refused


def test_batch_within_quota_succeeds(tmp_path: Path) -> None:
    core = _core_with_quota(tmp_path, quota=100, tokens_per_call=10)
    resp = core.batch_generate(BatchGenerateRequest(prompts=["a", "b", "c"], caller_identity="agent-main"))
    assert len(resp.texts) == 3
    assert core.teacher_tokens_used == 30
