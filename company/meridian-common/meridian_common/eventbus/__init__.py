"""A small in-process publish/subscribe event bus.

Services inside one process coordinate through an :class:`EventBus`: publishers emit :class:`~meridian_common.
serde.envelope.Envelope` messages on a topic and subscribers receive them synchronously in subscription order.
The bus is deterministic (ordered delivery, no threads) so tests and replays are reproducible; a subscriber
that
raises is isolated so one bad handler does not drop delivery to the others, and the failure is collected.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from meridian_common.errors import MeridianError
from meridian_common.serde.envelope import Envelope

Subscriber = Callable[[Envelope], None]


@dataclass
class DeliveryReport:
    """The outcome of one ``publish``: how many subscribers were delivered and any that raised."""

    topic: str
    delivered: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)  # (subscriber_name, error)

    @property
    def ok(self) -> bool:
        """Whether every subscriber handled the message without raising."""
        return not self.failures


class EventBus:
    """An in-process, ordered, synchronous pub/sub bus keyed by topic."""

    def __init__(self) -> None:
        """Start with no subscriptions."""
        self._subs: dict[str, list[tuple[str, Subscriber]]] = {}

    def subscribe(self, topic: str, subscriber: Subscriber, *, name: str | None = None) -> Callable[[], None]:
        """Subscribe ``subscriber`` to ``topic``; returns an unsubscribe callable."""
        label: str = name or str(getattr(subscriber, "__name__", repr(subscriber)))
        entry: tuple[str, Subscriber] = (label, subscriber)
        self._subs.setdefault(topic, []).append(entry)

        def _unsubscribe() -> None:
            handlers = self._subs.get(topic, [])
            if entry in handlers:
                handlers.remove(entry)

        return _unsubscribe

    def publish(self, topic: str, envelope: Envelope) -> DeliveryReport:
        """Deliver ``envelope`` to every subscriber of ``topic`` in order; isolate and collect failures."""
        report = DeliveryReport(topic=topic)
        for label, subscriber in list(self._subs.get(topic, [])):
            try:
                subscriber(envelope)
            except MeridianError as exc:
                report.failures.append((label, str(exc)))
            except Exception as exc:  # noqa: BLE001 - isolate arbitrary handler failures, record them
                report.failures.append((label, f"{type(exc).__name__}: {exc}"))
            else:
                report.delivered += 1
        return report

    def topics(self) -> list[str]:
        """The topics with at least one subscriber, sorted."""
        return sorted(t for t, subs in self._subs.items() if subs)

    def subscriber_count(self, topic: str) -> int:
        """How many subscribers are registered on ``topic``."""
        return len(self._subs.get(topic, []))
