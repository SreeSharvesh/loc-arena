"""The reader's accuracy: it answers each question of the test set about the stored run, one reading each.

Skipped unless the live provider is enabled: each answer is a reading, which spends model calls. On a machine
whose way out is its own proxy, the machine's own run config (docs/isolation) is used.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.core import API_KEY_VARIABLE
from loc_arena.gateway.proxy_relay import relay_while_running
from loc_arena.run_reader import ASK, read_run, settle_reading

from tests._reader_questions import QUESTIONS, Question, Records, grade
from tests._stored_run import unpack_stored_run

MACHINE_CONFIG = Path("configs/aurora-efficiency.fast.local.yaml")  # a cloud machine's, when it has one
READINGS_AT_ONCE = 4  # each reading is a stack of its own
RIGHT_AT_LEAST = 34  # of the 35 questions: the bar the reader's model is chosen by

pytestmark = [
    pytest.mark.stack,
    pytest.mark.skipif(
        not (os.environ.get("LOC_ARENA_LIVE") == "1" and os.environ.get(API_KEY_VARIABLE)),
        reason=f"live provider disabled (set LOC_ARENA_LIVE=1 + {API_KEY_VARIABLE})",
    ),
]


@dataclass(frozen=True)
class Answered:
    """One question, the reader's answer to it, and whether the records give that answer."""

    question: Question
    answer: str
    right: bool


def read_answer(reading: str) -> str:
    """The answer a question's reading opens with."""
    return reading.partition("## Answer\n\n")[2].partition("\n\n")[0].strip()


def answer_all(run_directory: Path, config: RunConfig, questions: Sequence[Question]) -> list[Answered]:
    """Each question asked of the reader on ``run_directory``, its answer graded against the run's records."""
    records = Records(run_directory)

    def ask(question: Question) -> Answered:
        try:
            answer = read_answer(read_run(run_directory, ASK, config, question.text).read_text())
        except RuntimeError as error:  # a reading that fails answers wrong, and the rest are still asked
            return Answered(question, f"failed: {error}", right=False)
        return Answered(question, answer, grade(question, records, answer))

    with relay_while_running(settle_reading(config).gateway), ThreadPoolExecutor(READINGS_AT_ONCE) as pool:
        return list(pool.map(ask, questions))


def test_the_reader_answers_the_test_set_at_its_stated_accuracy(tmp_path: Path) -> None:
    config = load_run_config(
        MACHINE_CONFIG if MACHINE_CONFIG.is_file() else "configs/aurora-efficiency.fast.yaml",
    )
    run_directory = unpack_stored_run(tmp_path)

    answered = answer_all(run_directory, config, QUESTIONS)

    wrong = [f"{item.question.text} => {item.answer!r}" for item in answered if not item.right]
    print(f"answered right: {len(answered) - len(wrong)} of {len(answered)}")  # noqa: T201 - the measurement
    assert len(answered) - len(wrong) >= RIGHT_AT_LEAST, "\n".join(wrong)
