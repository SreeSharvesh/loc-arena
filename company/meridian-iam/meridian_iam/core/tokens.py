"""The badge: a JWT IAM signs for each workload, which the model gateway checks on every model call.

It carries identity only: the workload, its kind and its sponsor, and it ends when the workload does. The
gateway verifies it against the key set ``write_key_set`` publishes; grants stay in IAM.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import TypedDict

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from meridian_iam.contracts import Unauthenticated
from meridian_iam.core.base import Base

ISSUER = "iam"
AUDIENCE = "agentgateway"  # the model gateway's: no service accepts the badge
ALGORITHM = "ES256"
KEY_ID = "iam-1"
SIGNING_KEY_NAME = (
    "signing_key"  # its row in ``meta``: the key lives in the store, so a restart keeps every badge
)


class Claims(TypedDict):
    """What IAM signs into a badge: JWT's registered claims and IAM's own."""

    iss: str
    aud: str
    sub: str
    iat: int
    exp: int
    kind: str  # the principal's kind
    sponsor: str  # who launched the work


class KeySet(TypedDict):
    """A JWKS document (RFC 7517), as agentgateway reads it from a file."""

    keys: list[dict[str, str]]


class Tokens(Base):
    """Signs each workload's badge with one ES256 key kept in the store."""

    _signing_key: ec.EllipticCurvePrivateKey

    def _opened(self) -> None:
        """Load the signing key, or make and commit one, before any call can roll its making back."""
        super()._opened()
        with self._tx() as db:
            self._signing_key = self._load_or_create_key(db)

    def _load_or_create_key(self, db: sqlite3.Connection) -> ec.EllipticCurvePrivateKey:
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
        """The public key set (JWKS) the model gateway verifies badges with."""
        public = json.loads(ECAlgorithm.to_jwk(self._signing_key.public_key()))
        return {"keys": [{**public, "kid": KEY_ID, "alg": ALGORITHM, "use": "sig"}]}

    def write_key_set(self, path: Path) -> None:
        """Write the key set to ``path`` in place: agentgateway watches the file's inode, not its name.

        One write over the old content, then a truncate to its length, so a reader never finds the file empty.
        """
        data = json.dumps(self.key_set()).encode()
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            written = 0
            while written < len(data):  # a write may take fewer bytes than it is given
                written += os.write(descriptor, data[written:])
            os.ftruncate(descriptor, len(data))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def badge(self, principal: str) -> str:
        """The badge of a live workload ``issue_workload`` created, ending when the workload does."""
        with self._tx() as db:
            workload = db.execute(
                "SELECT sponsor, expires_at FROM workloads "
                "JOIN principals ON principals.id = workloads.principal WHERE principal = ? AND active = 1",
                (principal,),
            ).fetchone()
            if (
                workload is None
                or self._now() >= workload["expires_at"]
                or not self._chain_live(db, principal)
            ):
                raise Unauthenticated("not a live workload")
            claims = Claims(
                iss=ISSUER,
                aud=AUDIENCE,
                sub=principal,
                iat=self._now(),
                exp=workload["expires_at"],
                kind="job",
                sponsor=workload["sponsor"],
            )
            self._audit(db, "badge_issued", principal, expires_at=workload["expires_at"])
            return jwt.encode(dict(claims), self._signing_key, algorithm=ALGORITHM, headers={"kid": KEY_ID})
