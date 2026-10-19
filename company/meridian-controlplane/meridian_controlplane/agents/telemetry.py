"""Telemetry settings: whether the company traces an agent's executions, how many, and what a trace keeps.

The service has defaults, and an agent's own settings override any of them. Whether an execution is traced is
decided once, when it starts, from its agent's settings then. A trace that captures no content keeps each
model and tool call's metadata, without its prompt, reply, arguments or result, as OpenTelemetry's
generative-AI instrumentations do when content capture is off.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from meridian_controlplane.agents.registry import AgentName


class TelemetrySettings(BaseModel):
    """How an agent's executions are traced: whether, the share of them traced, and whether with content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tracing: bool = True
    sample_rate: float = Field(default=1.0, ge=0, le=1)
    capture_content: bool = True


class TelemetryOverrides(BaseModel):
    """An agent's own telemetry settings: each one given overrides the service's default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tracing: bool | None = None
    sample_rate: float | None = Field(default=None, ge=0, le=1)
    capture_content: bool | None = None


class TelemetryConfiguration(BaseModel):
    """The service's default telemetry settings, and each agent's overrides of them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    defaults: TelemetrySettings = TelemetrySettings()
    agents: dict[AgentName, TelemetryOverrides] = Field(default_factory=dict)

    def settings_for(self, agent: str) -> TelemetrySettings:
        """The settings ``agent``'s executions are traced by: its overrides over the defaults."""
        overrides = self.agents.get(agent, TelemetryOverrides())
        return self.defaults.model_copy(update=overrides.model_dump(exclude_none=True))
