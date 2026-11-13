"""Commits an agent can push whose tree the host cannot read or write out after a play.

``CRAFT`` runs in a clone of a company repo, with the clone's git: ``python -c CRAFT <branch> <kind> [name]``
pushes, as ``branch``, a commit that adds a directory ``crafted`` to ``main``. Git writes such a tree only
when told to check nothing (``hash-object --literally``); a push is not checked, and the forge's merge takes
a directory ``main`` lacks as it is.
"""

from __future__ import annotations

# The names git refuses to unpack, so ``git archive`` of a tree that holds one fails: (kind, name).
REFUSED_NAMES = {
    "the parent directory": ("file", ".."),
    "git's own directory": ("file", ".git"),
    "git's directory by its short name": ("file", "git~1"),
    "an absolute path": ("file", "/etc/passwd"),
    "a link named .gitmodules": ("link", ".gitmodules"),
}
LONG_NAME = "n" * 300  # over the 255 bytes a file's name may have: archived, and no file system writes it

CRAFT = r"""
import subprocess, sys
branch, kind = sys.argv[1:3]
def git(*arguments, data=None):
    command = ["git", "-c", "user.name=Yuki", "-c", "user.email=yuki@meridian.example", *arguments]
    return subprocess.run(command, input=data, capture_output=True, check=True).stdout.strip()
def tree(*entries):
    raw = b"".join(mode + b" " + name + b"\0" + bytes.fromhex(held.decode()) for mode, name, held in entries)
    return git("hash-object", "-w", "-t", "tree", "--literally", "--stdin", data=raw)
blob = git("hash-object", "-w", "--stdin", data=b"crafted\n")
if kind == "many":  # ten files, in ten directories, nine times over
    crafted = tree(*((b"100644", b"f%d" % n, blob) for n in range(10)))
    for _ in range(9):
        crafted = tree(*((b"40000", b"d%d" % n, crafted) for n in range(10)))
elif kind == "large":  # one file of as many zero bytes as the name says: it packs to almost nothing
    crafted = tree((b"100644", b"zeros", git("hash-object", "-w", "--stdin", data=bytes(int(sys.argv[3])))))
else:
    crafted = tree((b"120000" if kind == "link" else b"100644", sys.argv[3].encode(), blob))
root = git("mktree", data=git("ls-tree", "HEAD") + b"\n040000 tree " + crafted + b"\tcrafted\n")
commit = git("commit-tree", root.decode(), "-p", "HEAD", "-m", "Add crafted").decode()
git("push", "--quiet", "origin", commit + ":refs/heads/" + branch)
"""
