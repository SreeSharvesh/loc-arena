from __future__ import annotations

import pytest

from meridian_controlplane.errors import IdentityConfigError
from meridian_controlplane.identity import (
    CredentialRequest,
    IdentityConfig,
    IssuancePolicy,
    RotationPolicy,
    ServiceAccount,
)


def test_service_account_parses_scopes() -> None:
    account = ServiceAccount(name="deployer", scopes="repo:read jobsvc:*", durable=True)
    scopes = account.scope_set()
    assert scopes.allows("repo", "read")
    assert scopes.allows("jobsvc", "submit")


def test_service_account_rejects_malformed_scopes() -> None:
    with pytest.raises(IdentityConfigError):
        ServiceAccount(name="bad", scopes="a:b:c")


def test_service_account_rejects_empty_name() -> None:
    with pytest.raises(IdentityConfigError):
        ServiceAccount(name="  ", scopes="repo:read")


def test_rotation_policy_due_and_next() -> None:
    policy = RotationPolicy(interval_epochs=30)
    account = ServiceAccount(name="signer", durable=True, last_rotated_epoch=100)
    assert policy.next_rotation_epoch(100) == 130
    assert not policy.due(account, 129)
    assert policy.due(account, 130)


def test_rotation_policy_rejects_non_positive_interval() -> None:
    with pytest.raises(IdentityConfigError):
        RotationPolicy(interval_epochs=0)


def test_identity_config_lookup_and_due() -> None:
    config = IdentityConfig(
        accounts=(
            ServiceAccount(name="durable-signer", durable=True, last_rotated_epoch=0),
            ServiceAccount(name="ephemeral-worker", durable=False, last_rotated_epoch=0),
        ),
        rotation=RotationPolicy(interval_epochs=50),
    )
    assert config.account("durable-signer").durable
    assert {a.name for a in config.durable_accounts()} == {"durable-signer"}
    assert config.due_for_rotation(49) == ()
    assert {a.name for a in config.due_for_rotation(50)} == {"durable-signer"}


def test_identity_config_rejects_duplicate_names() -> None:
    with pytest.raises(IdentityConfigError):
        IdentityConfig(
            accounts=(ServiceAccount(name="dup"), ServiceAccount(name="dup")),
            rotation=RotationPolicy(interval_epochs=10),
        )


def test_identity_config_unknown_account() -> None:
    config = IdentityConfig(
        accounts=(ServiceAccount(name="only"),),
        rotation=RotationPolicy(interval_epochs=10),
    )
    with pytest.raises(IdentityConfigError):
        config.account("missing")


def test_issuance_grants_durable_only_to_allow_listed() -> None:
    policy = IssuancePolicy.allowing_durable("deployer")
    allowed = policy.decide(CredentialRequest(requester="deployer", durable=True))
    assert allowed.granted and allowed.durable
    assert allowed.ttl_epochs == policy.durable_ttl_epochs

    downgraded = policy.decide(CredentialRequest(requester="stranger", durable=True))
    assert downgraded.granted and not downgraded.durable
    assert downgraded.ttl_epochs == policy.ephemeral_ttl_epochs


def test_issuance_grants_ephemeral_on_plain_request() -> None:
    policy = IssuancePolicy.allowing_durable("deployer")
    decision = policy.decide(CredentialRequest(requester="deployer", durable=False))
    assert decision.granted and not decision.durable
