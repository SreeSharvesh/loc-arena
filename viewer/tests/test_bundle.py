"""Public evidence and completeness guarantees on small independent run fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import write_lines
from pydantic import ValidationError
from run_insights import (
    Analysis,
    Insight,
    InsightDraft,
    analysis_path,
    load_analysis,
    load_run,
    validate_draft,
)

LONG_TEXT = "Every byte of this tool result remains available. " * 30


def insight(**changes: object) -> Insight:
    """One small grounded claim whose variations exercise invalid model output."""
    values = {
        "id": "observed",
        "title": "Read completed",
        "text": "The lead read a result.",
        "why_it_matters": "The result is preserved.",
        "status": "completed",
        "cites": ["attack:99"],
    }
    return Insight.model_validate(values | changes)


def test_judgments_target_the_right_play_and_mirror_namespace(bundle: Path) -> None:
    """Equal integers across streams cannot substitute for an actual monitor target."""
    run = load_run(bundle)

    records = {record.id: record for record in run.records}
    assert records["attack:7"].verdicts == []
    assert records["attack:99"].verdicts[0].target_id == "attack-mirror:7"
    assert records["attack-mirror:7"].verdicts[0].score == 0.4
    assert records["attack-mirror:7"].verdicts[0].target_id == "attack-mirror:7"
    assert records["twin-mirror:7"].verdicts[0].score == 0.2
    assert run.outcome.threshold == 0.6


@pytest.mark.parametrize("changed", ["tampered", "ambiguous"])
def test_mirror_judgments_never_attach_to_changed_or_ambiguous_sealed_records(
    bundle: Path,
    changed: str,
) -> None:
    """Only a unique match of complete content can prove a cross-stream counterpart."""
    if changed == "tampered":
        path = bundle / "episode" / "events.mirror.jsonl"
        record = json.loads(path.read_text())
        record["result"] = {"text": "changed in mirror"}
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    else:
        path = bundle / "episode" / "events.sealed.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records.append(records[-1] | {"seq": 100})
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    run = load_run(bundle)

    records = {record.id: record for record in run.records}
    assert records["attack:99"].verdicts == []
    assert len(records["attack-mirror:7"].verdicts) == 1
    assert any("no unique identical sealed counterpart" in warning for warning in run.warnings)


def test_all_records_preserve_long_results_sessions_unknown_fields_and_composed_briefs(bundle: Path) -> None:
    """The viewer can expose full source text even when highlights are shortened."""
    run = load_run(bundle)

    records = {record.id: record for record in run.records}
    assert len(records) == 8
    assert records["attack:99"].raw["result"] == {"text": LONG_TEXT}
    assert records["attack:99"].raw["future_field"] == ["retained", {"nested": True}]
    assert records["twin-session:lead:reply"].raw["message"] == {
        "role": "assistant",
        "content": [{"type": "text", "text": LONG_TEXT}],
    }
    assert run.agents[0].brief == "Improve the pipeline."
    assert run.agents[0].covert_brief == "First fragment.\n\nSecond fragment."


def test_saved_analysis_is_reused_only_for_unchanged_source_records(bundle: Path) -> None:
    """Writing the generated analysis does not invalidate its own fingerprint."""
    run = load_run(bundle)
    draft = InsightDraft(overview=[insight()])
    analysis = Analysis(
        **draft.model_dump(),
        source_fingerprint=run.fingerprint,
        model="test-model",
        generated_at=datetime(2026, 11, 21, tzinfo=UTC),
    )
    path = analysis_path(bundle)
    path.parent.mkdir()
    path.write_text(analysis.model_dump_json(), encoding="utf-8")

    assert load_analysis(bundle, load_run(bundle)) == analysis
    session = bundle / "episode" / "loops" / "lead" / ".pi" / "session.jsonl"
    session.write_text(session.read_text().replace("available", "changed"), encoding="utf-8")
    assert load_analysis(bundle, load_run(bundle)) is None


@pytest.mark.parametrize("cites", [["attack:404"], ["scores:side_task"], ["scores:side_task.missing"]])
def test_claim_citing_only_invented_evidence_is_dropped(bundle: Path, cites: list[str]) -> None:
    """Untrusted output is checked against exact records and scalar score paths."""
    checked = validate_draft(InsightDraft(insights=[insight(cites=cites)]), load_run(bundle))

    assert checked.insights == []
    assert checked.unknowns == ["Dropped 1 items and 1 citations: evidence did not check."]


@pytest.mark.parametrize("related", [["missing"], ["observed"]])
def test_claim_keeps_its_evidence_but_loses_an_invalid_link(bundle: Path, related: list[str]) -> None:
    checked = validate_draft(InsightDraft(insights=[insight(related=related)]), load_run(bundle))

    assert [item.related for item in checked.insights] == [[]]


def test_saved_score_facts_and_prior_chain_links_are_valid_evidence(bundle: Path) -> None:
    """An overview can cite grader facts and a later chain item can link to an earlier one."""
    draft = InsightDraft(
        overview=[insight(id="summary", cites=["scores:side_task.landing_minimal"])],
        chain=[insight(id="first"), insight(id="second", related=["first"])],
    )

    assert validate_draft(draft, load_run(bundle)) == draft


def test_repeated_identifier_is_dropped_and_forward_chain_link_removed(bundle: Path) -> None:
    """The graph has stable node identity and only explicitly earlier predecessors."""
    draft = InsightDraft(
        overview=[insight(id="first")],
        chain=[insight(id="first"), insight(id="second", related=["third"]), insight(id="third")],
    )

    checked = validate_draft(draft, load_run(bundle))

    assert [item.id for item in checked.chain] == ["second", "third"]
    assert checked.chain[0].related == []


def test_older_root_event_files_are_loaded_without_fabricating_missing_sessions(bundle: Path) -> None:
    """Older bundles remain readable and disclose missing full session messages."""
    for stream in ("sealed", "mirror"):
        (bundle / "episode" / f"events.{stream}.jsonl").rename(bundle / f"events.{stream}.jsonl")
    session = bundle / "episode" / "loops" / "lead" / ".pi" / "session.jsonl"
    session.unlink()

    run = load_run(bundle)

    assert any(record.id == "attack:99" for record in run.records)
    assert any("attack: pi session files are absent" in warning for warning in run.warnings)


def test_loading_refuses_sources_symlinked_outside_the_bundle(bundle: Path, tmp_path: Path) -> None:
    """A selected bundle cannot smuggle another host file into analysis through a symlink."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    source = bundle / "episode" / "events.sealed.jsonl"
    source.unlink()
    source.symlink_to(outside)

    with pytest.raises(ValueError, match="source escapes"):
        load_run(bundle)


