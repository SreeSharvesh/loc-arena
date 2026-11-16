"""What `make publish-run` refuses before it publishes: a real secret in the bundle, never an in-world one."""

import subprocess
import zipfile
from pathlib import Path

import pytest
from loc_arena.run_publish import PublishSettings, find_secrets

from tests._stored_run import unpack_stored_run

FAKE_KEY = "sk-or-v1-" + "0123456789abcdef" * 4  # the provider key's form
MACHINE = [
    ("GH_TOKEN", "machine-held-github-token"),
    ("HTTPS_PROXY", "http://user:machine-held-proxy-password@proxy:3128"),
    ("HOME", "/home/user"),
]
IN_WORLD = "hzuNJ6dPmo_2Shc94DTmlVMMY0XsYk-w2oPoEGyPJEs"  # a forge token of the stored run


def write_second_line(bundle: Path, place: str, line: str, work: Path) -> str:
    """Write ``line`` as the second line of ``place`` in ``bundle``, and say where the scan finds it.

    ``place`` is a file, ``zip!member``, or ``store!git``: a git store, or a git bundle when it ends in
    ``.bundle``, whose one commit holds the line in a file.
    """
    text = f"first line\n{line}\n"
    file, _, member = place.partition("!")
    (bundle / file).parent.mkdir(parents=True, exist_ok=True)
    if member != "git":
        if member:
            with zipfile.ZipFile(bundle / file, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(member, text)
        else:
            (bundle / file).write_text(text)
        return place
    repository = work / "repository"
    (repository / "notes").mkdir(parents=True)
    (repository / "notes" / "run.md").write_text(text)
    git = ("git", "-c", "user.name=agent", "-c", "user.email=agent@meridian.example", "-C")
    subprocess.run([*git, str(repository), "init", "-q"], check=True)
    subprocess.run([*git, str(repository), "add", "."], check=True)
    subprocess.run([*git, str(repository), "commit", "-qm", "notes"], check=True)
    if file.endswith(".bundle"):
        subprocess.run(
            [*git, str(repository), "bundle", "create", "-q", str(bundle / file), "--all"],
            check=True,
        )
    else:
        subprocess.run(["git", "clone", "-q", "--bare", str(repository), str(bundle / file)], check=True)
    blob = subprocess.run(
        [*git, str(repository), "hash-object", "notes/run.md"],
        check=True,
        capture_output=True,
    )
    return f"{file}!{blob.stdout.decode().strip()}"


@pytest.mark.parametrize(
    ("place", "line"),
    [
        ("services/agentgateway.log", f'{{"upstream": "openrouter", "key": "{FAKE_KEY}"}}'),
        (
            "gateway.calls.jsonl",
            '{"headers": {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.e30.c2lnbmF0dXJl"}}',
        ),
        (
            "run.eval!samples/1_epoch_1.json",
            r'{"output": "{\"Authorization\": \"Bearer eyJhbGciOiJIUzI1NiJ9.e30\"}"}',
        ),
        ("episode/records/workspace.jsonl", '{"output": "token machine-held-github-token"}'),
        ("services/forge.log", "connect through the proxy with machine-held-proxy-password"),
        ("run.eval!samples/1_epoch_1.json", f'{{"output": "export OPENROUTER_API_KEY={FAKE_KEY}"}}'),
        ("episode/forge/meridian-serving.git!git", f"OPENROUTER_API_KEY={FAKE_KEY}"),
        ("episode/forge_history/meridian-serving.bundle!git", f"OPENROUTER_API_KEY={FAKE_KEY}"),
    ],
    ids=[
        "provider key",
        "authorization credential",
        "authorization credential escaped in JSON",
        "environment value",
        "password of a proxy URL",
        "inside the inspect log",
        "inside a git store",
        "inside a git bundle",
    ],
)
def test_a_real_secret_is_refused_at_its_file_and_line(tmp_path: Path, place: str, line: str) -> None:
    bundle = tmp_path / "bundle"
    where = write_second_line(bundle, place, line, tmp_path)

    findings = find_secrets(bundle, PublishSettings(), MACHINE)

    assert [finding.split(": ", 1)[0] for finding in findings] == [f"{where}:2"]


def test_the_stored_run_with_its_in_world_tokens_passes(tmp_path: Path) -> None:
    """Its agents' git configs carry their forge tokens in Authorization headers, which the company minted."""
    run_directory = unpack_stored_run(tmp_path)
    (run_directory / "decisions.md").write_text(f"The agent sent Authorization: Bearer {IN_WORLD}.\n")

    findings = find_secrets(run_directory, PublishSettings(), [("OPENROUTER_API_KEY", FAKE_KEY), *MACHINE])

    assert findings == []
