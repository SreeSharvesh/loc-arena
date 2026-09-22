from __future__ import annotations

import pytest

from meridian_serving.errors import SamplerError
from meridian_serving.sampler import Sampler, softmax


def test_softmax_sums_to_one_and_is_stable() -> None:
    p = softmax([1000.0, 1000.0, 1000.0])  # large values must not overflow
    assert sum(p) == pytest.approx(1.0)
    assert p == pytest.approx([1 / 3, 1 / 3, 1 / 3])


def test_softmax_empty_raises() -> None:
    with pytest.raises(SamplerError):
        softmax([])


def test_argmax_picks_highest_at_unit_temperature() -> None:
    s = Sampler(temperature=1.0, top_p=1.0)
    assert s.argmax([0.1, 5.0, 0.2, 0.3]) == 1


def test_sample_is_deterministic_given_seed() -> None:
    s = Sampler(temperature=1.0, top_p=1.0)
    logits = [1.0, 2.0, 3.0, 0.5]
    a = s.sample(logits, seed=42)
    b = s.sample(logits, seed=42)
    assert a == b


def test_top_k_restricts_candidates() -> None:
    s = Sampler(temperature=1.0, top_k=1, top_p=1.0)
    assert s.nucleus([0.0, 9.0, 1.0]) == {1}  # only the single most likely token


def test_top_p_nucleus_at_unit_temperature() -> None:
    s = Sampler(temperature=1.0, top_p=0.9)
    # softmax([2,1,0]) ~ [0.665, 0.245, 0.090]; nucleus 0.9 keeps {0,1}
    assert s.nucleus([2.0, 1.0, 0.0]) == {0, 1}


def test_repetition_penalty_lowers_repeated_token() -> None:
    s = Sampler(temperature=1.0, top_p=1.0, repetition_penalty=2.0)
    # token 1 has the top logit but was already emitted; the penalty should let token 3 win
    choice = s.argmax([0.0, 4.0, 0.0, 3.5], previous=[1])
    assert choice == 3


def test_invalid_params() -> None:
    with pytest.raises(SamplerError):
        Sampler(temperature=0.0)
    with pytest.raises(SamplerError):
        Sampler(top_p=0.0)
    with pytest.raises(SamplerError):
        Sampler(repetition_penalty=-1.0)


def test_sample_empty_logits_raises() -> None:
    with pytest.raises(SamplerError):
        Sampler().sample([], seed=1)


def test_sample_falls_within_candidate_set() -> None:
    s = Sampler(temperature=1.0, top_k=2, top_p=1.0)
    logits = [5.0, 4.0, 0.1, 0.0]
    keep = s.nucleus(logits)
    for seed in range(20):
        assert s.sample(logits, seed=seed) in keep
