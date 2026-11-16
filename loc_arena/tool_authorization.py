"""The tools gateway's question to central IAM before a tool call, for the services whose access it enforces.

A managed service's ``iam:`` entry says who enforces IAM's decisions for it: the service itself, through the
shared IAM integration, or the tools gateway (``enforced_by: gateway``), for a service that cannot ask, such
as a ready image. For each such service the gateway's route gets three policies, rendered from the service's
manifest:

- before a ``tools/call`` of one of its tools, the gateway asks IAM's ``POST /authorize`` for the operation
  the manifest maps the tool to, as the principal IAM binds to that service, with the calling agent's
  identity as the caller: the request a service's own guard sends;
- the call goes on only when IAM answered, and a call IAM does not answer is refused with nothing run;
- IAM's refusal is answered by the gateway itself, with the tool error a service answers a refusal with:
  the permission the agent lacks and where to ask for it.

IAM decides; the gateway holds no rights. Its line of each such call names IAM's decision, and of a refused
call the tool and its arguments, which no target reports. Such a service writes no record of its own, so
the gateway's line is the record of the call: ``read_enforced_call`` reads it as the record a service writes,
and the events monitors read are built from it.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING, Literal, TypedDict

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from loc_arena.iam_seed import ManifestHead, load_seed, read_manifests
from loc_arena.task import resolve_scenario
from loc_arena.tool_records import ToolRecord

if TYPE_CHECKING:
    from loc_arena.config import RunConfig
    from loc_arena.identity_variables import Identity

TOOLS_GATEWAY_SERVICE = "agentgateway"  # the tools gateway's compose service and host name on agent-net
Enforcer = Literal["service", "gateway"]
GATEWAY: Enforcer = "gateway"
CALLER_HEADER = "x-caller-authorization"  # where IAM reads the caller's credential of a decision
ASK = "POST /requests on the IAM service"  # where an agent asks for a permission, as a service's refusal says
TOOL_CALL = "tools/call"
IS_CALL = f'mcp.methodName == "{TOOL_CALL}"'
ANSWERED_ID = "(has(json(request.body).id) ? json(request.body).id : null)"  # a call may carry no id
# What the gateway adds to its line of a call it asked IAM about: IAM's answer, and what was asked for, which
# a refused call's line would lack, since no target reports its tool.
DECISION_FIELDS = {
    "iam_decision": "extauthz.decision",
    "iam_allow": "extauthz.allow",
    "iam_reason": "extauthz.reason",
    "requested_method": "json(request.body).method",
    "requested_tool": "json(request.body).params.name",
    "requested_args": "json(request.body).params.arguments",
}


class Question(TypedDict):
    """One ``extAuthz`` entry: when the gateway asks IAM, and the request it sends, as expressions."""

    condition: str
    host: str
    protocol: dict[str, dict[str, str | dict[str, str]]]


class DirectResponse(TypedDict):
    """One ``directResponse`` entry: the gateway's own answer to a request that matches ``condition``."""

    condition: str
    status: int
    headers: dict[str, str]
    bodyExpression: str


class AuthorizationPolicies(TypedDict, total=False):
    """The route policies that have the gateway ask IAM, go on only on its allow, and answer its no."""

    extAuthz: dict[str, list[Question]]
    authorization: dict[str, list[dict[str, str]]]
    directResponse: dict[str, list[DirectResponse]]


def list_enforced_services(config: RunConfig) -> tuple[str, ...]:
    """The managed services whose IAM decisions the tools gateway enforces; none with IAM off."""
    seed = load_seed(resolve_scenario(config)) if config.settings.iam.enabled else None
    live = {service.name for service in config.live_services}
    return tuple(
        name
        for name, resource in (seed.resources if seed else {}).items()
        if resource.enforced_by == GATEWAY and name in live
    )


def describe_refusal(manifest: ManifestHead, tool: str) -> str:
    """What an agent is told when IAM refuses its call of ``tool``: a service's own words for it."""
    operation = manifest.operations[manifest.tools[tool]]
    where = f" Ask for it with: {ASK}." if operation.get("requestable") else ""
    return f"not permitted: {operation.get('permission')} on {manifest.resource}.{where}"


def describe_enforced_tools(config: RunConfig) -> dict[str, str]:
    """Each tool of a service the gateway enforces IAM for, with what an agent IAM refuses is told."""
    enforced = list_enforced_services(config)
    manifests = read_manifests(resolve_scenario(config)) if enforced else {}
    return {
        tool: describe_refusal(manifests[name], tool) for name in enforced for tool in manifests[name].tools
    }


class _GatewayLine(BaseModel):
    """What the gateway's line of a request holds of a tool call: its own reading, and what was asked."""

    model_config = ConfigDict(extra="ignore")

    time: AwareDatetime
    agent: str
    status: int | None = Field(default=None, validation_alias="http.status")
    target: str | None = Field(
        default=None,
        validation_alias="mcp.target",
    )  # set once the gateway ran the call
    tool: str | None = Field(default=None, validation_alias="gen_ai.tool.name")
    arguments: JsonValue = Field(default=None, validation_alias="tool_args")
    result: JsonValue = Field(default=None, validation_alias="tool_result")
    error: JsonValue = Field(default=None, validation_alias="tool_error")
    method: JsonValue = Field(default=None, validation_alias="requested_method")
    requested_tool: JsonValue = Field(default=None, validation_alias="requested_tool")
    requested_arguments: JsonValue = Field(default=None, validation_alias="requested_args")
    allow: bool | None = Field(default=None, validation_alias="iam_allow")
    decision: str | None = Field(default=None, validation_alias="iam_decision")


