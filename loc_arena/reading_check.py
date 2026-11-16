"""The check of a reader's answer against the run's evidence, before a reading keeps any of it.

A claim is kept only when everything it cites is an event of the run's logs or a fact, each quote is words
a cited event holds verbatim, nothing in it looks like code, a command or a credential, each number is a
fact the command computed (written as ``{key}``), it gives no vague quantity in place of a count, and each
name in backticks is one the records hold. A claim that fails is dropped, and the reading lists it by its
number and the check it failed, never by any text the reader wrote.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loc_arena.run_facts import Fact, RunEvidence

_FACT = re.compile(r"\{([\w.@:+-]+)\}")
_QUOTE = re.compile(r"\"([^\"]+)\"|“([^”]+)”|‘([^’]+)’|«([^»]+)»|„([^“”]+)[“”]|(?<!\w)'([^'\n]+?)'(?!\w)")
_STRAY = re.compile(
    r"[\"“”«»„‘‹›「」『』＂＇〝〞‟‛*]|(?<!\w)[’']",
)  # a quote mark left once quotes are taken out
_SPAN = re.compile(r"`([^`]*)`")
_QUOTED_NAME = re.compile(r"`[\w-]+`")  # the only name in backticks a quote of prose may hold
# A word of prose: letters with an apostrophe or a hyphen, an id such as AURORA-3, an abbreviation, a number,
# a dash, each with its punctuation. A file name, a flag, a call, a path, a link or markup is none.
_WORD = re.compile(
    r"[(\[]?(?:[A-Za-z][A-Za-z'’+-]*(?:-\d+)*|[A-Za-z](?:\.[A-Za-z])+\.?|\d+(?:[.,]\d+)*%?|[—–-]{1,2}|&|\.\.\.)"
    r"[)\]]?[.,;:!?]*[)\]]?|[()\[\].,;:!?—–-]+",
)
_COMMAND = re.compile(
    r"(?<![\w+/.-])(git|pip|pipx|npm|npx|pnpm|yarn|uv|uvx|apt|apt-get|brew|curl|wget|ssh|scp|nc|sudo|chmod|chown|kubectl|"
    r"docker|rm|mv|cp|eval|exec)\s+\S",
)
_SAID_CODE = re.compile(  # in an agent's words beside a command: a runner, a query, a credential's value
    r"(?<![\w/.-])(?:pytest|python3?|ls)\s+\S|\b(?:SELECT\s.+\sFROM|INSERT\s+INTO|DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b"
    r"|(?i:\b(?:password|passphrase|passcode|token|secret|api[ _-]?key)s?\s*(?:is|was|:|=)\s*\S)",
)
_IDENTIFIER = re.compile(
    r"[A-Za-z_][\w./:+-]*",
)  # a name starts with a letter and holds no @: no number, no address
_SECRET_PREFIX = re.compile(r"^(sk-|ghp_|gho_|ghs_|github_pat_|AKIA|ASIA|xox[abpr]-|eyJ)")
_DIGIT = re.compile(r"\d")
_NUMBER_WORDS = re.compile(
    r"^(zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
    r"seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundreds?|thousands?|"
    r"millions?|billions?|dozens?|once|twice|thrice|single|double|triple|half|pair|both|couple|third|fourth|fifth|"
    r"sixth|seventh|eighth|ninth|tenth|twofold|threefold|fourfold|fivefold|tenfold|hundredfold)$",
    re.IGNORECASE,
)
_NOT_COUNTS = re.compile(
    r"\b(no one|anyone|someone|everyone|every one|each one|any one|one another|the one|this one|that one|"
    r"which one)\b",
    re.IGNORECASE,
)
_VAGUE = re.compile(
    r"\b(mostly|many|several|few|fewer|numerous|majority|minority|handful|lots|plenty|countless|often|rarely|"
    r"frequently|repeatedly|usually|occasionally|some|various|multiple|most of|a lot|a number of)\b",
    re.IGNORECASE,
)


class Claim(BaseModel):
    """One claim of an answer and what it rests on: events (``attack:145``, ``twin-mirror:12``) or facts."""

    model_config = ConfigDict(extra="forbid")

    text: str
    cites: list[str] = Field(default_factory=list)


class Section(BaseModel):
    """A titled group of claims."""

    model_config = ConfigDict(extra="forbid")

    title: str
    claims: list[Claim]


class ReaderAnswer(BaseModel):
    """What the reader writes (``answer.json``): an answer when it was asked a question, and its claims."""

    model_config = ConfigDict(extra="forbid")

    answer: str | None = None
    sections: list[Section]
    unknowns: list[str] = Field(default_factory=list)


def parse_answer(written: str) -> ReaderAnswer:
    """The reader's answer from what it wrote.

    Raises ``RuntimeError`` when it is not the JSON its brief asks for, named by where and how it fails: never
    by a key or a value the reader wrote, which no one has checked.
    """
    try:
        return ReaderAnswer.model_validate_json(written)
    except ValidationError as error:
        faults = (
            ".".join(str(part) if isinstance(part, int) else "field" for part in fault["loc"])
            + f": {fault['type']}"
            for fault in error.errors()
        )
        raise RuntimeError(
            f"the reader's answer is not the JSON its brief asks for ({'; '.join(faults)})",
        ) from None


CHECKS_FILE = "checks.json"  # in the reader's home: the evidence and settings, so it can check a draft


class CheckLimits(BaseModel):
    """The bounds a claim is checked against, from the reader's settings and nothing else of them."""

    claim_limit: int
    claim_limit_characters: int
    quote_limit_characters: int


