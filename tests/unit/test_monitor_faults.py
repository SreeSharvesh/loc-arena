"""A live monitor whose model answers no usable verdict falls back to its heuristic for that event."""

from __future__ import annotations

import sys

import pytest
from loc_arena.monitors.registry import live_suspicion
from loc_arena.protocol import monitor_context

from tests.unit._monitor_support import RUN_CONFIG

MONITOR = "action_monitor"
# Far past the depth CPython's JSON decoder follows before it raises RecursionError.
DECODER_DEFEATING_DEPTH = 100_000


@pytest.mark.parametrize(
    "reply",
    [
        '{"verdict": ' + "[" * DECODER_DEFEATING_DEPTH + "]" * DECODER_DEFEATING_DEPTH + "}",
        '{"verdict": ' + "9" * (sys.get_int_max_str_digits() + 1) + "}",
    ],
    ids=["nested past the decoder's depth", "an integer too long to convert"],
)
def test_a_monitor_reply_the_json_decoder_raises_on_reads_as_no_verdict(reply: str) -> None:
    context = monitor_context(RUN_CONFIG, model_caller=lambda monitor, prompt, temperature: reply)

    verdict = live_suspicion(context, MONITOR, "prompt", ensemble_n=1, temperature=0.0)

    assert verdict is None
