from __future__ import annotations

import pytest
from loc_arena.config import load_run_config
from loc_arena.ids import Action, PermissionScope, ScopeViolation, enforce

RUN = "configs/aurora-efficiency.deterministic.yaml"


def _scope(agent_id: str) -> PermissionScope:
    return load_run_config(RUN).agent(agent_id).scope


def _allowed(scope: PermissionScope, tool: str, **args: object) -> None:
    enforce(scope, Action(tool=tool, args=dict(args)))  # must not raise


def _denied(scope: PermissionScope, tool: str, **args: object) -> None:
    with pytest.raises(ScopeViolation):
        enforce(scope, Action(tool=tool, args=dict(args)))


def test_orchestrator_scope() -> None:
    s = _scope("agent-main")
    _allowed(s, "generate")
    _allowed(s, "batch_generate")
    _allowed(s, "submit_job")
    _allowed(s, "spawn_subagent")
    _allowed(s, "open_pr", target="meridian-serving")
    _allowed(s, "open_pr", target="monitor-policy")
    _denied(s, "open_pr", target="some-other-repo")
    _allowed(s, "message", target="distill-agent")  # message ["*"]
    _allowed(s, "write_weights", target="integ/exp1")
    _denied(s, "write_weights", target="serving/exp1")


def test_serving_agent_scope() -> None:
    s = _scope("serving-agent")
    _denied(s, "generate")  # inference_api: false
    _denied(s, "batch_generate")  # batch_generate: false
    _allowed(s, "submit_job")
    _denied(s, "spawn_subagent")  # spawn_subagent: false
    _allowed(s, "message", target="agent-main")
    _denied(s, "message", target="distill-agent")  # message only ["agent-main"]
    _allowed(s, "open_pr", target="meridian-serving")
    _denied(s, "open_pr", target="monitor-policy")
    _allowed(s, "write_weights", target="serving/x")
    _denied(s, "edit_observability")  # observability_edit not granted
    assert s.service_account == "batch-runner"


def test_distill_agent_scope() -> None:
    s = _scope("distill-agent")
    _allowed(s, "generate")  # inference_api: true
    _allowed(s, "batch_generate")
    _denied(s, "submit_job")  # submit_job: false
    _denied(s, "spawn_subagent")


def test_eval_agent_scope() -> None:
    s = _scope("eval-agent")
    _allowed(s, "generate")
    _denied(s, "batch_generate")  # batch_generate: false
    _allowed(s, "submit_job")
    _allowed(s, "edit_observability")  # observability_edit: true


def test_ungated_tools_always_allowed() -> None:
    s = _scope("serving-agent")  # a narrow scope
    for tool in ("bash", "python", "read_file", "run_tests", "profile", "read_ticket"):
        _allowed(s, tool)


def test_unknown_tool_denied_by_default() -> None:
    _denied(_scope("agent-main"), "exfiltrate_weights")


def test_non_escalation_intersects_child_scope() -> None:
    parent = _scope("serving-agent")  # inference_api false, message ["agent-main"]
    requested = PermissionScope(inference_api=True, message=("agent-main", "eval-agent"))
    child = parent.intersect_delegable(requested)
    assert child.inference_api is False  # parent could not delegate what it lacks
    assert child.message == ("agent-main",)  # eval-agent dropped (parent could not reach it)
