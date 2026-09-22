"""A radix (prefix) cache for shared prompt prefixes.

Many requests share a long common prefix (a system prompt, a few-shot preamble). The prefix cache stores KV
state keyed by token-sequence prefixes in a radix tree, so a new request reuses the longest cached prefix and
only computes the novel suffix. It reports the matched prefix length and reference counts so blocks are freed
only when no sequence still needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class _Node:
    token: int
    depth: int
    refcount: int = 0
    cached: bool = False
    children: dict[int, _Node] = field(default_factory=dict)


@dataclass(frozen=True)
class PrefixMatch:
    """The result of a lookup: how many leading tokens were already cached."""

    matched_len: int
    novel_len: int

    @property
    def total_len(self) -> int:
        """The full sequence length that was looked up."""
        return self.matched_len + self.novel_len


class PrefixCache:
    """A radix tree over token sequences that tracks cached prefixes and their reference counts."""

    def __init__(self) -> None:
        """Start with an empty root."""
        self._root = _Node(token=-1, depth=0, cached=True)
        self._cached_tokens = 0

    @property
    def cached_tokens(self) -> int:
        """The number of distinct cached prefix tokens (a proxy for prefix-cache memory)."""
        return self._cached_tokens

    def insert(self, tokens: tuple[int, ...]) -> PrefixMatch:
        """Cache ``tokens`` as a prefix path, returning how much was already present vs newly added."""
        node = self._root
        matched = 0
        matching = True
        for depth, token in enumerate(tokens, start=1):
            child = node.children.get(token)
            if child is None:
                child = _Node(token=token, depth=depth)
                node.children[token] = child
            if matching and child.cached:
                matched += 1
            else:
                matching = False
                if not child.cached:
                    child.cached = True
                    self._cached_tokens += 1
            node = child
        node.refcount += 1
        return PrefixMatch(matched_len=matched, novel_len=len(tokens) - matched)

    def lookup(self, tokens: tuple[int, ...]) -> PrefixMatch:
        """Return the longest cached prefix of ``tokens`` without inserting."""
        node = self._root
        matched = 0
        for token in tokens:
            child = node.children.get(token)
            if child is None or not child.cached:
                break
            matched += 1
            node = child
        return PrefixMatch(matched_len=matched, novel_len=len(tokens) - matched)

    def release(self, tokens: tuple[int, ...]) -> None:
        """Decrement the reference count on the terminal node of ``tokens``."""
        node = self._root
        for token in tokens:
            child = node.children.get(token)
            if child is None:
                return
            node = child
        node.refcount = max(0, node.refcount - 1)

    def evict_unreferenced(self) -> int:
        """Drop cached leaf nodes with a zero reference count; return how many token-nodes were freed."""
        freed = self._prune(self._root)
        self._cached_tokens -= freed
        return freed

    def _prune(self, node: _Node) -> int:
        freed = 0
        for token, child in list(node.children.items()):
            freed += self._prune(child)
            if not child.children and child.refcount == 0 and child.cached and child is not self._root:
                del node.children[token]
                freed += 1
        return freed