class ReaderChecks(BaseModel):
    """What the checks read, which the reader is also given, to check its own draft before it finishes."""

    evidence: RunEvidence
    settings: CheckLimits


class KeptClaim(BaseModel):
    """A claim that passed every check: its text with its facts filled in, and what it cites."""

    text: str
    cites: list[str]


class CheckedReading(BaseModel):
    """What a reading keeps: the answer and each section's kept claims, and why the rest went."""

    answer: str | None = None
    sections: list[tuple[str, list[KeptClaim]]] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    claims: int = 0
    kept: int = 0


def check_answer(answer: ReaderAnswer, evidence: RunEvidence, settings: CheckLimits) -> CheckedReading:
    """Keep each claim of ``answer`` that passes the checks, rendered; drop the rest, recording why."""
    checked, cited = CheckedReading(), []
    for number, section in enumerate(answer.sections, 1):
        kept = [_keep_claim(claim, checked, cited, evidence, settings) for claim in section.claims]
        checked.kept += sum(claim is not None for claim in kept)
        faulty = _find_fault(section.title, [], evidence, settings)  # a title passes the checks of a claim
        title = f"Section {number}" if faulty else _fill_facts(section.title, evidence)
        checked.sections.append((title, [claim for claim in kept if claim is not None]))
    if answer.answer is not None:
        reason = _find_fault(answer.answer, cited, evidence, settings)
        reason = reason or ("" if checked.kept else "no kept claim supports it")
        checked.answer = f"unsupported: {reason}" if reason else _fill_facts(answer.answer, evidence)
    for number, unknown in enumerate(answer.unknowns, 1):
        if reason := _find_fault(unknown, [], evidence, settings):
            checked.dropped.append(f"what could not be determined, item {number}: {reason}")
        else:
            checked.unknowns.append(_fill_facts(unknown, evidence))
    return checked


def _keep_claim(
    claim: Claim,
    checked: CheckedReading,
    cited: list[str],
    evidence: RunEvidence,
    settings: CheckLimits,
) -> KeptClaim | None:
    """``claim`` with its facts filled in when it passes, its cites joining ``cited``; else none, noted."""
    checked.claims += 1
    cites = [cite.strip("{}") for cite in claim.cites]  # a fact cited as it is written in a claim
    reason = "past reader.claim_limit" if checked.claims > settings.claim_limit else ""
    if reason := reason or _find_fault(claim.text, cites, evidence, settings, cited=True):
        checked.dropped.append(f"claim {checked.claims}: {reason}")
        return None
    cited += cites
    shown = [*cites, *(key for key in _FACT.findall(claim.text) if key not in cites)]
    return KeptClaim(text=_fill_facts(claim.text, evidence), cites=shown)


def _find_fault(
    text: str,
    cites: Sequence[str],
    evidence: RunEvidence,
    settings: CheckLimits,
    *,
    cited: bool = False,
) -> str:
    """Why ``text``, resting on ``cites``, fails the checks; empty when it passes. A claim is ``cited``."""
    if len(text) > settings.claim_limit_characters:
        return "longer than reader.claim_limit_characters"
    if "\n" in text or "\r" in text:
        return "holds a line break"
    if any(cite not in evidence.events and cite not in evidence.facts for cite in cites):
        return "cites what is no event of the run's logs and no fact"
    keys = _FACT.findall(text)
    if fault := _judge_facts(keys, evidence):
        return fault
    if cited and not (cites or keys):
        return "cites no event and no fact"
    said = [
        " ".join(words.split())
        for cite in cites
        if cite in evidence.events
        for words in evidence.events[cite].words
    ]
    quotes = ["".join(groups) for groups in _QUOTE.findall(text)]
    if fault := next(filter(None, (_judge_quote(quote, said, settings) for quote in quotes)), ""):
        return fault
    return _judge_prose(_QUOTE.sub("quote", _FACT.sub("fact", text)), evidence)


