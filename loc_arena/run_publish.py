"""Publish a graded run to the private storage repository, so its records outlive the machine that played it.

One commit per run, under ``runs/<date>/<config>/<run id>/``: the run's readable files, the whole bundle as
``bundle.tar.xz`` (in numbered parts when it is larger than ``publish.part_bytes``), and ``bundle.txt``, which
gives its size, its SHA-256 and how to join and unpack it. The archive leaves out each play's checkout that
``rebuild_checkout`` makes again byte for byte from the play's forge history. Nothing is published until
``find_secrets`` finds nothing real in what is published, and a run already filed is not written again.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import lzma
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import zipfile_zstd  # noqa: F401 - reads the zstd members of Inspect's .eval zips, as inspect_ai itself does
from dulwich.archive import tar_stream
from dulwich.bundle import read_bundle
from dulwich.object_store import BaseObjectStore, DiskObjectStore, MemoryObjectStore
from dulwich.objects import Commit, Tree

from loc_arena.merged_main import (
    BUNDLE_SUFFIX,
    CHECKOUT,
    EPISODE_PHASE,
    HISTORY,
    MAIN_BRANCH,
    TWIN_PHASE,
    write_trees,
)
from loc_arena.publish_settings import PublishSettings

GIT_BUNDLE = re.compile(rb"# v[23] git bundle\n")  # the first line of a git bundle
LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")  # a config name or a run id: one path segment, no traversal
MACHINE_SUFFIX = ".local"  # a machine's own config, configs/*.local.yaml, which git ignores
ARCHIVE = "bundle.tar.xz"
CACHES = frozenset({"__pycache__", ".pytest_cache"})  # what grading writes in a checkout: no trace
XZ_PRESET = 9 | lzma.PRESET_EXTREME  # each model call repeats its conversation: xz's large window finds it


class PublishError(RuntimeError):
    """A run that could not be published; the message says why."""


class SecretFoundError(PublishError):
    """A bundle whose scan found what looks like a real secret; ``findings`` says where."""

    def __init__(self, findings: list[str]) -> None:
        """Say how many lines ``findings`` names, and keep them."""
        super().__init__(f"{len(findings)} lines hold what looks like a real secret")
        self.findings = findings


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


def rebuild_checkout(play: Path, into: Path) -> None:
    """Write in ``into`` each repo's ``main`` from ``play``'s forge history, unpacked as the run did."""
    archives = {}
    for history in sorted((play / HISTORY).glob(f"*{BUNDLE_SUFFIX}")):
        with history.open("rb") as file:
            bundle = read_bundle(file)
            bundle.store_objects(store := MemoryObjectStore())
            bundle.close()
        main = bundle.references.get(MAIN_BRANCH.encode())
        commit = store[main] if main is not None else None
        tree = store[commit.tree] if isinstance(commit, Commit) else None
        if isinstance(tree, Tree):
            archives[history.stem] = b"".join(tar_stream(store, tree, mtime=0))
    write_trees(into, archives)


def find_rebuilt_checkouts(bundle: Path) -> tuple[str, ...]:
    """Each play's checkout in ``bundle`` that ``rebuild_checkout`` makes again byte for byte, as its path."""
    rebuilt = []
    for play in (bundle / EPISODE_PHASE, bundle / TWIN_PHASE):
        with tempfile.TemporaryDirectory() as work:
            try:
                rebuild_checkout(play, Path(work) / CHECKOUT)
            except Exception:  # noqa: BLE001, S112 - a checkout its history cannot rebuild is kept, whatever the reason
                continue
            if (play / CHECKOUT).is_dir() and _list_files(play / CHECKOUT) == _list_files(
                Path(work) / CHECKOUT,
            ):
                rebuilt.append(f"{play.name}/{CHECKOUT}")
    return tuple(rebuilt)


def _list_files(root: Path) -> dict[str, tuple[bytes, int]]:
    """Each file and link under ``root`` but the caches: its content or target, and its execute bit."""
    return {
        path.relative_to(root).as_posix(): (
            os.readlink(path).encode() if path.is_symlink() else path.read_bytes(),
            path.lstat().st_mode & 0o100,
        )
        for path in root.rglob("*")
        if (path.is_symlink() or path.is_file()) and not CACHES & set(path.relative_to(root).parts)
    }


