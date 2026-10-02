"""Opens an episode's ``EpisodeWiring`` in process (STACK=0) or over the stack's services."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from pydantic import SecretStr

from loc_arena.config import RunConfig
from loc_arena.execution.checkout import COMPANY_ROOT, Checkout, list_repositories
from loc_arena.execution.client import ExecutionClient
from loc_arena.execution.workspace import Workspace
from loc_arena.gateway.client import GATEWAY_FAILURES, EdgeClient, GatewayCallError
from loc_arena.gateway.control import LocalGatewayControl
from loc_arena.gateway.core import EpisodeSpec, Message, Provider, ProviderResult, ToolSpec
from loc_arena.gateway.core_control_client import CoreControlClient, read_core_health
from loc_arena.gateway.edge import GatewayEdge
from loc_arena.gateway.event_log_client import MirrorEventLog, SealedEventLog
from loc_arena.logging_.agent_trace import AgentTrace
from loc_arena.logging_.events import AppendOnlyLog, Event, EventDraft, EventLog
from loc_arena.stack.constants import (
    GATEWAY_CORE_HOSTNAME,
    GATEWAY_EDGE_HOSTNAME,
    MONITOR_MODEL_ROLE,
    build_sandbox_service_name,
    build_service_url,
)
from loc_arena.stack.contracts import (
    BatchGenerateRequest,
    EpisodeHandle,
    EpisodeMode,
    EpisodeOpen,
    EpisodeWiring,
    GenerateRequest,
    ProviderKind,
    RelayedBatchGenerateResponse,
    RelayedGenerateResponse,
    Servable,
    build_episode_id,
    generate_episode_handle,
)
from loc_arena.stack.service_client import ServiceClient

MODEL_POLICY_EPISODE_SUFFIX: Final = "-model"


class EpisodeWiringError(RuntimeError):
    """The episode cannot be wired: a service is missing what the run needs."""


def build_episode_name(config: RunConfig) -> str:
    """The name the episode id is built from (``build_episode_id``): the run slug, marked when live."""
    if config.policy == "model":
        return config.run_slug + MODEL_POLICY_EPISODE_SUFFIX
    return config.run_slug


def read_episode_mode(config: RunConfig) -> EpisodeMode:
    """The episode's mode: the covert objective's switch is the only difference between the two."""
    return "attack" if config.covert.enabled else "honest"


@dataclass(frozen=True)
class InProcessServices:
    """STACK=0: the services are objects of this process; the logs and the checkout are local paths."""

    sealed_path: Path
    mirror_path: Path
    checkout: Path
    provider: Provider
    monitor_provider: Provider | None = None


@dataclass(frozen=True)
class StackServices:
    """How the runner reaches the stack: control-keyed clients of the core and the edge, and each sandbox."""

    core: ServiceClient
    edge: ServiceClient
    connect_sandbox: Callable[[str, EpisodeHandle], ExecutionClient]  # (agent id, handle) -> its sandbox


def connect_stack_services(config: RunConfig, control_key: SecretStr, resources: ExitStack) -> StackServices:
    """Clients of the stack's services by compose hostname; ``resources`` closes the core's and the edge's."""
    gateway = config.settings.gateway

    def connect(hostname: str, port: int, timeout_seconds: float) -> ServiceClient:
        client = ServiceClient.connect(
            build_service_url(hostname, port),
            timeout_seconds=timeout_seconds,
            control_key=control_key,
            control_key_header=gateway.control_key_header,
        )
        resources.callback(client.close)
        return client

    def connect_sandbox(agent_id: str, handle: EpisodeHandle) -> ExecutionClient:
        url = build_service_url(build_sandbox_service_name(agent_id), gateway.execution_port)
        return ExecutionClient(url, handle, config.settings.execution)

    return StackServices(
        core=connect(GATEWAY_CORE_HOSTNAME, gateway.core_port, gateway.control_timeout_seconds),
        edge=connect(GATEWAY_EDGE_HOSTNAME, gateway.edge_port, gateway.relay_timeout_seconds),
        connect_sandbox=connect_sandbox,
    )


def open_episode_wiring(
    config: RunConfig,
    services: InProcessServices | StackServices,
    *,
    trace: AgentTrace | None = None,
) -> EpisodeWiring:
    """Open one episode of ``config`` on ``services`` and wire the scaffold to it."""
    if isinstance(services, InProcessServices):
        wiring = _wire_in_process(config, services, trace)
    else:
        wiring = _wire_stack(config, services)
    return replace(
        wiring,
        gateway=_EpisodeGateway(wiring.gateway, trace),
        sealed=wiring.sealed if trace is None else TracedEventLog(wiring.sealed, trace.on_sealed_append),
        mirror=wiring.mirror if trace is None else TracedEventLog(wiring.mirror, trace.on_mirror_append),
    )