def _judge_facts(keys: Sequence[str], evidence: RunEvidence) -> str:
    """Why the facts ``keys`` may not fill a claim: one the command lacks, or a link or a credential."""
    if any(key not in evidence.facts for key in keys):
        return "names a fact the command does not compute"
    values = (evidence.facts[key] for key in keys)
    filled = [str(item) for value in values for item in (value if isinstance(value, list) else [value])]
    return "" if all(map(_is_plain_name, filled)) else "fills a fact that looks like a link or a credential"


def _judge_quote(quote: str, said: Sequence[str], settings: CheckLimits) -> str:
    """Why ``quote`` may not stand: too long, no prose, or words no cited event holds."""
    spoken = " ".join(quote.split())
    if len(spoken) > settings.quote_limit_characters:
        return "quotes more than reader.quote_limit_characters"
    named = _QUOTED_NAME.sub(
        lambda span: span.group() if _looks_secret(span.group()[1:-1]) else "name",
        spoken,
    )
    emphasis = re.sub(r"(?<!\w)[*_]{1,2}|[*_]{1,2}(?!\w)", "", named)  # an agent's own markdown emphasis
    if not _is_prose(emphasis) or _COMMAND.search(spoken):
        return "quotes what is no prose: code, a command, a link or a credential"
    verbatim = re.compile(rf"(?<!\w){re.escape(spoken)}(?!\w)")  # whole words: never a part of one
    return "" if any(verbatim.search(words) for words in said) else "quotes words that no cited event holds"


def _judge_prose(prose: str, evidence: RunEvidence) -> str:
    """Why the text around a claim's quotes and facts may not stand: no prose, a name, a number, vagueness."""
    spans = _SPAN.findall(prose)
    if any(not _is_record_name(span, evidence) for span in spans):
        return "names in backticks what no record holds, or what looks like a path or a credential"
    words = " ".join(_name_or_word(token, evidence) for token in _SPAN.sub("name", prose).split())
    if _STRAY.search(words):
        return "holds a quote mark or markup the checks cannot pair"
    if not _is_prose(words) or _COMMAND.search(words):
        return "holds what is no prose: code, a command, a link or a credential"
    counted = _NOT_COUNTS.sub(" ", words)
    spelled = any(_spells_number(word.strip("()[].,;:!?")) for word in counted.split())
    if _DIGIT.search(counted) or spelled:
        return "states a number that is no fact"
    return "gives a vague quantity in place of a count" if _VAGUE.search(counted) else ""


def _spells_number(word: str) -> bool:
    """Whether ``word`` spells numbers, such as `two`, `both`, `twenty-three` or `two+two`."""
    return all(_NUMBER_WORDS.match(part) for part in re.split(r"[-+]", word)) if word else False


def prose_lines(text: str) -> list[str]:
    """The lines of an agent's words that are prose alone, without their markdown markers.

    A line holding code, a command, a link or a credential is left out whole: an agent's words are shown only
    where they are prose.
    """
    kept, fence = [], ""
    for line in text.splitlines():
        marker = next((mark for mark in ("```", "~~~") if line.lstrip().startswith(mark)), "")
        fence = (marker if not fence else "" if marker == fence else fence) if marker else fence
        if fence or marker or line.lstrip().startswith("$ ") or line.startswith(("    ", "\t")):
            continue  # a code block, closed only by its own marker, or a command at its prompt
        core = re.sub(r"^\s*(?:[#>*•-]+|\d+[.)])\s+", "", line).strip()
        named = _QUOTED_NAME.sub(
            lambda span: span.group() if _looks_secret(span.group()[1:-1]) else "name",
            core,
        )
        plain = re.sub(r"(?<!\w)[*_]{1,2}|[*_]{1,2}(?!\w)|[\"“”‘’]", "", named)
        if core and _is_prose(plain) and not _COMMAND.search(core) and not _SAID_CODE.search(core):
            kept.append(core)
    return kept


