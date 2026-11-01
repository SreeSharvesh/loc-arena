"""Each agent's checkout as it was after each of its actions, in a git object store only the episode writes.

A trajectory has to say which code each action ran on, and an agent's checkout changes with every edit and
command, is removed when its play ends, and its agent can rewrite or empty it at any time. So after each of an
agent's native tool calls the episode reads that agent's whole checkout and stores it as a git tree in its
play's ``checkout_history``, a bare repository the sandboxes never mount: each capture a commit on
``refs/agents/<agent id>``, whose parent is the agent's capture before it. The sealed log alone gets a
``checkout`` event naming the tree, so the action before it ran on the tree of the agent's previous capture
and left this one; the monitors never see it. Git stores each file once, by its content, so a capture adds
only what changed since the last.

The checkout is read by the episode itself (dulwich), never by running git on it: its configuration and hooks
are the agent's. Nothing is followed out of it: a link is stored as a link, a file that is neither a link nor
a regular file is left out, and ``history`` settings bound what one capture reads (``HistorySettings``). A
file over ``max_file_bytes`` or one the episode cannot read is stored as a line saying so, and named in the
event. A repo's tree in a capture of its clean clone is git's own tree of that clone's ``HEAD``, so a
capture can be matched to the commits the forge's records name.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dulwich.objects import Blob, Commit, ObjectID, Tree
from dulwich.refs import Ref
from dulwich.repo import Repo
from pydantic import JsonValue

from loc_arena.settings import HistorySettings

HISTORY_DIRECTORY = "checkout_history"  # in a play's directory: the bare repository of every capture
AGENT_REFS = "refs/agents"  # each agent's latest capture, as a ref under this
EMAIL_DOMAIN = "meridian.example"  # a capture is authored by its agent, <agent id>@ this domain
_FILE, _EXECUTABLE, _LINK, _DIRECTORY = 0o100644, 0o100755, 0o120000, 0o040000  # git's tree entry modes


@dataclass
class _Reading:
    """What one capture has read so far, against the settings' bounds."""

    entries: int = 0
    truncated: bool = False
    omitted: list[str] = field(default_factory=list)


class CheckoutHistory:
    """The history of each agent's checkout, in the bare repository at ``store``."""

    def __init__(
        self,
        store: Path,
        checkouts: Mapping[str, Path],
        settings: HistorySettings,
        clock: Callable[[], float],
    ) -> None:
        """Keep in ``store`` the history of each checkout of ``checkouts`` by agent id, dated by ``clock``."""
        self._repo = Repo(str(store)) if store.exists() else Repo.init_bare(str(store), mkdir=True)
        self._checkouts = dict(checkouts)
        self._settings = settings
        self._clock = clock
        self._read: dict[str, tuple[tuple[int, int, int, int], ObjectID]] = {}  # by path: its stat, its blob
        self._lock = threading.Lock()  # agents may act at once; the store takes one capture at a time

    def capture(self, agent: str, label: str) -> dict[str, JsonValue] | None:
        """Store ``agent``'s checkout as it is now, as a commit saying ``label``; none if it has no checkout.

        Returns the ``checkout`` event's payload: the checkout's tree, each repo's, and what was left out.
        """
        directory = self._checkouts.get(agent)
        if directory is None:
            return None
        with self._lock:
            reading = _Reading()
            tree = self._store_tree(directory, directory, reading, depth=0)
            self._commit(agent, tree, label)
            repos: dict[str, JsonValue] = {
                name.decode(): entry_id.decode()
                for name, mode, entry_id in tree.iteritems()
                if stat.S_ISDIR(mode)
            }
        omitted: list[JsonValue] = list(reading.omitted)
        return {"tree": tree.id.decode(), "repos": repos, "omitted": omitted, "truncated": reading.truncated}

    def _store_tree(self, root: Path, directory: Path, reading: _Reading, depth: int) -> Tree:
        tree = Tree()
        if depth > self._settings.max_depth:
            reading.truncated = True
            return tree
        with os.scandir(directory) as scanned:
            entries = sorted(scanned, key=lambda entry: entry.name)
        for entry in entries:
            if entry.name in self._settings.skipped_names:
                continue
            if reading.entries >= self._settings.max_entries:
                reading.truncated = True
                break
            reading.entries += 1
            path = Path(entry.path)
            status = entry.stat(follow_symlinks=False)
            name = os.fsencode(entry.name)
            if stat.S_ISLNK(status.st_mode):
                tree.add(name, _LINK, self._store(Blob.from_string(os.fsencode(os.readlink(path)))))
            elif stat.S_ISDIR(status.st_mode):
                subtree = self._store_tree(root, path, reading, depth + 1)
                if len(subtree):  # git keeps no empty directory
                    tree.add(name, _DIRECTORY, subtree.id)
            elif stat.S_ISREG(status.st_mode):
                mode = _EXECUTABLE if status.st_mode & 0o111 else _FILE
                tree.add(name, mode, self._store_file(root, path, status, reading))
        self._store(tree)
        return tree

    def _store_file(self, root: Path, path: Path, status: os.stat_result, reading: _Reading) -> ObjectID:
        """The blob of ``path``: unchanged since it was last read, the blob read then.

        Its change time is part of what says it is unchanged: a write sets it, and nothing in a sandbox can
        set it back.
        """
        key = (status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
        known = self._read.get(str(path))
        if known is not None and known[0] == key:
            return known[1]
        limit = self._settings.max_file_bytes
        content = self._read_regular_file(path, limit)
        if content is None or len(content) > limit:
            reading.omitted.append(str(path.relative_to(root)))
            why = "unreadable" if content is None else f"over the {limit}-byte limit"
            content = f"left out of the checkout history: {status.st_size} bytes, {why}\n".encode()
        blob = self._store(Blob.from_string(content))
        self._read[str(path)] = (key, blob)
        return blob

    @staticmethod
    def _read_regular_file(path: Path, limit: int) -> bytes | None:
        """Up to ``limit`` bytes and one more of ``path``, if it is still a regular file; none if unreadable.

        It is opened without following a link and without waiting, so a file swapped for a link or a pipe
        since it was listed is never followed or waited on.
        """
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        with os.fdopen(descriptor, "rb") as opened:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return None
            return opened.read(limit + 1)

    def _store(self, obj: Blob | Tree | Commit) -> ObjectID:
        if obj.id not in self._repo.object_store:
            self._repo.object_store.add_object(obj)
        return obj.id

    def _commit(self, agent: str, tree: Tree, label: str) -> None:
        """Add a commit of ``tree`` to ``agent``'s captures, unless its last capture holds that tree."""
        ref = Ref(f"{AGENT_REFS}/{agent}".encode())
        parent = self._repo.refs[ref] if ref in self._repo.refs else None
        if parent is not None and self._repo[parent].tree == tree.id:  # ty: ignore[unresolved-attribute]
            return
        commit = Commit()
        commit.tree = tree.id
        commit.parents = [parent] if parent is not None else []
        commit.author = commit.committer = f"{agent} <{agent}@{EMAIL_DOMAIN}>".encode()
        commit.author_time = commit.commit_time = int(self._clock())
        commit.author_timezone = commit.commit_timezone = 0
        commit.message = f"{label}\n".encode()
        self._store(commit)
        self._repo.refs[ref] = commit.id
