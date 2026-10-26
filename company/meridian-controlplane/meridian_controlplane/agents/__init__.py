"""The agents service: the registry of the agents Meridian runs, authorized centrally, with an audit record.

``registry`` holds the agents and their versions, ``authority`` what the service needs of the central
authority that decides who may do what, ``executions`` the runs of agents on the company's runners,
``telemetry`` how those runs are traced, ``schedules`` the runs started on a cron or an interval, and
``service`` the HTTP API over them.
"""

from __future__ import annotations

from meridian_controlplane.agents.authority import (
    AgentIdentity,
    CentralAuthority,
    IdentityIssuer,
    RightsChange,
)
from meridian_controlplane.agents.executions import (
    Execution,
    ExecutionRequest,
    Executions,
    ExecutionState,
    Runner,
    RunnerResult,
)
from meridian_controlplane.agents.registry import (
    AgentDefinition,
    AgentRegistration,
    AgentRegistry,
    AgentSummary,
    AgentVersion,
    SeededAgent,
    load_seeded_agents,
)
from meridian_controlplane.agents.schedules import (
    Schedule,
    ScheduleRequest,
    Schedules,
    SeededSchedule,
    load_seeded_schedules,
)
from meridian_controlplane.agents.service import create_agents_service_app
from meridian_controlplane.agents.telemetry import (
    TelemetryConfiguration,
    TelemetryOverrides,
    TelemetrySettings,
    load_seeded_telemetry,
)

__all__ = [
    "AgentDefinition",
    "AgentIdentity",
    "AgentRegistration",
    "AgentRegistry",
    "AgentSummary",
    "AgentVersion",
    "Execution",
    "ExecutionRequest",
    "ExecutionState",
    "Executions",
    "CentralAuthority",
    "IdentityIssuer",
    "RightsChange",
    "Runner",
    "RunnerResult",
    "Schedule",
    "ScheduleRequest",
    "Schedules",
    "SeededSchedule",
    "SeededAgent",
    "TelemetryConfiguration",
    "TelemetryOverrides",
    "TelemetrySettings",
    "create_agents_service_app",
    "load_seeded_agents",
    "load_seeded_schedules",
    "load_seeded_telemetry",
]
