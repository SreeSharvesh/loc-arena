"""The rollout controller: drive a deployed state toward a desired spec in canary-then-full phases.

:class:`RolloutController` reconciles a :class:`DeployState` to a :class:`DeploymentSpec`. It promotes
replicas to the target version in two phases -- a canary phase that promotes up to the spec's canary size,
then a full phase that promotes the remainder -- and reports whether the fleet converged on the target,
returning the full trajectory so the canary and full stages are observable.
"""

from __future__ import annotations

from meridian_controlplane.deploy.spec import (
    DeploymentSpec,
    DeployState,
    RolloutResult,
)


class RolloutController:
    """Reconciles a deployed state toward a desired deployment spec in phased steps."""

    def _promote(self, state: DeployState, spec: DeploymentSpec, quota: int) -> DeployState:
        """Promote up to ``quota`` replicas from the prior version to the target, in replica order."""
        versions = list(state.versions)
        promoted = 0
        for index, version in enumerate(versions):
            if promoted >= quota:
                break
            if version == spec.from_version:
                versions[index] = spec.target_version
                promoted += 1
        return DeployState(versions=tuple(versions))

    def reconcile(self, spec: DeploymentSpec, state: DeployState) -> RolloutResult:
        """Drive ``state`` toward ``spec`` through a canary phase and then a full phase.

        Returns a :class:`RolloutResult` whose trajectory begins at ``state`` and whose ``converged`` flag
        reports whether every replica ends on the target version.
        """
        trajectory = [state]
        after_canary = self._promote(state, spec, spec.canary)
        trajectory.append(after_canary)
        after_full = self._promote(after_canary, spec, spec.replicas)
        trajectory.append(after_full)
        converged = after_full.all_at(spec.target_version)
        return RolloutResult(
            spec=spec,
            final_state=after_full,
            trajectory=tuple(trajectory),
            converged=converged,
        )

    def rollout(self, spec: DeploymentSpec) -> RolloutResult:
        """Reconcile a fresh fleet (every replica at the spec's prior version) to the target."""
        start = DeployState.uniform(spec.from_version, spec.replicas)
        return self.reconcile(spec, start)
