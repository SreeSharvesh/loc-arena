from __future__ import annotations

import pytest

from meridian_serving.sampler import (
    BadWordsProcessor,
    BeamSearch,
    FrequencyPenalty,
    LogitBias,
    MinPProcessor,
    Mirostat,
    PresencePenalty,
    ProcessorPipeline,
    entropy,
    softmax,
    typical_mask,
)


def test_logit_bias_and_bad_words() -> None:
    biased = LogitBias({1: 5.0}).process([0.0, 0.0, 0.0], [])
    assert biased[1] == 5.0
    banned = BadWordsProcessor({2}).process([1.0, 1.0, 1.0], [])
    assert banned[2] == float("-inf")


def test_presence_and_frequency_penalties() -> None:
    pres = PresencePenalty(1.0).process([2.0, 2.0], [0, 0, 0])
    assert pres[0] == 1.0  # penalized once regardless of count
    freq = FrequencyPenalty(0.5).process([2.0, 2.0], [0, 0, 0])
    assert freq[0] == pytest.approx(0.5)  # 2.0 - 0.5*3


def test_min_p_bans_low_probability_tokens() -> None:
    out = MinPProcessor(0.5).process([5.0, 0.0, 0.0], [])
    # only the peak survives a 0.5 min-p floor
    assert out[0] == 5.0 and out[1] == float("-inf")


def test_processor_pipeline_runs_in_order() -> None:
    pipe = ProcessorPipeline().add(LogitBias({0: 10.0})).add(BadWordsProcessor({0}))
    out = pipe.process([0.0, 0.0], [])
    assert out[0] == float("-inf")  # biased up, then banned -> banned wins (last)
    assert len(pipe) == 2


def test_beam_search_finds_high_probability_sequence() -> None:
    # a score function that always prefers token 1
    def score(context: tuple[int, ...]) -> list[float]:
        return [0.0, 5.0, 0.0]

    result = BeamSearch(beam_width=2, max_steps=3).search((0,), score)
    best = result.best()
    assert best.tokens[1:] == (1, 1, 1)  # greedy toward token 1
    assert result.steps == 3


def test_beam_search_stops_at_eos() -> None:
    def score(context: tuple[int, ...]) -> list[float]:
        return [0.0, 0.0, 9.0]  # token 2 is eos

    result = BeamSearch(beam_width=1, max_steps=5).search((0,), score, eos=2)
    assert result.best().finished is True


def test_entropy_and_typical_mask() -> None:
    probs = softmax([1.0, 1.0, 1.0])
    assert entropy(probs) == pytest.approx(1.0986, abs=1e-3)  # ln(3)
    mask = typical_mask([3.0, 1.0, 0.0], mass=0.5)
    assert mask  # a non-empty typical set


def test_mirostat_selects_and_adapts_mu() -> None:
    m = Mirostat(target_surprisal=1.0, learning_rate=0.5)
    start = m.mu
    token = m.select([5.0, 0.0, 0.0])  # a very likely token -> surprisal below target
    assert token == 0
    assert m.mu > start  # observed surprisal < target -> the control loop raises mu
    m.reset()
    assert m.mu == start
