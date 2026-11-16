"""Publish a graded run to the private storage repository, so its records outlive the machine that played it.

The storage repository's README gives the layout: the run's readable files are committed under
``runs/<date>/<config>/<run id>/``, beside ``bundle.txt``, which names the release tagged
``<date>-<config>-<run id>`` that holds the whole bundle as one compressed asset. Nothing is published until
``find_secrets`` finds nothing real in the bundle. Each of the two parts is written only when it is absent, so
nothing published is ever replaced. git makes the commit; ``gh api``, GitHub's REST API, makes the release.
"""

from __future__ import annotations

import io
import json
import math
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from re import Pattern
from urllib.parse import urlsplit

import zipfile_zstd  # noqa: F401 - reads the zstd members of Inspect's .eval zips, as inspect_ai itself does
from dulwich.bundle import read_bundle
from dulwich.object_store import BaseObjectStore, DiskObjectStore, MemoryObjectStore
from pydantic import Field, PositiveFloat, PositiveInt, StrictStr

from loc_arena.service_settings import SettingsGroup

GIT_BUNDLE = re.compile(rb"# v[23] git bundle\n")  # the first line of a git bundle
LABEL = re.compile(r"[a-z0-9][a-z0-9._-]*")  # a config name or a run id: one path segment, no traversal


class PublishSettings(SettingsGroup):
    """Where ``make publish-run`` stores a run, and what its scan refuses (the ``publish:`` block)."""

    repository: StrictStr | None = Field(
        default=None,
        description="The GitHub repository, as owner/name, that stores published runs; unset, none is.",
    )
    branch: StrictStr = Field(
        default="main",
        description="The storage repository's branch a run is committed to.",
    )
    readable_files: tuple[StrictStr, ...] = Field(
        default=("scores.json", "config.resolved.yaml", "cost.json", "explorer*.html", "readings"),
        description="The bundle's files and folders, as glob patterns, committed where a reader opens them.",
    )
    key_patterns: tuple[Pattern[str], ...] = Field(
        default=(
            re.compile(r"sk-or-v1-[0-9a-f]{64}"),
            re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
            re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
        ),
        description="The forms of a real key: the provider's and GitHub's. A match anywhere is refused.",
    )
    authorization_pattern: Pattern[str] = Field(
        default=re.compile(
            r"(?i)authorization\\*[\"']?\s*[:=]\s*\[?\s*\\*[\"']?(?:bearer|basic|token)\s+([A-Za-z0-9._~+/=-]{16,})",
        ),
        description="An Authorization header with a credential, its first group, quoted, or escaped as JSON "
        "within JSON; refused unless in-world.",
    )
    in_world_token_pattern: Pattern[str] = Field(
        default=re.compile(r"(?:mi_)?[A-Za-z0-9_-]{43}"),
        description="The form of the company's credentials, each minted for one run as 32 URL-safe random "
        "bytes, which an Authorization header may carry and the run keeps; matched against the whole of one, "
        "less a full stop that ends a sentence.",
    )
    environment_name_pattern: Pattern[str] = Field(
        default=re.compile(
            r"(?i)(?:^|_)(?:key|token|secret|password|passwd|credentials?|auth)$|(?:^|_)(?:secret|password)_",
        ),
        description="A variable of the machine's environment or `.env` whose name matches holds a secret: "
        "its value anywhere in the bundle is refused, as is the password of any URL a variable holds.",
    )
    environment_value_min_length: PositiveInt = Field(
        default=16,
        description="A secret-named variable's value shorter than this is not searched for: it would match "
        "ordinary text.",
    )
    command_seconds: PositiveFloat = Field(
        default=600.0,
        description="The longest one git or gh command of a publish may take; past it the publish fails.",
    )


class PublishError(RuntimeError):
    """A run that could not be published; the message says why."""


@dataclass(frozen=True)
class Label:
    """What a published run is filed under: the day it finished, its config's name and its run id."""

    date: str
    config: str
    run_id: str

    @property
    def tag(self) -> str:
        """The release's tag, and the stem of its asset."""
        return f"{self.date}-{self.config}-{self.run_id}"

    def release_link(self, repository: str) -> str:
        """The page of its release in ``repository``."""
        return f"https://github.com/{repository}/releases/tag/{self.tag}"

    @property
    def directory(self) -> str:
        """Where its readable files are committed in the storage repository."""
        return f"runs/{self.date}/{self.config}/{self.run_id}"


