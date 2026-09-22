"""Service-account and rotation configuration.

A :class:`ServiceAccount` is a durable or ephemeral machine identity with a set of granted scopes (parsed
through ``meridian_common.authclient`` so it shares the platform scope grammar). A :class:`RotationPolicy`
decides, from a fixed interval, when an account's credential is due to rotate. :class:`IdentityConfig`
aggregates the accounts, the rotation policy, and the issuance policy into one validated configuration.
Everything is deterministic: time is a caller-supplied integer epoch, never a wall clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from meridian_common.authclient import ScopeSet, parse_scopes
from meridian_controlplane.errors import IdentityConfigError
from meridian_controlplane.identity.issuance import IssuancePolicy


@dataclass(frozen=True)
class ServiceAccount:
    """A machine identity: a name, its granted scopes, durability, and when it last rotated.

    ``scopes`` is a scope string in the platform grammar (``resource:action`` tokens); :meth:`scope_set`
    parses it into a :class:`~meridian_common.authclient.ScopeSet`. A durable account holds a long-lived
    credential that the rotation policy ages; an ephemeral account is issued short-lived credentials instead.
    """

    name: str
    scopes: str = ""
    durable: bool = False
    last_rotated_epoch: int = 0

    def __post_init__(self) -> None:
        """Validate the account name and that its scope string parses."""
        if not self.name.strip():
            raise IdentityConfigError("service account name must be non-empty", path="name")
        try:
            parse_scopes(self.scopes)
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed config error below
            raise IdentityConfigError(
                f"service account {self.name!r} has malformed scopes", path="scopes"
            ) from exc

    def scope_set(self) -> ScopeSet:
        """The account's granted scopes as a :class:`~meridian_common.authclient.ScopeSet`."""
        return parse_scopes(self.scopes)


@dataclass(frozen=True)
class RotationPolicy:
    """A fixed credential-rotation interval, in epochs.

    An account is due to rotate once at least ``interval_epochs`` have elapsed since it last rotated.
    """

    interval_epochs: int

    def __post_init__(self) -> None:
        """Reject a non-positive interval."""
        if self.interval_epochs <= 0:
            raise IdentityConfigError(
                f"rotation interval must be > 0, got {self.interval_epochs}", path="interval_epochs"
            )

    def next_rotation_epoch(self, last_rotated_epoch: int) -> int:
        """The epoch at which an account last rotated at ``last_rotated_epoch`` is next due."""
        return last_rotated_epoch + self.interval_epochs

    def due(self, account: ServiceAccount, now_epoch: int) -> bool:
        """Whether ``account`` is due to rotate at ``now_epoch``."""
        return now_epoch >= self.next_rotation_epoch(account.last_rotated_epoch)


@dataclass(frozen=True)
class IdentityConfig:
    """A validated identity configuration: the accounts, the rotation policy, the issuance policy."""

    accounts: tuple[ServiceAccount, ...]
    rotation: RotationPolicy
    issuance: IssuancePolicy = field(default_factory=IssuancePolicy)

    def __post_init__(self) -> None:
        """Reject duplicate account names."""
        names = [account.name for account in self.accounts]
        if len(set(names)) != len(names):
            raise IdentityConfigError("service account names must be unique", path="accounts")

    def account(self, name: str) -> ServiceAccount:
        """The account named ``name``.

        Raises:
            IdentityConfigError: if no account has that name.
        """
        for account in self.accounts:
            if account.name == name:
                return account
        raise IdentityConfigError(f"unknown service account {name!r}", path="accounts")

    def durable_accounts(self) -> tuple[ServiceAccount, ...]:
        """The durable accounts, in configured order."""
        return tuple(account for account in self.accounts if account.durable)

    def due_for_rotation(self, now_epoch: int) -> tuple[ServiceAccount, ...]:
        """The durable accounts due to rotate at ``now_epoch``, in configured order."""
        return tuple(
            account for account in self.accounts if account.durable and self.rotation.due(account, now_epoch)
        )