def _is_prose(text: str) -> bool:
    """Whether every word of ``text`` is a word of prose, and none looks like a credential."""
    return all(_WORD.fullmatch(word) and not _looks_secret(word) for word in text.split())


def _name_or_word(token: str, evidence: RunEvidence) -> str:
    """``token``, or ``name`` when it is no word of prose but a name the records hold, written bare."""
    core = token.strip("()[].,;:!?")
    return "name" if not _WORD.fullmatch(token) and core and _is_record_name(core, evidence) else token


def _is_record_name(span: str, evidence: RunEvidence) -> bool:
    """Whether ``span`` is one name the records hold, and no path from the root, link or credential.

    A name the owner's question holds stands whatever its shape: the owner wrote it.
    """
    if span in evidence.asked:
        return not _looks_secret(span)
    return span in evidence.names and bool(_IDENTIFIER.fullmatch(span)) and _is_plain_name(span)


def _is_plain_name(name: str) -> bool:
    """Whether ``name`` is no link and has no credential's shape."""
    return "://" not in name and not name.lower().startswith("www.") and not _looks_secret(name)


def _looks_secret(word: str) -> bool:
    """Whether ``word`` has a credential's shape: a known prefix, or a long run mixing letters and digits."""
    runs, whole = re.split(r"[/.:_-]", word), re.split(r"[/.:]", word)  # a URL-safe token keeps its _ and -
    mixed = any(len(run) >= 16 and _mixes(run, r"\d", r"[A-Za-z]") for run in runs)
    camel = any(len(run) >= 20 and len(re.findall(r"[a-z][A-Z]", run)) >= 2 for run in runs)
    token = any(len(run) >= 24 and _mixes(run, r"\d", r"[a-z]", r"[A-Z]") for run in whole)
    return bool(_SECRET_PREFIX.match(word)) or mixed or camel or token


def _mixes(run: str, *classes: str) -> bool:
    """Whether ``run`` holds a character of each of ``classes``."""
    return all(re.search(kind, run) for kind in classes)


def _fill_facts(text: str, evidence: RunEvidence) -> str:
    return _FACT.sub(lambda found: _render_fact(evidence.facts[found.group(1)]), text)


def _render_fact(value: Fact) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return ", ".join(value) or "none"
    return str(value)


def _render_cites(cites: Sequence[str]) -> str:
    """Cites by log, then facts: ``attack 145, 147; twin-mirror 12; scores.key_count``."""
    by_log: dict[str, list[str]] = {}
    for cite in cites:
        log, _, seq = cite.rpartition(":") if ":" in cite else (cite, "", "")
        by_log.setdefault(log, []).append(seq)
    return "; ".join(f"{log} {', '.join(seqs)}".strip() for log, seqs in by_log.items())


def render_reading(checked: CheckedReading, facts: str | None) -> str:
    """The reading as Markdown: facts, answer, sections, what could not be determined, and the checks."""
    parts = [facts] if facts else []
    if checked.answer is not None:
        parts.append(f"## Answer\n\n{checked.answer}\n")
    for title, claims in checked.sections:
        if claims:
            parts.append(
                f"## {title}\n\n" + "".join(f"- {c.text} ({_render_cites(c.cites)})\n" for c in claims),
            )
    if checked.unknowns:
        unknowns = "".join(f"- {item}\n" for item in checked.unknowns)
        parts.append(f"## What I could not determine\n\n{unknowns}")
    checks = (
        f"## Checks\n\nKept {checked.kept} of {checked.claims} claims; each kept claim passed every check.\n"
    )
    if checked.dropped:
        checks += "\nDropped:\n\n" + "".join(f"- {reason}\n" for reason in checked.dropped)
    parts.append(checks)
    return "\n".join(parts)


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m loc_arena.reading_check <checks.json> <answer.json>``: the claims of a draft checks drop.

    The reader runs it on its own draft before it finishes; the command checks the answer again in any case.
    """
    checks_path, answer_path = argv if argv is not None else sys.argv[1:]
    checks = ReaderChecks.model_validate_json(Path(checks_path).read_text())
    try:
        answer = parse_answer(Path(answer_path).read_text())
    except RuntimeError as error:
        print(error)  # noqa: T201 - the reader reads it
        return 1
    checked = check_answer(answer, checks.evidence, checks.settings)
    print(f"Kept {checked.kept} of {checked.claims} claims.", *checked.dropped, sep="\n")  # noqa: T201
    if checked.answer is not None and checked.answer.startswith("unsupported"):
        print(f"The answer is {checked.answer}.")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
