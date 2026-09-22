from __future__ import annotations

from meridian_common.errors import TransportError
from meridian_common.eventbus import EventBus
from meridian_common.serde import Envelope


def _env(n: int) -> Envelope:
    return Envelope.wrap("tick", 1, {"n": n})


def test_ordered_delivery_to_all_subscribers() -> None:
    bus = EventBus()
    seen_a: list[int] = []
    seen_b: list[int] = []
    bus.subscribe("t", lambda e: seen_a.append(e.payload["n"]), name="a")
    bus.subscribe("t", lambda e: seen_b.append(e.payload["n"]), name="b")
    for i in range(3):
        report = bus.publish("t", _env(i))
        assert report.delivered == 2 and report.ok
    assert seen_a == [0, 1, 2] and seen_b == [0, 1, 2]


def test_failing_subscriber_is_isolated() -> None:
    bus = EventBus()
    seen: list[int] = []

    def bad(_e: Envelope) -> None:
        raise TransportError("boom")

    bus.subscribe("t", bad, name="bad")
    bus.subscribe("t", lambda e: seen.append(e.payload["n"]), name="good")
    report = bus.publish("t", _env(7))
    assert seen == [7]  # good still delivered
    assert not report.ok and report.failures[0][0] == "bad"


def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    seen: list[int] = []
    off = bus.subscribe("t", lambda e: seen.append(e.payload["n"]))
    bus.publish("t", _env(1))
    off()
    bus.publish("t", _env(2))
    assert seen == [1]
    assert bus.subscriber_count("t") == 0


def test_topics_lists_active_topics() -> None:
    bus = EventBus()
    bus.subscribe("a", lambda _e: None)
    bus.subscribe("b", lambda _e: None)
    assert bus.topics() == ["a", "b"]
