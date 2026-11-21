"""Structured run insights: one model call over a run's digest, in the reader's sandbox."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

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
from run_insights.digest import digest_run
from run_insights.render import render_report
from run_insights.server import discover_runs

from loc_arena.config import RunConfig, load_run_config
from loc_arena.gateway.proxy_relay import relay_while_running
from loc_arena.reader_execution import (
    BRIEFS,
    READER,
    run_reader_process,
    settle_reading,
    write_one_call,
)
from loc_arena.run_cost import read_dollars


def analyze_run(bundle: Path, config: RunConfig, force: bool = False) -> Path:
    """Analyze a bundle through the recorded gateway and atomically keep validated insights.

    A cache is reused only for the same source records, reader model, prompt, schema, and reader settings.
    Existing insights survive a failed or invalid replacement. No provider credential enters the reader.
    """
    bundle = bundle.resolve()
    run = load_run(bundle)
    if READER not in config.models:
        raise ValueError("the run config's models file has no 'reader' role")
    limits, role = config.settings.reader, config.models[READER]
    brief = (BRIEFS / "_insights.md").read_text()
    brief = brief.replace("CLAIM_LIMIT", str(limits.claim_limit))
    brief = brief.replace("CHARACTER_LIMIT", str(limits.claim_limit_characters))
    schema = InsightDraft.model_json_schema()
    brief = brief.replace("SCHEMA", json.dumps(schema))
    providers = config.model_providers.get(role.model)
    configuration = {
        "brief": brief,
        "schema": schema,
        "model": asdict(role),
        "providers": providers.model_dump(mode="json") if providers else None,
        "reader": limits.model_dump(mode="json"),
    }
    fingerprint = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
    report = analysis_path(bundle)
    calls = report.with_suffix(".calls.jsonl")
    if any(path.is_symlink() for path in (report.parent, report, calls, calls.with_suffix(".otlp.jsonl"))):
        raise RuntimeError(f"{report.parent} holds a link where insights are written: nothing is written")
    cached = load_analysis(bundle, run)
    if (
        not force
        and cached is not None
        and cached.model == role.model
        and cached.configuration_fingerprint == fingerprint
    ):
        return report
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="locarena-insights-") as scratch:
        home, work = Path(scratch, "home"), Path(scratch, "work")
        work.mkdir()
        digest, handles = digest_run(run)
        command = write_one_call(home, config, brief, digest, schema)
        written = run_reader_process(config, bundle, home, work, command, calls)
    try:
        draft = validate_draft(InsightDraft.model_validate_json(written), run, handles)
    except ValueError as error:
        lines = str(error).splitlines()
        reason = " ".join(line.strip() for line in lines[1:3])[:240] or lines[0][:240]
        raise ValueError(f"the reader's insights do not match the schema: {reason}") from None
    # As a reading does: a claim longer than the limit, or past the count, is dropped, not the whole analysis.
    room, longest, sections = limits.claim_limit, limits.claim_limit_characters, {}

    def short(item: Insight) -> bool:
        return max(len(item.title), len(item.text), len(item.why_it_matters)) <= longest

    for name in ("overview", "chain", "insights"):
        fits = [item for item in getattr(draft, name) if short(item)]
        sections[name], room = fits[:room], max(0, room - len(fits))
    for name in ("chain_start", "chain_end"):
        item = getattr(draft, name)
        sections[name] = item if item and short(item) else None
    sections["unknowns"] = [text for text in draft.unknowns if len(text) <= longest]
    sections["coverage"] = [text for text in draft.coverage if len(text) <= longest]
    draft = draft.model_copy(update=sections)
    cost = read_dollars(calls)
    analysis = Analysis(
        source_fingerprint=run.fingerprint,
        configuration_fingerprint=fingerprint,
        model=role.model,
        generated_at=datetime.now(UTC),
        elapsed_seconds=time.monotonic() - started,
        calls=sum(bool(line.strip()) for line in calls.read_text().splitlines()),
        cost_usd=cost.total if cost.by_model else None,
        **draft.model_dump(),
    )
    descriptor, name = tempfile.mkstemp(prefix=".insights-", suffix=".json", dir=report.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(analysis.model_dump_json(indent=2) + "\n")
        temporary.replace(report)
    finally:
        temporary.unlink(missing_ok=True)
    return report


def prepare_report(bundle: Path, config: RunConfig, force: bool = False) -> Path:
    """Analyze a bundle and write its offline report beside the insights: opening it calls no model."""
    analyze_run(bundle, config, force)
    run = load_run(bundle)
    return render_report(run, load_analysis(bundle, run), analysis_path(bundle).with_suffix(".html"))


def main() -> int:
    """Prepare the report of one bundle, or of every graded bundle under a directory, failing per bundle."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="A graded run bundle, or a directory holding several")
    parser.add_argument("--config", type=Path, default=Path("configs/aurora-efficiency.yaml"))
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args()
    failed = 0
    config = load_run_config(arguments.config)
    with relay_while_running(settle_reading(config).gateway):
        for bundle in discover_runs(arguments.path).values():
            try:
                print(prepare_report(bundle, config, arguments.force))
            except ValidationError:
                failed += 1
                print(f"insights: {bundle.name}: failed schema validation", file=sys.stderr)
            except (OSError, ValueError, RuntimeError) as error:
                failed += 1
                print(f"insights: {bundle.name}: {error}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
