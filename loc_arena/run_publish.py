"""Publish a graded run to the private storage repository, so its records outlive the machine that played it.

One commit per run, under ``runs/<date>/<config>/<run id>/``: the run's readable files, the whole bundle as
``bundle.tar.xz`` (in numbered parts when it is larger than ``publish.part_bytes``), and ``bundle.txt``, which
gives its size, its SHA-256 and how to join and unpack it. The archive leaves out what is rebuilt exactly from
what it keeps (``publish.left_out``). Nothing is published until ``find_secrets`` finds nothing real in the
bundle, and a run whose folder exists is never written again.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import io
import json
import lzma
import math
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from re import Pattern
from urllib.parse import urlsplit

import zipfile_zstd  # noqa: F401 - reads the zstd members of Inspect's .eval zips, as inspect_ai itself does
from dulwich.bundle import read_bundle
from dulwich.object_store import BaseObjectStore, DiskObjectStore, MemoryObjectStore
from pydantic import Field, PositiveFloat, PositiveInt, StrictStr

from loc_arena.service_settings import SettingsGroup

GIT_BUNDLE = re.compile(rb"# v[23] git bundle\n")  # the first line of a git bundle
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # a config name or a run id: one path segment, no traversal
MACHINE_SUFFIX = ".local"  # a machine's own config, configs/*.local.yaml, which git ignores
ARCHIVE = "bundle.tar.xz"
XZ_PRESET = 9 | lzma.PRESET_EXTREME  # each model call repeats its conversation: xz's large window finds it


class PublishSettings(SettingsGroup):
    """Where ``make publish-run`` stores a run, and what its scan refuses (the ``publish:`` block)."""

    repository: StrictStr | None = Field(
        default=None,
        description="The git URL of the repository that stores published runs; unset, none is published.",
    )
    branch: StrictStr = Field(
        default="main",
        description="The storage repository's branch a run is committed to.",
    )
    readable_files: tuple[StrictStr, ...] = Field(
        default=("scores.json", "config.resolved.yaml", "cost.json", "explorer*.html", "readings"),
        description="The bundle's files and folders, as glob patterns, committed beside the archive, where a "
        "reader opens them.",
    )
    left_out: tuple[StrictStr, ...] = Field(
        default=("episode/checkout", "honest_cal/checkout"),
        description="Paths in the bundle, as glob patterns, that a publish neither scans nor archives: each "
        "play's graded trees, with the caches their grading wrote, which git archive makes again from the "
        "commits the play's forge_history keeps.",
    )
    part_bytes: PositiveInt = Field(
        default=95_000_000,
        description="The largest file a publish commits; a larger archive is split into numbered parts, as "
        "GitHub refuses a file over 100 MB.",
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
            r"(?i)authorization\\*[\"']?\s*[:=,]\s*\[?\s*b?\\*[\"']?[A-Za-z][\w-]*\s+"
            r"(?P<credential>[A-Za-z0-9._~+/=-]{16,})",
        ),
        description="An Authorization header with a scheme and a credential (its group of that name), "
        "written as a header, a mapping or a pair, quoted, or escaped as JSON within JSON; refused unless "
        "in-world.",
    )
    in_world_token_pattern: Pattern[str] = Field(
        default=re.compile(r"(?:mi_)?[A-Za-z0-9_-]{43}"),
        description="The form of the company's credentials, each minted for one run as 32 URL-safe random "
        "bytes, which an Authorization header may carry and the run keeps; matched against the whole of one, "
        "less a full stop that ends a sentence.",
    )
    environment_name_pattern: Pattern[str] = Field(
        default=re.compile(
            r"(?i)(?:^|_)(?:key|apikey|token|pat|secret|password|passwd|credentials?|auth)$|(?:^|_)(?:secret|password)_",
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
    def directory(self) -> str:
        """Where it is committed: under its config's name, less a machine's own suffix."""
        return f"runs/{self.date}/{self.config.removesuffix(MACHINE_SUFFIX)}/{self.run_id}"


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
    source: the values of those with secret names, and the password of each URL, are searched for. A link's
    target is read as its line, as the archive keeps the link. Anything the scan cannot read is refused, and
    what the publish leaves out is not read.
    """
    secrets = (
        value if settings.environment_name_pattern.search(name) else urlsplit(value).password
        for name, value in environment
    )
    long = {value for value in secrets if value and len(value) >= settings.environment_value_min_length}
    values = long | {json.dumps(value)[1:-1] for value in long}  # as a JSON record holds it too
    findings: list[str] = []
    for path in sorted(bundle.rglob("*")):
        name = path.relative_to(bundle).as_posix()
        if _is_left_out(name, settings):
            continue
        try:
            readings = (
                [(name, [path.readlink().as_posix()])] if path.is_symlink() else _read_lines(path, name)
            )
            for where, lines in readings:
                findings += [
                    f"{where}:{number}: {what}"
                    for number, line in enumerate(lines, 1)
                    if (what := _find_secret(line, settings, values))
                ]
        except Exception as error:  # noqa: BLE001 - what the scan cannot read is refused, whatever the reason
            findings.append(f"{name}: unreadable ({type(error).__name__}: {error})")
    return findings


def _is_left_out(name: str, settings: PublishSettings) -> bool:
    inside = PurePosixPath(name)
    return any(
        fnmatch.fnmatch(str(path), pattern)
        for path in (inside, *inside.parents)
        for pattern in settings.left_out
    )


