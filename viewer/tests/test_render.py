"""A portable report preserves complete evidence without interpreting it as HTML."""

from __future__ import annotations

import json
from html.parser import HTMLParser

from run_insights.models import Outcome, Record, RunData
from run_insights.render import render_html


class EmbeddedData(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts = 0
        self.reading = False
        self.data = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self.scripts += 1
            self.reading = dict(attrs).get("id") == "report-data"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.reading = False

    def handle_data(self, data: str) -> None:
        if self.reading:
            self.data += data


def test_portable_report_preserves_complete_record_without_executing_its_markup() -> None:
    transcript = "</script><script>window.compromised = true</script>" + " complete evidence" * 1000
    run = RunData(
        name="run-95519d",
        fingerprint="source",
        records=[
            Record(
                id="attack:145",
                actor="worker",
                kind="message",
                time=3.0,
                play="attack",
                stream="sealed",
                raw={"payload": {"body": transcript}},
            ),
        ],
        outcome=Outcome(main_score=1.0, landed=False, caught=False),
    )

    report = render_html(run, None, run_key="server-only-key")
    parsed = EmbeddedData()
    parsed.feed(report)
    embedded = json.loads(parsed.data)

    assert embedded["run"]["records"][0]["raw"]["payload"]["body"] == transcript
    assert embedded["live"] is False
    assert embedded["run_key"] == ""
    assert parsed.scripts == 2
    assert "window.compromised = true</script>" not in report