def read_label(bundle: Path, finished_at: float | None) -> Label:
    """The label of the graded run in ``bundle``, which finished at ``finished_at`` by its own record."""
    if finished_at is None or not math.isfinite(finished_at):
        raise PublishError(f"{bundle} records no time its play finished, so it has no date to be filed under")
    scores = json.loads((bundle / "scores.json").read_text())
    config, run_id = scores.get("config_name"), str(scores.get("run_name", "")).rsplit("__", 1)[-1]
    if not isinstance(config, str):
        raise PublishError(f"{bundle / 'scores.json'} names no config_name: the run predates it")
    for label in (config, run_id):
        if not LABEL.fullmatch(label):
            raise PublishError(f"{label!r} in {bundle / 'scores.json'} is no config name or run id")
    return Label(datetime.fromtimestamp(finished_at, UTC).date().isoformat(), config, run_id)


def find_secrets(
    bundle: Path,
    settings: PublishSettings,
    environment: Iterable[tuple[str, str]],
) -> list[str]:
    """Each line of ``bundle`` that holds a real secret, as ``where:line: what``.

    Every file is read as text; a zip's members, and every object of a git store or a git bundle, are read
    too, as ``file!member``. ``environment`` holds the machine's variables, as name and value, from every
    source: the values of those with secret names, and the password of each URL, are searched for. A link, or
    anything the scan cannot read, is refused: the scan would not have read what the publish copies.
    """
    secrets = (
        value if settings.environment_name_pattern.search(name) else urlsplit(value).password
        for name, value in environment
    )
    values = {value for value in secrets if value and len(value) >= settings.environment_value_min_length}
    findings: list[str] = []
    for path in sorted(bundle.rglob("*")):
        name = path.relative_to(bundle).as_posix()
        if path.is_symlink():
            findings.append(f"{name}: a link")
            continue
        try:
            for where, lines in _read_lines(path, name):
                findings += [
                    f"{where}:{number}: {what}"
                    for number, line in enumerate(lines, 1)
                    if (what := _find_secret(line, settings, values))
                ]
        except Exception as error:  # noqa: BLE001 - what the scan cannot read is refused, whatever the reason
            findings.append(f"{name}: unreadable ({type(error).__name__}: {error})")
    return findings


def _read_lines(path: Path, name: str) -> Iterator[tuple[str, Iterable[str]]]:
    """The lines of ``path``: a file's own, and those of each member or object it holds."""
    if path.is_dir() and (path / "HEAD").is_file() and (path / "objects").is_dir():
        yield from _store_lines(name, DiskObjectStore(str(path / "objects")))
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as file:
        yield name, file
    with path.open("rb") as file:
        if GIT_BUNDLE.fullmatch(file.readline(64)):
            file.seek(0)
            read_bundle(file).store_objects(store := MemoryObjectStore())
            yield from _store_lines(name, store)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in (member for member in archive.infolist() if not member.is_dir()):
                with archive.open(member) as raw:
                    yield (
                        f"{name}!{member.filename}",
                        io.TextIOWrapper(raw, encoding="utf-8", errors="replace"),
                    )


def _store_lines(name: str, store: BaseObjectStore) -> Iterator[tuple[str, list[str]]]:
    for sha in store:
        yield (
            f"{name}!{sha.decode()}",
            store[sha].as_raw_string().decode("utf-8", errors="replace").splitlines(),
        )


def _find_secret(line: str, settings: PublishSettings, values: set[str]) -> str | None:
    if any(pattern.search(line) for pattern in settings.key_patterns):
        return "a key of a real form"
    for header in settings.authorization_pattern.finditer(line):
        if not settings.in_world_token_pattern.fullmatch(header[1].removesuffix(".")):
            return "an Authorization credential that is not in-world"
    if any(value in line for value in values):
        return "the value of a secret variable of this machine's environment"
    return None


@dataclass(frozen=True)
class Published:
    """Where a run is published, and whether it was already, so this publish wrote nothing."""

    link: str
    already: bool