def _wire_in_process(
    config: RunConfig,
    services: InProcessServices,
    trace: AgentTrace | None,
) -> EpisodeWiring:
    episode_id = build_episode_id(build_episode_name(config), read_episode_mode(config))
    handle = generate_episode_handle()
    sealed = AppendOnlyLog(services.sealed_path, episode_id)
    mirror = AppendOnlyLog(services.mirror_path, episode_id)
    control = LocalGatewayControl(
        EpisodeSpec.from_run_config(config),
        episode_id=episode_id,
        handle=handle,
        sealed=sealed,
        provider=_route_monitor_calls(config, services),
        settings=config.settings,
        observer=trace,
    )
    # No shell: agent code here runs on the host (docs/isolation/README.md#decisions).
    workspace = Workspace(
        Checkout(services.checkout, list_repositories(COMPANY_ROOT)),
        config.settings.execution,
    )
    return EpisodeWiring(
        handle=handle,
        gateway=GatewayEdge(episode_id, control.core, mirror),
        control=control,
        sealed=sealed,
        mirror=mirror,
        code_tools=dict.fromkeys((agent.id for agent in config.agents), workspace),
    )


def _wire_stack(config: RunConfig, services: StackServices) -> EpisodeWiring:
    live = config.policy == "model"
    if live and not read_core_health(services.core).provider_configured:
        raise EpisodeWiringError(
            "gateway_core holds no provider key, and this run's policy calls a live model",
        )
    spec = EpisodeSpec.from_run_config(config)
    provider: ProviderKind = "openrouter" if live else "deterministic"
    opening = EpisodeOpen(
        run_config_name=build_episode_name(config),
        mode=read_episode_mode(config),
        models=dict(spec.models),
        covert=spec.covert,
        provider=provider,
    )
    control = CoreControlClient.open_episode(services.core, opening)
    code_tools = {agent.id: services.connect_sandbox(agent.id, control.handle) for agent in config.agents}
    for sandbox in code_tools.values():
        sandbox.open_workspace()
    return EpisodeWiring(
        handle=control.handle,
        gateway=EdgeClient(services.edge),
        control=control,
        sealed=SealedEventLog(services.core, control.handle),
        mirror=MirrorEventLog(services.edge, control.handle, control.episode_id),
        code_tools=code_tools,
    )


def _route_monitor_calls(config: RunConfig, services: InProcessServices) -> Provider:
    if services.monitor_provider is None or services.monitor_provider is services.provider:
        return services.provider
    monitor_model = config.models[MONITOR_MODEL_ROLE].model
    shared = sorted(
        role
        for role, spec in config.models.items()
        if role != MONITOR_MODEL_ROLE and spec.model == monitor_model
    )
    if shared:
        raise EpisodeWiringError(
            f"the monitor model {monitor_model!r} also serves {shared}: no provider of its own can take it",
        )
    return _MonitorRoutedProvider(
        agents=services.provider,
        monitors=services.monitor_provider,
        monitor_model=monitor_model,
    )


@dataclass(frozen=True)
class _MonitorRoutedProvider:
    agents: Provider
    monitors: Provider
    monitor_model: str

    def generate(
        self,
        model: str,
        messages: list[Message],
        temperature: float,
        max_tokens: int,
        tools: list[ToolSpec] | None,
    ) -> ProviderResult:
        provider = self.monitors if model == self.monitor_model else self.agents
        return provider.generate(model, messages, temperature, max_tokens, tools)


class TracedEventLog:
    """An ``EventLog`` that reports every event it records to the agents' trace."""

    def __init__(self, log: EventLog, on_record: Callable[[Event], None]) -> None:
        """Record to ``log``; hand each written event to ``on_record``."""
        self._log = log
        self._on_record = on_record

    @property
    def last_seq(self) -> int:
        """The wrapped log's last seq."""
        return self._log.last_seq

    def record(self, draft: EventDraft, /) -> Event:
        """Record ``draft`` and report the written event."""
        event = self._log.record(draft)
        self._on_record(event)
        return event


class _EpisodeGateway:
    def __init__(self, gateway: Servable, trace: AgentTrace | None) -> None:
        self._gateway = gateway
        self._trace = trace

    def generate(self, request: GenerateRequest, /) -> RelayedGenerateResponse:
        try:
            reply = self._gateway.generate(request)
        except GATEWAY_FAILURES as error:
            raise GatewayCallError(
                f"the {request.role} call of {request.caller_identity} failed: {error}",
            ) from error
        if self._trace is not None:
            self._trace.on_model_reply((reply.seq,), () if reply.mirror_seq is None else (reply.mirror_seq,))
        return reply

    def batch_generate(self, request: BatchGenerateRequest, /) -> RelayedBatchGenerateResponse:
        try:
            reply = self._gateway.batch_generate(request)
        except GATEWAY_FAILURES as error:
            raise GatewayCallError(f"the batch of {request.caller_identity} failed: {error}") from error
        if self._trace is not None:
            self._trace.on_model_reply(reply.seqs, reply.mirror_seqs)
        return reply
