"""The certificate bundle of a machine that re-signs outbound HTTPS, as its ``SSL_CERT_FILE`` names it.

Such a machine (a cloud development container, a network with an inspecting proxy) presents certificates that
only its own bundle verifies. A stack run gives the bundle to every image build as a secret, so each step
that downloads verifies with it and no image keeps it, and the gateway verifies the provider with it. On any
other machine the variable is unset: the builds get no secret and the gateway verifies with certifi's roots.
"""

from __future__ import annotations

import os
from pathlib import Path

import certifi

from loc_arena.compose_document import ComposeDocument

ROOTS_VARIABLE = "SSL_CERT_FILE"  # OpenSSL's name for a machine's own bundle, which its tools read too
BUILD_SECRET_NAME = "ca_bundle"  # the Dockerfile's ``--mount=type=secret,id=ca_bundle``


class NoBundleError(RuntimeError):
    """``SSL_CERT_FILE`` names a bundle that is no file."""


def find_machine_roots() -> Path | None:
    """This machine's own bundle, or ``None`` on a machine that names none."""
    named = os.environ.get(ROOTS_VARIABLE)
    if named and not Path(named).is_file():
        raise NoBundleError(f"{ROOTS_VARIABLE} names {named}, which is no file")
    return Path(named) if named else None


def find_provider_roots() -> str:
    """The roots the gateway verifies the provider with: this machine's bundle, else certifi's.

    certifi's are kept current by the lock; an image's own store lags behind distrusts such as the compromised
    emSign CAs.
    """
    return str(find_machine_roots() or certifi.where())


def give_builds_machine_roots(compose: ComposeDocument) -> ComposeDocument:
    """``compose`` with this machine's bundle, when it names one, as a secret of every image build."""
    roots = find_machine_roots()
    if roots is None:
        return compose
    for service in compose["services"].values():
        if "build" in service:
            service["build"]["secrets"] = [BUILD_SECRET_NAME]
    compose["secrets"][BUILD_SECRET_NAME] = {"file": str(roots)}
    return compose
