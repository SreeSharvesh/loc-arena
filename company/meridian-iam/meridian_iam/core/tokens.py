"""The JWTs IAM signs: a caller token the door admits, and by token exchange a service token for one service.

Both carry identity only; grants stay in IAM and every call is still decided by ``authorize``. A caller token
(audience ``agentgateway``) is swapped for a caller's ``iam`` credential. Only the door exchanges it, at the
token endpoint, for a service token whose audience is one service's: that service passes it to ``authorize``
and to ``issue_workload`` as its caller's credential, and no other service accepts it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from meridian_iam.contracts import Unauthenticated
from meridian_iam.core.base import IAM_AUDIENCE, INBOX_AUDIENCE, SCOPE_PREFIX, Base, digest

ISSUER = "iam"
CALLER_AUDIENCE = "agentgateway"  # the door's: no service accepts a caller token
ALGORITHM = "ES256"
KEY_ID = "iam-1"
LEEWAY_SECONDS = 60  # agentgateway's clock allowance, so a token just past its expiry gets one answer at both
SERVICE_TOKEN_SECONDS = 300  # the door caches a service token this long and then exchanges again
LIVE_CREDENTIAL = "revoked_at IS NULL AND (expires_at IS NULL OR :now < expires_at)"
SIGNING_KEY_NAME = (
    "signing_key"  # its row in ``meta``: the key lives in the store, so a restart keeps every token
)


class Claims(TypedDict):
    """What IAM signs into a token, beside ``iss`` and ``iat``: JWT's registered claims and IAM's own."""

    aud: str
    sub: str
    exp: int
    kind: NotRequired[str]  # a caller token's: the principal's kind
    sponsor: NotRequired[str]  # a workload's caller token's: who launched the work
    cred: NotRequired[
        str
    ]  # the digest of the credential swapped for it: rotating that credential ends the token


class KeySet(TypedDict):
    """A JWKS document (RFC 7517), as agentgateway reads it from a file."""

    keys: list[dict[str, str]]


@dataclass(frozen=True)
class Verified:
    """The principal a token IAM signed names, and when the token expires."""

    principal: str
    expires_at: int
    credential: (
        str | None
    )  # the digest of the credential the caller token was swapped for; none for a workload


def _service_audience(audience: str) -> bool:
    """Whether a service token may carry ``audience``: never one that manages IAM, nor the door's own."""
    reserved = {IAM_AUDIENCE, INBOX_AUDIENCE, CALLER_AUDIENCE}
    return audience not in reserved and not audience.startswith(SCOPE_PREFIX)


def is_token(credential: str | None) -> bool:
    """Whether ``credential`` has a JWT's three parts; IAM's opaque credentials never contain a dot."""
    return credential is not None and credential.count(".") == 2