def find_secrets(
    bundle: Path,
    settings: PublishSettings,
    environment: Iterable[tuple[str, str]],
    left_out: tuple[str, ...] = (),
) -> list[str]:
    """Each line of ``bundle`` that holds a real secret, as ``where:line: what``.

    Every file is read as text; a zip's members, and every object of a git store or a git bundle, are read
    too, as ``file!member``. ``environment`` holds the machine's variables, as name and value, from every
    source: the values of those with secret names, and the password of each URL, are searched for. A link's
    target is read as its line, as the archive keeps the link. Anything the scan cannot read is refused, and
    what the publish leaves out (``left_out``, paths in ``bundle``) is not read.
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
        if _is_left_out(name, left_out):
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


def _is_left_out(name: str, left_out: tuple[str, ...]) -> bool:
    return any(name == path or name.startswith(f"{path}/") for path in left_out)


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
            bundle = read_bundle(file)
            bundle.store_objects(store := MemoryObjectStore())
            bundle.close()
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


def publish_run(
    bundle: Path,
    label: Label,
    settings: PublishSettings,
    environment: Iterable[tuple[str, str]],
) -> Published:
    """Scan ``bundle``, then commit it under its label in one commit, unless its folder is there already.

    ``environment`` is the machine's variables, which the scan searches for. A folder of another run under the
    same label is refused, naming that run, and so is a file that changed after the scan began.
    """
    if settings.repository is None:
        raise PublishError("the config sets no publish.repository")
    left_out = find_rebuilt_checkouts(bundle) if settings.leave_out_rebuilt_checkouts else ()
    scanned_at = int(time.time())
    if findings := find_secrets(bundle, settings, environment, left_out):
        raise SecretFoundError(findings)
    base = re.sub(r"^git@([^:]+):", r"https://\1/", settings.repository)  # an SSH URL's page
    link = f"{re.sub(r'//[^/@]+@', '//', base).removesuffix('.git')}/tree/{settings.branch}/{label.directory}"
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
                _refuse_if_changed(file.lstat().st_mtime, scanned_at, file)
                (target / file.relative_to(bundle)).parent.mkdir(parents=True, exist_ok=True)
                if not file.is_dir() or file.is_symlink():
                    shutil.copy2(file, target / file.relative_to(bundle), follow_symlinks=False)
        (target / "bundle.txt").write_text(_write_archive(bundle, target, settings, left_out, scanned_at))
        _run(settings, "git", "-C", clone, "add", "--sparse", label.directory)
        _run(settings, "git", "-C", clone, "commit", "-q", "-m", f"Publish run {label.directory}")
        _run(settings, "git", "-C", clone, "push", "-q", "origin", f"HEAD:{settings.branch}")
    return Published(link, already=False)


def _refuse_if_changed(mtime: float, scanned_at: int, path: Path | str) -> None:
    if mtime >= scanned_at:
        raise PublishError(f"{path} changed after the scan began; publish the run once it is finished")


def _write_archive(
    bundle: Path,
    target: Path,
    settings: PublishSettings,
    left_out: tuple[str, ...],
    scanned_at: int,
) -> str:
    """Write ``bundle`` in ``target`` as one archive, or in numbered parts; what ``bundle.txt`` says."""
    archive = target / ARCHIVE

    def keep(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if _is_left_out(member.name.partition("/")[2], left_out):
            return None
        _refuse_if_changed(member.mtime, scanned_at, member.name)
        return member

    with lzma.open(archive, "wb", preset=XZ_PRESET) as packed, tarfile.open(fileobj=packed, mode="w") as tar:
        tar.add(bundle, arcname=bundle.name, filter=keep)
    size, digest = archive.stat().st_size, hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
    lines = [f"archive: {ARCHIVE}, {size} bytes, sha256 {digest}"]
    if size > settings.part_bytes:
        with archive.open("rb") as whole:
            count = math.ceil(size / settings.part_bytes)
            parts = [target / f"{ARCHIVE}.{index:03}" for index in range(count)]
            for part in parts:
                part.write_bytes(whole.read(settings.part_bytes))
        archive.unlink()
        names = " ".join(part.name for part in parts)
        lines.append(f"parts: {names}; join: cat {ARCHIVE}.[0-9]* > {ARCHIVE}")
    lines.append(f"unpack: tar -xJf {ARCHIVE}, into logs/<run slug>/ of a loc-arena checkout")
    rebuilt = ", ".join(left_out) or "nothing"
    lines.append(f"left out, rebuilt byte for byte by loc_arena.run_publish.rebuild_checkout: {rebuilt}")
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