def test_invalid_event_shapes_fail_instead_of_silently_removing_evidence(bundle: Path) -> None:
    """Boolean sequence numbers cannot be coerced into valid event identifiers."""
    write_lines(
        bundle / "episode" / "events.sealed.jsonl",
        [
            {"seq": True, "actor_uid": "lead", "kind": "action", "ts": 1},
        ],
    )

    with pytest.raises(ValidationError):
        load_run(bundle)


@pytest.mark.parametrize("replacement", ["prompts_inline.missing", "prompts_inline.composed"])
def test_missing_or_cyclic_prompt_fragments_fail_with_bad_reference(
    bundle: Path,
    replacement: str,
) -> None:
    """A malformed composed brief cannot silently disappear or loop while loading."""
    path = bundle / "config.resolved.yaml"
    path.write_text(path.read_text().replace("[prompts_inline.first,", f"[{replacement},"), encoding="utf-8")

    with pytest.raises(ValueError, match="missing or cyclic prompt reference"):
        load_run(bundle)


def test_chain_frame_keeps_a_checked_start_and_drops_an_unchecked_end(bundle: Path) -> None:
    draft = InsightDraft(chain_start=insight(id="start"), chain_end=insight(id="end", cites=["attack:404"]))

    checked = validate_draft(draft, load_run(bundle))

    assert checked.chain_start is not None
    assert checked.chain_end is None
