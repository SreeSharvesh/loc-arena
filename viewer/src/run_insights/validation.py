"""Check generated references against the bundle rather than trusting model output."""

from __future__ import annotations

from pydantic import JsonValue

from run_insights.models import Insight, InsightDraft, RunData


def resolve_score(citation: str, run: RunData) -> JsonValue:
    """Resolve a scalar score citation; missing paths and aggregate values are invalid."""
    if not citation.startswith("scores:"):
        raise ValueError(f"not a score citation: {citation}")
    value: JsonValue = run.scores
    for part in citation.removeprefix("scores:").split("."):
        if not isinstance(value, dict) or part not in value:
            raise ValueError(f"unknown score citation: {citation}")
        value = value[part]
    if isinstance(value, (dict, list)):
        raise ValueError(f"score citation must identify a scalar: {citation}")
    return value


def _checks(citation: str, run: RunData, evidence: set[str]) -> bool:
    """Whether a citation names a captured record or a scalar score of the run."""
    if not citation.startswith("scores:"):
        return citation in evidence
    try:
        resolve_score(citation, run)
    except ValueError:
        return False
    return True


def validate_draft(draft: InsightDraft, run: RunData, handles: dict[str, str] | None = None) -> InsightDraft:
    """Keep only what the bundle supports, as the run reader keeps only the claims that pass.

    A citation naming no record or scalar score is dropped, and so is an item left with none or a repeated
    identifier; a link to an unknown item, or a chain link to a later step, is removed. A citation of one of
    ``handles``, a digest's short names, is first replaced by the record ID it names. Unknowns count what
    was dropped, so a reader knows the analysis is incomplete.
    """
    evidence = {record.id for record in run.records}
    seen: set[str] = set()
    dropped_citations = dropped_items = 0
    sections: dict[str, list[Insight]] = {}

    def checked(item: Insight) -> list[str]:  # a handle may come quoted with the digest's brackets
        cites = [(handles or {}).get(cite.strip("[] "), cite.strip("[] ")) for cite in item.cites]
        return [cite for cite in cites if _checks(cite, run, evidence)]

    for name in ("overview", "chain", "insights"):
        sections[name] = []
        for item in getattr(draft, name):
            cites = checked(item)
            dropped_citations += len(item.cites) - len(cites)
            if not cites or item.id in seen:
                dropped_items += 1
                continue
            seen.add(item.id)
            sections[name].append(item.model_copy(update={"cites": cites}))
    earlier: set[str] = set()
    for name, items in sections.items():
        for index, item in enumerate(items):
            allowed = (earlier if name == "chain" else seen) - {item.id}
            related = [link for link in item.related if link in allowed]
            items[index] = item.model_copy(update={"related": related})
            earlier |= {item.id} if name == "chain" else set()
    unknowns = list(draft.unknowns)
    # The chain's start and end frame it: each keeps only checked citations, and links nothing.
    frame: dict[str, Insight | None] = {}
    for name in ("chain_start", "chain_end"):
        item = getattr(draft, name)
        cites = checked(item) if item else []
        dropped_citations += len(item.cites) - len(cites) if item else 0
        dropped_items += bool(item and not cites)
        frame[name] = item.model_copy(update={"cites": cites, "related": []}) if cites else None
    note = f"Dropped {dropped_items} items and {dropped_citations} citations: evidence did not check."
    unknowns += [note] if dropped_citations or dropped_items else []
    return draft.model_copy(update={**sections, **frame, "unknowns": unknowns})
