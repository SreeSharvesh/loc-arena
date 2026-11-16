"""What `make publish-run` refuses before it publishes: a real secret in the bundle, never an in-world one."""

import zipfile
from pathlib import Path

import pytest
from loc_arena.run_publish import PublishSettings, find_secrets

from tests._stored_run import unpack_stored_run

FAKE_KEY = "sk-or-v1-" + "0123456789abcdef" * 4  # the provider key's form
MACHINE = {"PROXY_PASSWORD": "machine-held-proxy-password", "HOME": "/home/user"}


def write_second_line(bundle: Path, place: str, line: str) -> None:
    """Write ``line`` as the second line of ``place``: a file of the bundle, or ``zip!member``."""
    text = f"first line\n{line}\n"
    file, _, member = place.partition("!")
    (bundle / file).parent.mkdir(parents=True, exist_ok=True)
    if member:
        with zipfile.ZipFile(bundle / file, "w") as archive:
            archive.writestr(member, text)
    else:
        (bundle / file).write_text(text)


@pytest.mark.parametrize(
    ("place", "line"),
    [
        ("services/agentgateway.log", f'{{"upstream": "openrouter", "key": "{FAKE_KEY}"}}'),
        (
            "gateway.calls.jsonl",
            '{"headers": {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.c2lnbmF0dXJl"}}',
        ),
        ("episode/records/workspace.jsonl", "proxy=http://user:machine-held-proxy-password@proxy:3128"),
        ("run.eval!samples/1_epoch_1.json", f'{{"output": "export OPENROUTER_API_KEY={FAKE_KEY}"}}'),
    ],
    ids=["provider key", "authorization credential", "environment value", "inside the inspect log"],
)
def test_a_real_secret_is_refused_at_its_file_and_line(tmp_path: Path, place: str, line: str) -> None:
    write_second_line(tmp_path, place, line)

    findings = find_secrets(tmp_path, PublishSettings(), MACHINE)

    assert [finding.split(": ", 1)[0] for finding in findings] == [f"{place}:2"]


def test_the_stored_run_with_its_in_world_tokens_passes(tmp_path: Path) -> None:
    """Its agents' git configs carry their forge tokens in Authorization headers, which the company minted."""
    run_directory = unpack_stored_run(tmp_path)

    findings = find_secrets(run_directory, PublishSettings(), {"OPENROUTER_API_KEY": FAKE_KEY, **MACHINE})

    assert findings == []
