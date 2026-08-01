"""The prerequisite DAG and its routing rule. DESIGN.md §9, §10.

Seeded where an official taxonomy exists, learned from diagnosis
`prerequisite_gaps` where it does not. The routing rule: **>=3 failures at L4+
stops the tag and serves its highest-confidence parents.** Repeatedly failing
an item even with the full solution path in hand is not a signal to try
another item of the same kind.

The provenance gate is enforced here rather than at the call site, because it
is the rule most likely to be quietly bypassed under deadline pressure:
**unreviewed model labels may serve practice but may not reinforce
prerequisite edges** (DESIGN.md §10, principle 7). Bad labels feed the DAG,
the DAG drives routing, and a confidently wrong study plan is the worst
failure mode because it is invisible.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

# Routing trigger: this many failures at or above this hint level.
FAILURE_THRESHOLD = 3
DEEP_HINT_LEVEL = 4

# Label sources permitted to create or strengthen an edge.
EDGE_ELIGIBLE_SOURCES = frozenset({"official", "human"})

# Confidence a single learned observation contributes, and the ceiling learned
# edges may reach. Learned edges never reach seed confidence (1.0) -- an
# inferred prerequisite is not the same claim as a published one.
LEARNED_INCREMENT = 0.15
LEARNED_CEILING = 0.85


@dataclass(frozen=True)
class Edge:
    parent_slug: str
    child_slug: str
    confidence: float
    source: str
    n_observations: int = 0


class ProvenanceError(ValueError):
    """Raised when an ineligible label source tries to touch the DAG."""


def should_route_to_prerequisites(deep_failures: int) -> bool:
    """Whether to stop serving a tag and serve its parents instead."""
    return deep_failures >= FAILURE_THRESHOLD


def count_deep_failures(attempts: list[dict]) -> int:
    """Failures at L4+ -- the pattern that means the gap is upstream.

    A miss at L0 is ordinary. A miss with the full scaffold in hand means the
    item was never the problem.
    """
    return sum(
        1
        for a in attempts
        if not a.get("correct") and (a.get("min_hint_level") or 0) >= DEEP_HINT_LEVEL
    )


def reinforce(
    existing: Edge | None,
    parent_slug: str,
    child_slug: str,
    label_source: str,
    reviewed: bool,
) -> Edge:
    """Strengthen or create a learned edge, enforcing the provenance gate."""
    if label_source not in EDGE_ELIGIBLE_SOURCES and not reviewed:
        raise ProvenanceError(
            f"label_source={label_source!r} unreviewed may not reinforce the DAG "
            f"({parent_slug} -> {child_slug}); it may still serve practice"
        )
    if existing is not None and existing.source == "seed":
        # A published taxonomy outranks anything inferred from attempt data.
        return existing

    n = (existing.n_observations if existing else 0) + 1
    confidence = min(LEARNED_CEILING, n * LEARNED_INCREMENT)
    return Edge(parent_slug, child_slug, confidence, "learned", n)


def adjacency(edges: list[Edge]) -> dict[str, list[tuple[str, float]]]:
    """child -> [(parent, confidence)], the shape the allocator consumes."""
    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for edge in edges:
        out[edge.child_slug].append((edge.parent_slug, edge.confidence))
    for parents in out.values():
        parents.sort(key=lambda pc: pc[1], reverse=True)
    return dict(out)


def has_cycle(edges: list[Edge]) -> list[str] | None:
    """Return a cycle if the edge set is not acyclic.

    Learned edges can introduce cycles -- two tags can each look like the
    other's prerequisite across different sessions. Routing would then loop, so
    load-time validation checks this and the caller drops the weakest edge.
    """
    graph: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        graph[edge.parent_slug].append(edge.child_slug)

    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = defaultdict(int)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for nxt in graph.get(node, []):
            if color[nxt] == GREY:
                return stack[stack.index(nxt) :] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    for node in list(graph):
        if color[node] == WHITE:
            cycle = visit(node)
            if cycle:
                return cycle
    return None