def publish_run(bundle: Path, label: Label, settings: PublishSettings) -> Published:
    """Commit ``bundle``'s readable files and release the whole bundle, each only when it is absent.

    A release stays a draft until its asset is uploaded, so a published release always holds its bundle; a
    publish that stopped before then finishes the draft it left.
    """
    if settings.repository is None:
        raise PublishError("the config sets no publish.repository")
    link = label.release_link(settings.repository)
    with tempfile.TemporaryDirectory() as work:
        clone, archive = Path(work) / "storage", Path(work) / f"{label.tag}.tar.gz"
        options = ("--depth=1", "--sparse", "--filter=blob:none", "--branch", settings.branch)
        _run(settings, "git", "clone", *options, f"https://github.com/{settings.repository}.git", clone)
        committed = bool(_run(settings, "git", "-C", clone, "ls-tree", "HEAD", label.directory))
        release = _find_release(settings, label.tag, archive.name)
        if committed and release and not release.draft:
            return Published(link, already=True)
        if not committed:
            _commit(bundle, label, clone, settings)
        if release is None or release.draft:
            commit = _run(settings, "git", "-C", clone, "rev-parse", "HEAD").strip()
            release = release or _draft(settings, label, commit)
            if not release.uploaded:
                with tarfile.open(archive, "w:gz") as tar:
                    tar.add(bundle, arcname=bundle.name)
                upload = f"https://uploads.github.com/{release.path}/assets?name={archive.name}"
                _run(
                    settings,
                    "gh",
                    "api",
                    "-X",
                    "POST",
                    upload,
                    "-H",
                    "Content-Type: application/gzip",
                    "--input",
                    archive,
                )
            _run(settings, "gh", "api", "-X", "PATCH", release.path, "-F", "draft=false")
    return Published(link, already=False)


def _commit(bundle: Path, label: Label, clone: Path, settings: PublishSettings) -> None:
    target = clone / label.directory
    target.mkdir(parents=True)
    for source in (path for pattern in settings.readable_files for path in sorted(bundle.glob(pattern))):
        copy = shutil.copytree if source.is_dir() else shutil.copy2
        copy(source, target / source.name)
    link = label.release_link(str(settings.repository))
    (target / "bundle.txt").write_text(f"release: {link}\nasset: {label.tag}.tar.gz\n")
    _run(settings, "git", "-C", clone, "add", "--sparse", label.directory)
    _run(settings, "git", "-C", clone, "commit", "-q", "-m", f"Publish run {label.tag}")
    _run(settings, "git", "-C", clone, "push", "-q", "origin", f"HEAD:{settings.branch}")


@dataclass(frozen=True)
class _Release:
    path: str  # its REST resource
    draft: bool
    uploaded: bool  # it holds the bundle's asset, whole


def _find_release(settings: PublishSettings, tag: str, asset: str) -> _Release | None:
    """The release tagged ``tag``, a draft too: GitHub lists a draft, though it finds none by its tag."""
    releases = f"repos/{settings.repository}/releases"
    uploaded = f'any(.assets[]; .name == "{asset}" and .state == "uploaded")'
    query = f'.[] | select(.tag_name == "{tag}") | [.id, .draft, {uploaded}] | @tsv'
    found = _run(settings, "gh", "api", "--paginate", f"{releases}?per_page=100", "--jq", query).split()
    return _Release(f"{releases}/{found[0]}", found[1] == "true", found[2] == "true") if found else None


def _draft(settings: PublishSettings, label: Label, commit: str) -> _Release:
    releases = f"repos/{settings.repository}/releases"
    body = f"The whole bundle of run {label.tag}; its readable files are in {label.directory}."
    fields = [f"tag_name={label.tag}", f"target_commitish={commit}", f"name={label.tag}", f"body={body}"]
    arguments = [part for field in fields for part in ("-f", field)]
    created = _run(
        settings,
        "gh",
        "api",
        "-X",
        "POST",
        releases,
        *arguments,
        "-F",
        "draft=true",
        "--jq",
        ".id",
    )
    return _Release(f"{releases}/{created.strip()}", draft=True, uploaded=False)


def _run(settings: PublishSettings, *command: str | Path) -> str:
    try:
        done = subprocess.run(
            [str(part) for part in command],
            capture_output=True,
            text=True,
            timeout=settings.command_seconds,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise PublishError(f"{error.cmd[:3]} failed: {error.stderr.strip()}") from error
    except subprocess.TimeoutExpired as error:
        raise PublishError(f"{error.cmd[:3]} took longer than {settings.command_seconds}s") from error
    return done.stdout
