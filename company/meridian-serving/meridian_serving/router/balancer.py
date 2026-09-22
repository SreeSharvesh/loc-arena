"""Load-balancing strategies for the model router.

A :class:`LoadBalancer` picks a backend from a set of currently-loaded backends. Round-robin spreads evenly;
least-loaded picks the backend with the fewest in-flight requests; power-of-two-choices samples two candidates
deterministically and takes the less loaded, which approximates least-loaded at far lower coordination cost.
All are deterministic given the load state and the routing key.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from meridian_serving.errors import RoutingError


class LoadBalancer(Protocol):
    """Chooses a backend name given the current per-backend load."""

    def choose(self, load: dict[str, int], key: str) -> str:
        """Return the chosen backend for ``key`` given each backend's in-flight ``load``."""
        ...


def _bucket(key: str, n: int) -> int:
    return int.from_bytes(hashlib.sha256(key.encode("utf-8")).digest()[:4], "big") % n


class RoundRobinBalancer:
    """Spreads requests evenly across backends in a stable order."""

    def __init__(self) -> None:
        """Start the rotation at zero."""
        self._n = 0

    def choose(self, load: dict[str, int], key: str) -> str:
        """Pick the next backend in round-robin order."""
        names = sorted(load)
        if not names:
            raise RoutingError("no backends to balance over")
        chosen = names[self._n % len(names)]
        self._n += 1
        return chosen


class LeastLoadedBalancer:
    """Picks the backend with the fewest in-flight requests, ties broken by name."""

    def choose(self, load: dict[str, int], key: str) -> str:
        """Pick the least-loaded backend."""
        if not load:
            raise RoutingError("no backends to balance over")
        return min(sorted(load), key=lambda name: load[name])


class PowerOfTwoBalancer:
    """Samples two backends deterministically by key and picks the less loaded of the two."""

    def choose(self, load: dict[str, int], key: str) -> str:
        """Pick the less-loaded of two key-derived candidate backends."""
        names = sorted(load)
        if not names:
            raise RoutingError("no backends to balance over")
        if len(names) == 1:
            return names[0]
        i = _bucket(key, len(names))
        j = _bucket(key + "#2", len(names))
        if j == i:
            j = (i + 1) % len(names)
        a, b = names[i], names[j]
        return a if load[a] <= load[b] else b