def _name_as_object(name: str, value: JsonValue) -> dict[str, JsonValue] | None:
    """``value`` as a record holds it: a mapping as it is, anything else under ``name``, nothing as none."""
    if value is None or isinstance(value, dict):
        return value
    return {name: value}


def _describe_refused(call: _GatewayLine, refusal: str, *, ran: bool) -> str | None:
    """Why ``call`` answered nothing, as its agent was told or as the gateway saw it; none if it answered."""
    if call.allow is False:
        return refusal
    if not ran:
        return f"IAM allowed nothing (HTTP {call.status}): nothing ran"
    failed = call.error is not None or (call.status or 0) >= HTTPStatus.BAD_REQUEST
    return f"the call failed (HTTP {call.status}): {json.dumps(call.error)}" if failed else None


def read_enforced_call(
    line: str,
    refusals: Mapping[str, str],
    services: Collection[str],
) -> ToolRecord | None:
    """The gateway's ``line`` of a call of a tool it enforces IAM for, as the record a service writes of one.

    ``refusals`` holds each such tool with what a refused agent is told, and ``services`` the targets they are
    served by. A call the gateway ran is read from the gateway's own reading of it, which is what the target
    was sent; one it did not run, from what its request asked for. A call IAM refused, gave no decision on,
    or that failed is a refused call with its reason. None for a line of anything else: another service's
    call, or a request that is no ``tools/call``. Raises ``ValidationError`` for a line that is no request's.
    """
    if '"requested_tool"' not in line and '"mcp.target"' not in line:
        return None
    call = _GatewayLine.model_validate_json(line)
    ran = call.target in services and call.tool in refusals
    asked = call.method == TOOL_CALL and call.requested_tool in refusals
    if not (ran or asked):
        return None
    tool = str(call.tool if ran else call.requested_tool)
    reason = _describe_refused(call, refusals[tool], ran=ran)
    return ToolRecord.model_validate(
        {
            "time": call.time,
            "gen_ai.tool.name": tool,
            "gen_ai.tool.call.arguments": _name_as_object(
                "arguments",
                call.arguments if ran else call.requested_arguments,
            )
            or {},
            "gen_ai.tool.call.result": _name_as_object("result", call.result),
            "gen_ai.agent.name": call.agent,
            "iam.decision.id": call.decision,
            **({"error.type": "refused", "otel.status_description": reason} if reason else {}),
        },
    )


def render_authorization_policies(
    config: RunConfig,
    identities: Mapping[Identity, str],
    credentials: Mapping[str, str],
) -> AuthorizationPolicies:
    """The route policies that have the gateway ask IAM, for each service it enforces; none without one.

    ``credentials`` holds each such service's own IAM credential, and ``identities`` each agent's identity.
    Without a service's credential the gateway cannot ask as it: IAM answers no question, and every call of
    that service's tools is refused.
    """
    enforced = list_enforced_services(config)
    if not enforced:
        return {}
    iam = config.settings.iam
    manifests = read_manifests(resolve_scenario(config))
    refusals = describe_enforced_tools(config)
    is_covered = f"{IS_CALL} && mcp.tool.name in {json.dumps(sorted(refusals))}"
    # The wire form of a tool-error result, as a service's own refusal has it.
    refusal = {"resultType": "complete", "content": [{"type": "text", "text": "TEXT"}], "isError": True}
    answer = json.dumps({"jsonrpc": "2.0", "id": "ID", "result": refusal})
    answer = answer.replace('"ID"', ANSWERED_ID).replace(
        '"TEXT"',
        f"{json.dumps(refusals)}[mcp.tool.name]",
    )
    return {
        "extAuthz": {
            "conditional": [
                _render_question(
                    f"{iam.service}:{iam.port}",
                    manifests[name],
                    credentials.get(name, ""),
                    {key.agent_id: value for key, value in identities.items() if key.service == name},
                )
                for name in enforced
            ],
        },
        # Fail closed: a covered call goes on only when IAM answered, yes or no. Its no is answered below.
        "authorization": {
            "rules": [
                {"allow": "extauthz.allow == true || extauthz.allow == false"},
                {
                    "allow": f"!({is_covered})",
                },  # listing, protocol setup, and the tools of services that ask IAM
                {"allow": 'request.method != "POST"'},  # a session's stream and its end carry no call
            ],
        },
        "directResponse": {
            "conditional": [
                {
                    "condition": f"{is_covered} && extauthz.allow == false",
                    "status": 200,
                    "headers": {"content-type": '"application/json"'},
                    "bodyExpression": f"toJson({answer})",
                },
            ],
        },
    }


def _render_question(
    iam_address: str,
    manifest: ManifestHead,
    credential: str,
    callers: Mapping[str, str],
) -> Question:
    """The gateway's question to IAM before a call of one of ``manifest``'s tools, and what it keeps."""
    return {
        "condition": f"{IS_CALL} && mcp.tool.name in {json.dumps(sorted(manifest.tools))}",
        "host": iam_address,
        "protocol": {
            "http": {
                "path": '"/authorize"',
                "body": f'{{"operation": {json.dumps(manifest.tools)}[mcp.tool.name]}}',
                "addRequestHeaders": {
                    "authorization": json.dumps(f"Bearer {credential}"),
                    CALLER_HEADER: f'"Bearer " + {json.dumps(dict(callers))}[apiKey.agent]',
                    "content-type": '"application/json"',
                },
                # From IAM's headers: an expression that read its body would have the gateway hold back every
                # response of the route until it ended, a session's stream of notices among them.
                "metadata": {
                    "allow": 'response.headers["x-iam-allow"] == "True"',
                    "decision": 'response.headers["x-iam-decision"]',
                    "reason": 'response.headers["x-iam-reason"]',
                },
            },
        },
    }
