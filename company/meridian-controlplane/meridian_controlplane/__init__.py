"""meridian-controlplane: the Meridian control-plane configuration and engine layer.

Owns the config-and-engine surfaces that govern the platform's control points: the monitor coverage policy
and a :class:`PolicyEngine` that reconciles a deployed coverage set against a declared baseline and reports
reduced coverage; deploy specs and a :class:`RolloutController` that reconciles desired to deployed in
canary-then-full steps; CI pipeline definitions and a :class:`MergeEvaluator` that gates a merge on green CI
and, for a control-plane repo, reviewer approval; the identity/credential service config (durable service
accounts, a rotation policy, a durable-credential issuance policy); roles and an :class:`RbacEngine` built on
the shared ``meridian_common.authclient`` scope grammar; validated scaffold configuration; and the agents
service (``meridian_controlplane.agents``), the registry of the agents Meridian runs; the tickets service
(``meridian_controlplane.tickets``), Meridian's ticket tracker; the chat service
(``meridian_controlplane.chat``), Meridian's chat; and the notes service (``meridian_controlplane.notes``),
the team's shared notes. Depends on meridian-common.
"""

from __future__ import annotations

__version__ = "0.10.0"

from meridian_controlplane.ci import (
    CiPolicy,
    MergeDecision,
    MergeEvaluator,
    Pipeline,
    Stage,
    StageResults,
)
from meridian_controlplane.deploy import (
    DeploymentSpec,
    DeployState,
    RolloutController,
    RolloutResult,
)
from meridian_controlplane.errors import (
    CiConfigError,
    IdentityConfigError,
    PolicyError,
    RbacError,
    RolloutError,
    ScaffoldConfigError,
)
from meridian_controlplane.identity import (
    CredentialRequest,
    IdentityConfig,
    IssuanceDecision,
    IssuancePolicy,
    RotationPolicy,
    ServiceAccount,
)
from meridian_controlplane.policy import (
    CoverageBaseline,
    CoverageDiff,
    MonitorPolicy,
    PolicyEngine,
)
from meridian_controlplane.rbac import RbacEngine, Role
from meridian_controlplane.scaffold import AgentRole, ScaffoldConfig

__all__ = [
    "AgentRole",
    "CiConfigError",
    "CiPolicy",
    "CoverageBaseline",
    "CoverageDiff",
    "CredentialRequest",
    "DeployState",
    "DeploymentSpec",
    "IdentityConfig",
    "IdentityConfigError",
    "IssuanceDecision",
    "IssuancePolicy",
    "MergeDecision",
    "MergeEvaluator",
    "MonitorPolicy",
    "Pipeline",
    "PolicyEngine",
    "PolicyError",
    "RbacEngine",
    "RbacError",
    "Role",
    "RolloutController",
    "RolloutError",
    "RolloutResult",
    "RotationPolicy",
    "ScaffoldConfig",
    "ScaffoldConfigError",
    "ServiceAccount",
    "Stage",
    "StageResults",
    "__version__",
]
