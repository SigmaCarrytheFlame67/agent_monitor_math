"""Incremental cycle checks for the Analyze dependency graphs.

Edges are added one at a time and an edge that would close a cycle is
refused.  Reachability is kept as bitsets (Python integers), so each check is
one bit test and each accepted edge updates only the nodes that reach its
source: a dense graph of a few hundred statements costs milliseconds, where a
depth-first search per edge costs minutes.
"""
from __future__ import annotations

from typing import Hashable


class Reachability:
    """Reachability between the nodes of a growing acyclic graph."""

    def __init__(self) -> None:
        self._index: dict[Hashable, int] = {}
        self._reach: list[int] = []      # bit j of _reach[i]: node j is reachable from node i
        self._reached_by: list[int] = []  # bit i of _reached_by[j]: node j is reachable from node i

    def _bit(self, node: Hashable) -> int:
        index = self._index.get(node)
        if index is None:
            index = len(self._reach)
            self._index[node] = index
            self._reach.append(1 << index)
            self._reached_by.append(1 << index)
        return index

    def reaches(self, start: Hashable, target: Hashable) -> bool:
        """Whether ``target`` is reachable from ``start`` (a node reaches itself)."""
        if start == target:
            return True
        first, second = self._index.get(start), self._index.get(target)
        if first is None or second is None:
            return False
        return bool(self._reach[first] >> second & 1)

    def add(self, source: Hashable, target: Hashable) -> bool:
        """Add ``source -> target`` unless it would close a cycle; returns
        whether the edge was added."""
        if self.reaches(target, source):
            return False
        first, second = self._bit(source), self._bit(target)
        gained = self._reach[second]
        sources = self._reached_by[first]
        if self._reach[first] & gained == gained:
            return True  # implied by edges already added
        position = sources
        while position:
            low = position & -position
            index = low.bit_length() - 1
            self._reach[index] |= gained
            position ^= low
        position = gained
        while position:
            low = position & -position
            index = low.bit_length() - 1
            self._reached_by[index] |= sources
            position ^= low
        return True
