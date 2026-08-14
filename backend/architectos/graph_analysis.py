"""Dependency-free graph analytics over the memory graph.

Pure-Python implementations (stdlib only, matching the project's zero-runtime-deps
policy) of the primitives that power "themes" and multi-hop retrieval:

- ``compute_degrees``      — connectivity per node (god-node sizing in the UI).
- ``detect_communities``   — deterministic synchronous label propagation, so the
  graph splits into subsystems/themes without igraph/leidenalg/networkx.
- ``personalized_pagerank`` — seeded random-walk importance for multi-hop
  retrieval (Phase 2); kept here so the graph math lives in one place.

Everything is deterministic: given the same nodes/edges the output is stable
(sorted iteration + deterministic tie-breaks), which keeps colors, tests, and
benchmarks reproducible.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

EdgePair = tuple[str, str]


def _adjacency(node_ids: Iterable[str], edges: Iterable[EdgePair]) -> tuple[list[str], dict[str, set[str]]]:
    """Build an undirected adjacency map restricted to ``node_ids``.

    Returns the stable, de-duplicated node order and the neighbor sets. Self-loops
    and edges touching unknown nodes are ignored.
    """
    nodes = list(dict.fromkeys(str(nid) for nid in node_ids if str(nid)))
    id_set = set(nodes)
    adjacency: dict[str, set[str]] = {nid: set() for nid in nodes}
    for source, target in edges:
        s, t = str(source), str(target)
        if s == t or s not in id_set or t not in id_set:
            continue
        adjacency[s].add(t)
        adjacency[t].add(s)
    return nodes, adjacency


def compute_degrees(node_ids: Iterable[str], edges: Iterable[EdgePair]) -> dict[str, int]:
    """Undirected degree per node (each qualifying edge adds 1 to both ends)."""
    nodes, adjacency = _adjacency(node_ids, edges)
    return {nid: len(adjacency[nid]) for nid in nodes}


def detect_communities(
    node_ids: Iterable[str],
    edges: Iterable[EdgePair],
    *,
    max_iter: int = 25,
) -> dict[str, int]:
    """Assign each node a community id via deterministic label propagation.

    Synchronous updates over a sorted node order with a deterministic tie-break
    (most frequent neighbor label, ties broken by smallest label) converge to a
    stable partition. Communities are renumbered ``0..k-1`` by size descending
    (ties by smallest member id) so the largest subsystem is always community 0 —
    which keeps UI colors stable across reloads. Isolated nodes each become their
    own singleton community.
    """
    nodes, adjacency = _adjacency(node_ids, edges)
    if not nodes:
        return {}

    # Seed: every node in its own community (index by position for compactness).
    label: dict[str, int] = {nid: index for index, nid in enumerate(nodes)}
    order = sorted(nodes)

    for _ in range(max_iter):
        changed = False
        for nid in order:
            neighbors = adjacency[nid]
            if not neighbors:
                continue
            counts: dict[int, int] = defaultdict(int)
            for neighbor in neighbors:
                counts[label[neighbor]] += 1
            top = max(counts.values())
            # Deterministic tie-break: smallest label among the most frequent.
            best = min(lbl for lbl, count in counts.items() if count == top)
            if label[nid] != best:
                label[nid] = best
                changed = True
        if not changed:
            break

    # Renumber by community size (desc), then by smallest member id (asc).
    members: dict[int, list[str]] = defaultdict(list)
    for nid in nodes:
        members[label[nid]].append(nid)
    ordered = sorted(members.items(), key=lambda kv: (-len(kv[1]), min(kv[1])))
    remap = {old: new for new, (old, _members) in enumerate(ordered)}
    return {nid: remap[label[nid]] for nid in nodes}


def personalized_pagerank(
    node_ids: Iterable[str],
    edges: Iterable[EdgePair],
    seeds: Iterable[str],
    *,
    damping: float = 0.85,
    max_iter: int = 60,
    tolerance: float = 1.0e-6,
) -> dict[str, float]:
    """Seeded PageRank over the undirected memory graph (multi-hop importance).

    The random walk restarts on ``seeds`` (the query's matched nodes), so mass
    concentrates on nodes reachable from the query even across several hops —
    the retrieval upgrade over a single 1-hop expansion. Returns a score per node
    (0 for everything when there are no valid seeds).
    """
    nodes, adjacency = _adjacency(node_ids, edges)
    if not nodes:
        return {}
    seed_list = [str(s) for s in seeds if str(s) in adjacency]
    if not seed_list:
        return {nid: 0.0 for nid in nodes}

    restart = 1.0 / len(seed_list)
    teleport: dict[str, float] = {nid: 0.0 for nid in nodes}
    for seed in seed_list:
        teleport[seed] += restart

    rank: dict[str, float] = dict(teleport)
    for _ in range(max_iter):
        nxt = {nid: (1.0 - damping) * teleport[nid] for nid in nodes}
        dangling = 0.0
        for nid in nodes:
            neighbors = adjacency[nid]
            if not neighbors:
                dangling += rank[nid]
                continue
            share = damping * rank[nid] / len(neighbors)
            for neighbor in neighbors:
                nxt[neighbor] += share
        if dangling:
            # Redistribute dangling mass back onto the seeds (personalized).
            for seed in seed_list:
                nxt[seed] += damping * dangling * restart
        delta = sum(abs(nxt[nid] - rank[nid]) for nid in nodes)
        rank = nxt
        if delta < tolerance:
            break
    return rank
