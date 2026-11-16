"""What `make publish-run` refuses before it publishes: a real secret in the bundle, never an in-world one."""

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path

import pytest
from loc_arena.publish_settings import PublishSettings
from loc_arena.run_publish import Label, PublishError, SecretFoundError, find_secrets, publish_run

from tests._stored_run import unpack_stored_run

FAKE_KEY = "sk-or-v1-" + "0123456789abcdef" * 4  # the provider key's form
MACHINE = [
    ("GH_TOKEN", "machine-held-github-token"),
    ("HTTPS_PROXY", "http://user:machine-held-proxy-password@proxy:3128"),
    ("DATABASE_PASSWORD", 'machine-held "quoted" password'),
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
        ("gateway.calls.jsonl", r'{"output": "password=machine-held \"quoted\" password"}'),
        (
            "episode/records/workspace.jsonl",
            "headers=[(b'authorization', b'Bearer eyJhbGciOiJIUzI1NiJ9.e30')]",
        ),
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
        "environment value escaped in JSON",
        "authorization header as a pair",
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


def test_what_the_publish_leaves_out_is_not_scanned(tmp_path: Path) -> None:
    write_second_line(
        tmp_path,
        "episode/checkout/meridian-serving/config.py",
        f"KEY = '{FAKE_KEY}'",
        tmp_path,
    )

    findings = find_secrets(tmp_path, PublishSettings(), MACHINE, ("episode/checkout",))

    assert findings == []


def test_a_secret_in_a_sqlite_row_is_found_in_its_dump(tmp_path: Path) -> None:
    with contextlib.closing(sqlite3.connect(tmp_path / "forge.db")) as database:
        database.execute("CREATE TABLE notes (body TEXT)")
        database.execute("INSERT INTO notes VALUES (?)", (f"key {FAKE_KEY}",))
        database.commit()

    findings = find_secrets(tmp_path, PublishSettings(), MACHINE)

    assert "forge.db!rows:3: a key of a real form" in findings  # after BEGIN and CREATE TABLE


def test_the_stored_run_with_its_in_world_tokens_passes(tmp_path: Path) -> None:
    """Its agents' git configs carry their forge tokens in Authorization headers, which the company minted."""
    run_directory = unpack_stored_run(tmp_path)
    (run_directory / "decisions.md").write_text(f"The agent sent Authorization: Bearer {IN_WORLD}.\n")
    (run_directory / "notes.md").symlink_to("decisions.md")  # kept as a link, as an agent's merge may hold

    findings = find_secrets(run_directory, PublishSettings(), [("OPENROUTER_API_KEY", FAKE_KEY), *MACHINE])

    assert findings == []


LABEL = Label("2026-11-16", "aurora-efficiency.local", "abc123")  # a machine's own config


def git(*arguments: str | Path) -> str:
    """Run git with ``arguments``; what it printed."""
    done = subprocess.run(["git", *map(str, arguments)], check=True, capture_output=True, text=True)
    return done.stdout


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Commits are made as one author."""
    for variable in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(variable, "publisher")
    for variable in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(variable, "publisher@example.com")


def commit_repository(repository: Path, files: dict[str, str]) -> None:
    git("init", "-q", "-b", "main", repository)
    for name, text in files.items():
        (repository / name).write_text(text)
    git("-C", repository, "add", ".")
    git("-C", repository, "commit", "-qm", "start")


@pytest.fixture
def storage(tmp_path: Path, identity: None) -> str:
    """A storage repository with one commit, as a file URL."""
    commit_repository(tmp_path / "seed", {"README.md": "runs\n"})
    git("clone", "-q", "--bare", tmp_path / "seed", tmp_path / "storage.git")
    return (tmp_path / "storage.git").as_uri()


@pytest.fixture
def run_bundle(tmp_path: Path, identity: None) -> Path:
    """A graded run's bundle, finished a minute ago: records, a forge history, and the checkout graded on it.

    The checkout is the forge's ``git archive`` of ``main``, with a cache its grading wrote.
    """
    bundle = tmp_path / "logs" / "slug" / "run__abc123"
    files = {
        "scores.json": json.dumps({"run_name": "run__abc123"}),
        "readings/agents.md": "a reading\n" * 200,  # larger than the parts the test asks for
        "events.sealed.jsonl": os.urandom(3000).hex(),  # random, so the archive is split into parts
        "episode/checkout/meridian-common/__pycache__/main.cpython-312.pyc": "cache\n",
    }
    for name, text in files.items():
        (bundle / name).parent.mkdir(parents=True, exist_ok=True)
        (bundle / name).write_text(text)
    commit_repository(tmp_path / "meridian-common", {"main.py": "print('main')\n"})
    (bundle / "episode/forge_history").mkdir()
    git(
        "-C",
        tmp_path / "meridian-common",
        "bundle",
        "create",
        "-q",
        bundle / "episode/forge_history/meridian-common.bundle",
        "--all",
    )
    archived = subprocess.run(
        ["git", "-C", str(tmp_path / "meridian-common"), "archive", "main"],
        check=True,
        capture_output=True,
    )
    with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
        archive.extractall(bundle / "episode/checkout/meridian-common", filter="data")
    finished = time.time() - 60
    for path in (bundle, *bundle.rglob("*")):
        os.utime(path, (finished, finished))
    return bundle


@pytest.mark.parametrize(
    ("changed", "kept"),
    [
        (False, set()),
        (
            True,
            {
                "episode/checkout",
                "episode/checkout/meridian-common",
                "episode/checkout/meridian-common/main.py",
            },
        ),
    ],
    ids=["checkout its history rebuilds is left out", "checkout its history does not rebuild is kept"],
)
def test_a_published_run_joins_and_unpacks_to_its_bundle_less_what_is_rebuilt(
    tmp_path: Path,
    storage: str,
    run_bundle: Path,
    changed: bool,
    kept: set[str],
) -> None:
    main = run_bundle / "episode/checkout/meridian-common/main.py"
    if changed:
        main.write_text("print('not what the forge holds')\n")
        for path in (main, main.parent):
            os.utime(path, (time.time() - 60, time.time() - 60))

    publish_run(run_bundle, LABEL, PublishSettings(repository=storage, part_bytes=1024), [])

    git("clone", "-q", storage, tmp_path / "fetched")
    folder = tmp_path / "fetched" / "runs/2026-11-16/aurora-efficiency/abc123"
    joined = b"".join(part.read_bytes() for part in sorted(folder.glob("bundle.tar.xz.[0-9]*")))
    with tarfile.open(fileobj=io.BytesIO(joined), mode="r:xz") as archive:
        names = {name.removeprefix("run__abc123/") for name in archive.getnames()}
    assert f"sha256 {hashlib.sha256(joined).hexdigest()}" in (folder / "bundle.txt").read_text()
    assert sorted(path.name for path in folder.iterdir() if not path.name.startswith("bundle.tar.xz.")) == [
        "bundle.txt",
        "scores.json",
    ]
    cache = {
        "episode/checkout/meridian-common/__pycache__",
        "episode/checkout/meridian-common/__pycache__/main.cpython-312.pyc",
    }
    assert names == {
        "run__abc123",
        "scores.json",
        "readings",
        "readings/agents.md",
        "events.sealed.jsonl",
        "episode",
        "episode/forge_history",
        "episode/forge_history/meridian-common.bundle",
    } | kept | (cache if kept else set())


def test_a_bundle_holding_a_secret_publishes_nothing(storage: str, run_bundle: Path) -> None:
    (run_bundle / "events.sealed.jsonl").write_text(f"OPENROUTER_API_KEY={FAKE_KEY}\n")
    commits = git("--git-dir", storage.removeprefix("file://"), "rev-list", "--count", "main")

    with pytest.raises(SecretFoundError):
        publish_run(run_bundle, LABEL, PublishSettings(repository=storage), [])

    assert git("--git-dir", storage.removeprefix("file://"), "rev-list", "--count", "main") == commits


def test_a_file_changed_after_the_scan_began_is_not_published(storage: str, run_bundle: Path) -> None:
    later = time.time() + 600  # written while the publish runs
    os.utime(run_bundle / "events.sealed.jsonl", (later, later))

    with pytest.raises(PublishError, match="events.sealed.jsonl changed after the scan began"):
        publish_run(run_bundle, LABEL, PublishSettings(repository=storage), [])


def test_a_second_publish_of_a_run_writes_nothing(storage: str, run_bundle: Path) -> None:
    settings = PublishSettings(repository=storage)
    publish_run(run_bundle, LABEL, settings, [])
    commits = git("--git-dir", storage.removeprefix("file://"), "rev-list", "--count", "main")

    published = publish_run(run_bundle, LABEL, settings, [])

    assert published.already
    assert git("--git-dir", storage.removeprefix("file://"), "rev-list", "--count", "main") == commits


def test_another_run_under_the_same_label_is_refused(storage: str, run_bundle: Path) -> None:
    settings = PublishSettings(repository=storage)
    publish_run(run_bundle, LABEL, settings, [])
    (run_bundle / "scores.json").write_text(json.dumps({"run_name": "other__abc123"}))
    os.utime(run_bundle / "scores.json", (time.time() - 60, time.time() - 60))

    with pytest.raises(PublishError, match="holds another run, run__abc123"):
        publish_run(run_bundle, LABEL, settings, [])
