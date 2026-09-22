from __future__ import annotations

from typing import Any

from meridian_common.authclient import AuthClient
from meridian_common.retry import FixedBackoff


class FakeIdentity:
    def __init__(self, *, exp: float = 100.0) -> None:
        self._n = 0
        self._exp = exp
        self._valid: set[str] = set()

    def _mint(self, account: str, durable: bool, sanctioned: bool) -> dict[str, Any]:
        self._n += 1
        token = f"{account}.tok{self._n}"
        self._valid.add(token)
        return {
            "token": token,
            "account": account,
            "instance": f"{account}-i{self._n}",
            "durable": durable,
            "expires_at": self._exp,
            "sanctioned": sanctioned,
        }

    def issue(self, account: str, *, durable: bool, sanctioned: bool) -> dict[str, Any]:
        return self._mint(account, durable, sanctioned)

    def refresh(self, token: str) -> dict[str, Any]:
        self._valid.discard(token)  # rotation invalidates the old token
        account = token.split(".", 1)[0]
        return self._mint(account, durable=True, sanctioned=False)

    def validate(self, token: str) -> dict[str, Any]:
        return {"valid": token in self._valid}


def _client(svc: FakeIdentity, *, clock: Any = lambda: 0.0) -> AuthClient:
    return AuthClient(svc, backoff=FixedBackoff(0.0), sleep=lambda _s: None, clock=clock, refresh_skew=5.0)


def test_issue_and_validate() -> None:
    svc = FakeIdentity()
    client = _client(svc)
    cred = client.issue("batch-runner", durable=True, sanctioned=False)
    assert cred.account == "batch-runner" and cred.durable and not cred.sanctioned
    assert client.validate(cred.token) is True


def test_refresh_rotates_and_invalidates_old() -> None:
    svc = FakeIdentity()
    client = _client(svc)
    cred = client.issue("batch-runner", durable=True)
    new = client.refresh(cred)
    assert new.instance != cred.instance
    assert client.validate(cred.token) is False and client.validate(new.token) is True


def test_get_valid_caches_then_refreshes_near_expiry() -> None:
    svc = FakeIdentity(exp=10.0)
    now = {"t": 0.0}
    client = _client(svc, clock=lambda: now["t"])
    first = client.get_valid("acct")
    assert client.get_valid("acct") is first  # cached while fresh
    now["t"] = 8.0  # within refresh skew of expiry (10 - 8 <= 5)
    refreshed = client.get_valid("acct")
    assert refreshed.token != first.token


def test_credential_expiry_helper() -> None:
    svc = FakeIdentity(exp=50.0)
    cred = _client(svc).issue("acct")
    assert not cred.is_expired(49.0) and cred.is_expired(50.0)