def _read_lines(path: Path, name: str) -> Iterator[tuple[str, Iterable[str]]]:
    """The lines of ``path``: a file's own, and those of each member, object or row it holds."""
    if path.is_dir() and (path / "HEAD").is_file() and (path / "objects").is_dir():
        yield from _store_lines(name, DiskObjectStore(str(path / "objects")))
    if not path.is_file():
        return
    with path.open(encoding="utf-8", errors="replace") as file:
        yield name, file
    with path.open("rb") as file:
        header = file.readline(64)
        if GIT_BUNDLE.fullmatch(header):
            file.seek(0)
            read_bundle(file).store_objects(store := MemoryObjectStore())
            yield from _store_lines(name, store)
    if header.startswith(b"SQLite format 3\x00"):
        read_only = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(read_only, uri=True)) as database:
            yield f"{name}!rows", database.iterdump()  # every row, whole, however SQLite split it on disk
    if zipfile.is_zipfile(path):
        yield from _zip_lines(path, name)


def _zip_lines(path: Path, name: str) -> Iterator[tuple[str, Iterable[str]]]:
    with zipfile.ZipFile(path) as archive:
        for member in (member for member in archive.infolist() if not member.is_dir()):
            with archive.open(member) as raw:
                yield f"{name}!{member.filename}", io.TextIOWrapper(raw, encoding="utf-8", errors="replace")


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
        if not settings.in_world_token_pattern.fullmatch(header["credential"].removesuffix(".")):
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
    """Commit ``bundle`` under its label in one commit, unless its folder is there already.

    A folder of another run under the same label is refused, naming that run.
    """
    if settings.repository is None:
        raise PublishError("the config sets no publish.repository")
    link = f"{settings.repository.removesuffix('.git')}/tree/{settings.branch}/{label.directory}"
    with tempfile.TemporaryDirectory() as work:
        clone = Path(work) / "storage"
        options = ("--depth=1", "--sparse", "--filter=blob:none", "--branch", settings.branch)
        _run(settings, "git", "clone", *options, settings.repository, clone)
        if _run(settings, "git", "-C", clone, "ls-tree", "HEAD", label.directory):
            filed = json.loads(
                _run(settings, "git", "-C", clone, "show", f"HEAD:{label.directory}/scores.json"),
            )
            if filed.get("run_name") != json.loads((bundle / "scores.json").read_text()).get("run_name"):
                raise PublishError(f"{label.directory} holds another run, {filed.get('run_name')}")
            return Published(link, already=True)
        target = clone / label.directory
        target.mkdir(parents=True)
        for source in (path for pattern in settings.readable_files for path in sorted(bundle.glob(pattern))):
            files = sorted(source.rglob("*")) if source.is_dir() and not source.is_symlink() else [source]
            for file in (file for file in files if file.lstat().st_size <= settings.part_bytes):
                (target / file.relative_to(bundle)).parent.mkdir(parents=True, exist_ok=True)
                if not file.is_dir() or file.is_symlink():
                    shutil.copy2(file, target / file.relative_to(bundle), follow_symlinks=False)
        (target / "bundle.txt").write_text(_write_archive(bundle, target, settings))
        _run(settings, "git", "-C", clone, "add", "--sparse", label.directory)
        _run(settings, "git", "-C", clone, "commit", "-q", "-m", f"Publish run {label.directory}")
        _run(settings, "git", "-C", clone, "push", "-q", "origin", f"HEAD:{settings.branch}")
    return Published(link, already=False)


def _write_archive(bundle: Path, target: Path, settings: PublishSettings) -> str:
    """Write ``bundle`` in ``target`` as one archive, or in numbered parts; what ``bundle.txt`` says."""
    archive = target / ARCHIVE

    def keep(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        return None if _is_left_out(member.name.partition("/")[2], settings) else member

    with lzma.open(archive, "wb", preset=XZ_PRESET) as packed, tarfile.open(fileobj=packed, mode="w") as tar:
        tar.add(bundle, arcname=bundle.name, filter=keep)
    size, digest = archive.stat().st_size, hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    lines = [f"archive: {ARCHIVE}, {size} bytes, sha256 {digest}"]
    if size > settings.part_bytes:
        with archive.open("rb") as whole:
            parts = [
                target / f"{ARCHIVE}.{index:03}" for index in range(math.ceil(size / settings.part_bytes))
            ]
            for part in parts:
                part.write_bytes(whole.read(settings.part_bytes))
        archive.unlink()
        lines.append(
            f"parts: {' '.join(part.name for part in parts)}; join: cat {ARCHIVE}.[0-9]* > {ARCHIVE}",
        )
    lines.append(f"unpack: tar -xJf {ARCHIVE}, into logs/<run slug>/ of a loc-arena checkout")
    lines.append(f"left out, rebuilt from what is kept: {', '.join(settings.left_out)}")
    return "".join(f"{line}\n" for line in lines)


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
        raise PublishError(f"{' '.join(error.cmd[:5])} failed: {error.stderr.strip()}") from error
    except subprocess.TimeoutExpired as error:
        raise PublishError(
            f"{' '.join(error.cmd[:5])} took longer than {settings.command_seconds}s",
        ) from error
    return done.stdout