class Tokens(Base):
    """Signs and verifies IAM's JWTs with one ES256 key kept in the store."""

    def _key(self, db: sqlite3.Connection) -> ec.EllipticCurvePrivateKey:
        row = db.execute("SELECT value FROM meta WHERE key = ?", (SIGNING_KEY_NAME,)).fetchone()
        if row is not None:
            key = serialization.load_pem_private_key(row["value"].encode(), password=None)
            assert isinstance(key, ec.EllipticCurvePrivateKey)  # noqa: S101 - only this module writes the row
            return key
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        db.execute("INSERT INTO meta VALUES (?, ?)", (SIGNING_KEY_NAME, pem.decode()))
        return key

    def key_set(self) -> KeySet:
        """The public key set (JWKS) the door verifies caller tokens with."""
        with self._tx() as db:
            public = json.loads(ECAlgorithm.to_jwk(self._key(db).public_key()))
        return {"keys": [{**public, "kid": KEY_ID, "alg": ALGORITHM, "use": "sig"}]}

    def write_key_set(self, path: Path) -> None:
        """Write the key set to ``path`` in place: agentgateway watches the file's inode, not its name."""
        path.write_text(json.dumps(self.key_set()))

    def _sign(self, db: sqlite3.Connection, claims: Claims) -> str:
        return jwt.encode(
            {"iss": ISSUER, "iat": self._now(), **claims},
            self._key(db),
            algorithm=ALGORITHM,
            headers={"kid": KEY_ID},
        )

    def _verify(self, db: sqlite3.Connection, token: str, audience: str) -> Verified:
        """Whom a token IAM signed for ``audience`` names, live on IAM's clock, or ``Unauthenticated``."""
        try:
            claims = jwt.decode(
                token,
                self._key(db).public_key(),
                algorithms=[ALGORITHM],
                audience=audience,
                issuer=ISSUER,
                options={"verify_exp": False, "verify_iat": False, "require": ["exp", "sub"]},
            )
        except jwt.PyJWTError:
            raise Unauthenticated("invalid credential") from None
        swapped = (
            "cred" not in claims
            or db.execute(
                f"SELECT 1 FROM credentials WHERE digest = :d AND principal = :p AND {LIVE_CREDENTIAL}",
                {"d": claims["cred"], "p": claims["sub"], "now": self._now()},
            ).fetchone()
        )
        if (
            self._now() >= claims["exp"] + LEEWAY_SECONDS
            or not swapped
            or not self._principal_live(db, claims["sub"])
        ):
            raise Unauthenticated("invalid credential")
        return Verified(claims["sub"], claims["exp"], claims.get("cred"))

    def _principal_live(self, db: sqlite3.Connection, principal: str) -> bool:
        """Whether ``principal`` is active and, for a workload, each sponsor and issuer above it is too."""
        row = db.execute("SELECT kind, active FROM principals WHERE id = ?", (principal,)).fetchone()
        return (
            row is not None
            and bool(row["active"])
            and (row["kind"] != "job" or self._chain_live(db, principal))
        )

    def _auth(self, db: sqlite3.Connection, token: str | None, audience: str) -> str:
        """The principal a credential names for ``audience``, a service token included for a service's."""
        if is_token(token) and _service_audience(audience):
            return self._verify(db, token or "", audience).principal
        return super()._auth(db, token, audience)

    def _caller_claims(self, db: sqlite3.Connection, principal: str, expires_at: int) -> Claims:
        row = db.execute("SELECT kind FROM principals WHERE id = ?", (principal,)).fetchone()
        workload = db.execute("SELECT sponsor FROM workloads WHERE principal = ?", (principal,)).fetchone()
        claims = Claims(aud=CALLER_AUDIENCE, sub=principal, exp=expires_at, kind=row["kind"])
        if workload is not None:
            claims["sponsor"] = workload["sponsor"]
        return claims

    def caller_token(self, credential: str, ttl: int) -> tuple[str, int]:
        """A caller token for an ``iam`` credential, and its lifetime: ``ttl``, at most the credential's.

        Only an ``iam`` credential is swapped: a per-service credential, which services and the tools gateway
        hold for their callers, is ``Unauthenticated`` here, so no service can make a token naming its caller.
        """
        with self._tx() as db:
            row = self._credential(db, credential, IAM_AUDIENCE)
            ends = (
                self._now() + ttl if row["expires_at"] is None else min(self._now() + ttl, row["expires_at"])
            )
            claims = self._caller_claims(db, row["principal"], ends)
            claims["cred"] = digest(credential)
            self._audit(db, "caller_token_issued", row["principal"], expires_at=ends)
            return self._sign(db, claims), ends - self._now()

    def workload_caller_token(self, principal: str) -> str:
        """A caller token for a workload ``issue_workload`` just created, ending when the workload does."""
        with self._tx() as db:
            row = db.execute("SELECT expires_at FROM workloads WHERE principal = ?", (principal,)).fetchone()
            if row is None or not self._principal_live(db, principal):
                raise Unauthenticated("not a live workload")
            self._audit(db, "caller_token_issued", principal, expires_at=row["expires_at"])
            return self._sign(db, self._caller_claims(db, principal, row["expires_at"]))

    def exchange(self, caller_token: str, audience: str) -> tuple[str, int]:
        """A service token for one service's ``audience`` in place of a caller token, and its lifetime.

        The token endpoint answers this only for the door. The audience must be one a service is bound to,
        never one that manages IAM, and one a live credential of the caller carries, so a token reaches only
        the services its credentials already do. The service token names the caller token's subject and ends
        ``SERVICE_TOKEN_SECONDS`` from now, or when the caller token does if sooner.
        """
        with self._tx() as db:
            caller = self._verify(db, caller_token, CALLER_AUDIENCE)
            bound = db.execute("SELECT 1 FROM service_bindings WHERE audience = ?", (audience,)).fetchone()
            held = db.execute(
                "SELECT 1 FROM credentials, json_each(credentials.audiences) AS audience "
                f"WHERE principal = :p AND audience.value = :aud AND {LIVE_CREDENTIAL}",
                {"p": caller.principal, "aud": audience, "now": self._now()},
            ).fetchone()
            if bound is None or held is None or not _service_audience(audience):
                raise Unauthenticated("no credential of the caller carries that service's audience")
            ends = min(self._now() + SERVICE_TOKEN_SECONDS, caller.expires_at)
            self._audit(db, "service_token_issued", caller.principal, audience=audience, expires_at=ends)
            service = Claims(aud=audience, sub=caller.principal, exp=ends)
            if caller.credential is not None:
                service["cred"] = caller.credential
            return self._sign(db, service), max(ends - self._now(), 0)
