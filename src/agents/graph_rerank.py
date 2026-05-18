"""W80c — Hybrid graph + vector rerank helper.

PR 1 deliverable from W80c Stage 2. Builds an in-memory adjacency map
from the cross-function edges already persisted at
``graph:full:<schema>`` (Stage 1 diagnostic confirmed all 2,249 OFSERM
edges are there) and fuses cosine rank with edge-derived signals via
Reciprocal Rank Fusion (RRF, Cormack & Clarke 2009).

Stage 1 design notes (``docs/w80c_diagnostic.md``):

  * The full-graph blob is loaded ONCE per process per schema, cached
    in a module-level dict. A request-time lookup is then a dict hit
    (~zero Redis ops).
  * Expansion is bounded: only the top-N vector hits are used as seeds
    (default 3), avoiding the 100+ neighbor explosion from
    dimension-table-touching functions like
    ``CAP_CONSL_NON_REGULATORY_ENTITY_..._IDENTIFICATION``.
  * Edge strength is captured by ``matching_columns``: a 5-column
    overlap on ``FSI_NON_REG_CONSL_ENTITY_INVST`` is load-bearing data
    flow; a 0-column overlap on ``DIM_DATES`` is dimension passthrough.
    The reranker uses that count as the primary edge weight rather
    than pre-filtering dim-only edges (Q3 decision: keep edges in the
    index, suppress via weighting).
  * RRF was chosen over a linear cosine+graph combination because RRF
    needs no score normalization — cosine distance and the weighted
    graph score live in different units, and the score-scale drift
    between corpora would make a single λ fragile.

The module is wire-in-free in PR 1: ``main.py`` still doesn't call
``rerank_with_rrf``. PR 2 wires it at
``main.py:1173`` (immediately before ``ensure_anchor_in_search_results``)
gated on ``VARIABLE_TRACE`` / ``COLUMN_LOGIC``.

Cross-schema reachability (OFSMDM-writes → OFSERM-reads) is DEFERRED
per Q1: the loader scopes ``build_cross_function_graph`` to a single
schema, and the significant-investment canary that motivated W80c
lives entirely in OFSERM. A future canary that crosses schemas will
require a global rollup or a query-time merge of per-schema rollups —
out of scope here. See the deferral note in ``docs/w35_diagnostic.md``
Section 1.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Tuple

from src.logger import get_logger
from src.parsing.store import get_full_graph, get_function_graph

logger = get_logger(__name__, concern="app")


# A sentinel ``graph_rank`` for candidates with zero graph reach. Picked
# large enough that ``1 / (k + sentinel)`` is dominated by any real
# ranked contribution; the exact value doesn't matter as long as it
# dwarfs the keep_top + vector_hit count.
_NO_GRAPH_REACH_RANK = 1_000_000

_DEFAULT_WEIGHTS: Dict[str, float] = {
    # PR 2 retune attempt (2026-05-18, reverted same day): lifting α to
    # 3.0 was a no-op because RRF fuses INTEGER ranks, not raw scores —
    # uniformly scaling one weight preserves the sort order of
    # ``graph_score`` across candidates, so ``graph_rank`` integers stay
    # identical and the fused output is bit-for-bit unchanged. The real
    # mechanism that flooded the keep_top window was the 137-candidate
    # expansion from FCT_ENTITY_INFO-touching seeds; that's now bounded
    # by a per-seed cap in :func:`expand_one_hop` (default 20). Keeping
    # α=1.0 here as the PR 1 shipped default.
    "matching_columns": 1.0,
    "seed_reach": 0.5,
    "sub_process": 0.5,
    # ``process`` is intentionally 0 — too coarse alone (all canary
    # targets share ``batch=ABL_CAR_CSTM_V4``). Left exposed so PR 2
    # can experiment without an API change.
    "process": 0.0,
}

# Default per-seed cap for ``expand_one_hop``. The W80c PR 2 wire-in
# canary measured 137 expansion candidates from 3 seeds touching
# FCT_ENTITY_INFO / DIM_* tables; the resulting flood pushed strong-
# cosine top-1 hits with weak graph signals (T1 in the significant-
# investment canary) out of the keep_top=25 window. Capping each seed
# to its top-N neighbours BY ``len(matching_columns)`` descending (ties
# broken stably in edge-list order) bounds expansion at ~3*N pre-dedupe
# while keeping every load-bearing edge (T2 → T4 with 5 cols, T2 → T5
# with 3 cols, T3 → T2 with 2 cols, etc.) inside the cap. N=20 is the
# PR 2 starting point — tuning per future canary measurements.
_DEFAULT_PER_SEED_CAP = 20


# ---------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    """One cross-function edge as seen from a specific lookup target.

    ``direction='out'`` means the lookup target writes a table that
    ``to_function`` reads. ``direction='in'`` is the reverse —
    ``to_function`` writes, the lookup target reads. Recording both
    directions when building the index lets ``neighbors()`` return one
    flat list per function regardless of which side of the edge the
    caller is on.
    """

    to_function: str
    table: str
    matching_columns: FrozenSet[str]
    direction: str


@dataclass
class Candidate:
    """One rerank candidate with the raw features that produced its rank.

    Callers (tests, future audit tooling) read these fields to verify
    why a candidate ranked where it did. The linear-combined
    ``graph_score`` used for ranking is attached as a private attribute
    by ``score_candidate`` — it isn't a stable part of this shape.
    """

    function_name: str
    vector_rank: int
    vector_score: float
    matching_column_sum: int
    seed_reach_count: int
    same_sub_process_path: bool
    same_process: bool
    graph_rank: int = 0
    final_rank: int = 0


# ---------------------------------------------------------------------
# Process-level cache for the per-schema EdgeIndex
# ---------------------------------------------------------------------


_CACHE: Dict[str, "EdgeIndex"] = {}
_CACHE_LOCK = threading.Lock()


def _reset_cache_for_test() -> None:
    """Clear the process-level ``EdgeIndex`` cache.

    Test-only. Not part of the public API and not stable. Tests must
    call this in setup/teardown to keep cached indices from leaking
    between test cases (each test typically constructs a fresh fake
    Redis with a different edge set).
    """
    with _CACHE_LOCK:
        _CACHE.clear()


class EdgeIndex:
    """In-memory adjacency map covering ONE schema.

    A single ``{from_fn: [Edge, ...]}`` map with both ``out`` and
    ``in`` directions recorded so one lookup returns every edge
    touching a function. Construct via :meth:`for_schema`; direct
    instantiation is reserved for the build path.

    Lifetime is process-level: the same instance is returned for the
    same schema across the whole process. No staleness logic — if the
    loader rewrites ``graph:full:<schema>``, a process restart picks
    it up. That matches how the loader itself runs once per boot.
    """

    def __init__(self, schema: str) -> None:
        self.schema: str = (schema or "").upper()
        self._adj: Dict[str, List[Edge]] = {}

    # -- construction --------------------------------------------------

    @classmethod
    def for_schema(cls, redis_client: Any, schema: str) -> "EdgeIndex":
        """Return the cached ``EdgeIndex`` for *schema*, building on first call.

        Reads ``graph:full:<schema>``, walks ``CROSS_FUNCTION_TABLE_FLOW``
        edges only (intra-function edges are ignored), and stores both
        directions per edge. Returns an empty index — no exception,
        no warning — when the key is missing or ``redis_client`` is
        ``None``; Stage 1 lists that as the loader's clean-Redis case.
        """
        schema_key = (schema or "").upper()
        with _CACHE_LOCK:
            cached = _CACHE.get(schema_key)
            if cached is not None:
                return cached

        # Build outside the lock — the Redis fetch can be slow. Recheck
        # after to honor the first writer in a multi-thread race.
        instance = cls._build(redis_client, schema_key)
        with _CACHE_LOCK:
            existing = _CACHE.get(schema_key)
            if existing is not None:
                return existing
            _CACHE[schema_key] = instance
        return instance

    @classmethod
    def _build(cls, redis_client: Any, schema: str) -> "EdgeIndex":
        idx = cls(schema)
        t0 = time.perf_counter()
        if redis_client is None:
            logger.info(
                "EdgeIndex.for_schema(%s): redis_client is None — empty index",
                schema,
            )
            return idx

        try:
            graph = get_full_graph(redis_client, schema)
        except Exception as exc:  # store.get_full_graph already guards but be safe
            logger.warning(
                "EdgeIndex.for_schema(%s): graph:full load failed — %s",
                schema, exc,
            )
            return idx

        if graph is None:
            logger.info(
                "EdgeIndex.for_schema(%s): graph:full key missing — empty index",
                schema,
            )
            return idx

        edges = graph.get("edges") or []
        nodes = graph.get("nodes") or {}
        function_count = len(nodes) if isinstance(nodes, dict) else 0
        cross_count = 0
        for raw in edges:
            if not isinstance(raw, dict):
                continue
            if raw.get("type") != "CROSS_FUNCTION_TABLE_FLOW":
                continue
            from_fn = raw.get("from_function") or ""
            to_fn = raw.get("to_function") or ""
            if not from_fn or not to_fn:
                continue
            table = (raw.get("table") or "").upper()
            raw_cols = raw.get("matching_columns") or []
            matching = frozenset(c for c in raw_cols if c)

            from_key = from_fn.upper()
            to_key = to_fn.upper()
            idx._adj.setdefault(from_key, []).append(
                Edge(
                    to_function=to_fn,
                    table=table,
                    matching_columns=matching,
                    direction="out",
                )
            )
            idx._adj.setdefault(to_key, []).append(
                Edge(
                    to_function=from_fn,
                    table=table,
                    matching_columns=matching,
                    direction="in",
                )
            )
            cross_count += 1

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.info(
            "EdgeIndex built for schema=%s: %d functions, %d edges, %.3f ms",
            schema, function_count, cross_count, elapsed_ms,
        )
        return idx

    # -- lookups -------------------------------------------------------

    def neighbors(self, function_name: str) -> List[Edge]:
        """All 1-hop edges touching *function_name* (case-insensitive)."""
        if not function_name:
            return []
        return list(self._adj.get(function_name.upper(), ()))

    def edges_between(self, fn_a: str, fn_b: str) -> List[Edge]:
        """Edges directly connecting *fn_a* and *fn_b* in either direction.

        Diagnostic helper — the rerank path uses :meth:`neighbors`
        plus a seed-membership check, not this method.
        """
        if not fn_a or not fn_b:
            return []
        target = fn_b.upper()
        return [e for e in self.neighbors(fn_a) if e.to_function.upper() == target]


# ---------------------------------------------------------------------
# Expansion + scoring
# ---------------------------------------------------------------------


def expand_one_hop(
    seeds: List[str],
    edge_index: EdgeIndex,
    *,
    per_seed_cap: int = _DEFAULT_PER_SEED_CAP,
) -> List[str]:
    """Return the deduped 1-hop neighbor set of *seeds*, excluding seeds.

    Each seed's neighbor list is sorted by ``len(matching_columns)``
    descending (ties broken stably in original edge-list order — Python's
    ``sorted`` is stable, so the secondary key is implicit) and sliced
    to the first ``per_seed_cap`` entries before contributing to the
    output. This bounds the expansion blast radius from a single seed:
    the W80c PR 2 wire-in canary measured 137 candidates from 3 seeds
    touching ``FCT_ENTITY_INFO`` / ``DIM_*`` tables — most of those
    edges had ``matching_columns == []`` (pure passthrough). Capping at
    ``per_seed_cap=20`` keeps every load-bearing edge (5-col, 3-col,
    2-col, 1-col) and drops the long tail of 0-col passthrough.

    ``per_seed_cap <= 0`` disables the cap (returns the pre-PR-2-retune
    behaviour). Use this for tests of the underlying mechanism — the
    production path always passes a positive cap via
    :func:`rerank_with_rrf`.

    Stable across-seed ordering: seeds are walked in input order; a
    neighbor reachable from multiple seeds appears at its first-seen
    seed's position. Case-insensitive dedupe key (uppercased function
    name); output preserves the casing recorded in the edge.
    """
    if not seeds or edge_index is None:
        return []
    seed_upper = {(s or "").upper() for s in seeds if s}
    out: List[str] = []
    added: set[str] = set()
    for seed in seeds:
        if not seed:
            continue
        neighbors = edge_index.neighbors(seed)
        if per_seed_cap is not None and per_seed_cap > 0:
            # Stable sort: primary key = -len(matching_columns), ties
            # preserve original edge-list order (Python sort stability).
            neighbors = sorted(neighbors, key=lambda e: -len(e.matching_columns))
            neighbors = neighbors[:per_seed_cap]
        for edge in neighbors:
            tgt = edge.to_function
            tgt_u = tgt.upper()
            if tgt_u in seed_upper or tgt_u in added:
                continue
            added.add(tgt_u)
            out.append(tgt)
    return out


def _path_tuple(value: Any) -> Tuple[str, ...]:
    """Coerce a manifest ``sub_process_path`` into a comparable tuple.

    Matches the resilience of W89's normalizer — list/tuple/string all
    accepted; malformed values become empty.
    """
    if isinstance(value, (list, tuple)):
        return tuple(str(p) for p in value if p is not None)
    if isinstance(value, str) and value:
        return (value,)
    return ()


def score_candidate(
    candidate_fn: str,
    seeds: List[str],
    edge_index: EdgeIndex,
    hierarchy_lookup: Callable[[str], Optional[Dict[str, Any]]],
    weights: Optional[Dict[str, float]] = None,
) -> Candidate:
    """Compute the four graph features for one candidate.

    ``matching_column_sum`` sums column counts across EVERY edge from
    the candidate to ANY seed — a candidate connected to two seeds via
    different tables aggregates both. ``seed_reach_count`` counts the
    distinct seeds reached, not the number of edges.

    ``same_sub_process_path`` is True iff the candidate's path tuple
    is exactly equal to some seed's path tuple — a prefix match doesn't
    count. ``same_process`` is the looser sibling.

    The linear-combined ``graph_score`` used by the ranker is attached
    as ``_graph_score`` on the returned ``Candidate``; the raw features
    remain visible on the dataclass for auditability.
    """
    merged_weights = dict(_DEFAULT_WEIGHTS)
    if weights:
        merged_weights.update(weights)

    candidate_upper = (candidate_fn or "").upper()
    seeds_upper_all = [(s or "").upper() for s in seeds if s]
    seeds_excl = {s for s in seeds_upper_all if s != candidate_upper}

    matching_column_sum = 0
    seeds_reached: set[str] = set()
    if seeds_excl:
        for edge in edge_index.neighbors(candidate_fn):
            neighbor_u = edge.to_function.upper()
            if neighbor_u in seeds_excl:
                matching_column_sum += len(edge.matching_columns)
                seeds_reached.add(neighbor_u)

    c_hier = hierarchy_lookup(candidate_fn) or {}
    c_path = _path_tuple(c_hier.get("sub_process_path"))
    c_process = (c_hier.get("process") or "")

    same_sub = False
    same_proc = False
    if c_path or c_process:
        for s in seeds:
            if not s:
                continue
            if s.upper() == candidate_upper:
                continue
            s_hier = hierarchy_lookup(s) or {}
            s_path = _path_tuple(s_hier.get("sub_process_path"))
            s_process = (s_hier.get("process") or "")
            if c_path and s_path and c_path == s_path:
                same_sub = True
            if c_process and s_process and c_process == s_process:
                same_proc = True
            if same_sub and same_proc:
                break

    candidate = Candidate(
        function_name=candidate_fn,
        vector_rank=0,
        vector_score=0.0,
        matching_column_sum=matching_column_sum,
        seed_reach_count=len(seeds_reached),
        same_sub_process_path=same_sub,
        same_process=same_proc,
    )
    graph_score = (
        merged_weights["matching_columns"] * matching_column_sum
        + merged_weights["seed_reach"] * len(seeds_reached)
        + merged_weights["sub_process"] * (1.0 if same_sub else 0.0)
        + merged_weights["process"] * (1.0 if same_proc else 0.0)
    )
    candidate._graph_score = graph_score  # type: ignore[attr-defined]
    return candidate


# ---------------------------------------------------------------------
# RRF fusion entry point
# ---------------------------------------------------------------------


def rerank_with_rrf(
    vector_hits: List[Dict[str, Any]],
    *,
    redis_client: Any,
    seed_count: int = 3,
    keep_top: int = 30,
    schema: Optional[str] = None,
    weights: Optional[Dict[str, float]] = None,
    k: int = 60,
    per_seed_cap: int = _DEFAULT_PER_SEED_CAP,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Reciprocal Rank Fusion between cosine rank and graph-edge rank.

    *vector_hits* — list of dicts with at least ``function_name``,
    ``schema``, ``score``. Same shape as ``VectorStore.search`` output
    and what ``state['search_results']`` holds.

    Schema is inferred from ``vector_hits[0]['schema']`` when not
    supplied. If still empty (e.g. an anchor-injected hit with empty
    schema arrives as the sole element), the input is returned
    unchanged — Stage 1 confirmed canary targets land in one schema,
    but the defensive path matters because ``ensure_anchor_in_search_results``
    can stamp an empty schema on its injected entry.

    ``per_seed_cap`` (default 20) bounds the per-seed neighbor count
    forwarded to expansion. The PR 2 wire-in canary measured 137
    expansion candidates from 3 seeds touching dimension tables; the
    cap holds expansion at ~``3 * per_seed_cap`` pre-dedupe and keeps
    load-bearing edges (highest matching_columns first) within reach
    of every seed. Pass ``per_seed_cap=0`` to disable.

    The function is pure relative to its inputs apart from Redis I/O,
    which is gated by the process-level ``EdgeIndex`` cache. The input
    list is not mutated.
    """
    empty_stats: Dict[str, Any] = {
        "seed_count": 0,
        "expanded_count": 0,
        "kept_count": len(vector_hits or []),
        "rank_change_count": 0,
    }
    if not vector_hits:
        return list(vector_hits or []), empty_stats

    # Schema resolution. The first non-anchor-injected entry has a
    # real schema; consult position 0 first since the typical case is
    # a vector-search-only slate where every hit shares a schema.
    resolved_schema = (schema or "").strip()
    if not resolved_schema:
        for hit in vector_hits:
            if isinstance(hit, dict):
                s = (hit.get("schema") or "").strip()
                if s:
                    resolved_schema = s
                    break
    if not resolved_schema:
        return list(vector_hits), empty_stats

    edge_index = EdgeIndex.for_schema(redis_client, resolved_schema)

    # Input order snapshot — used for vector_rank assignment and for
    # the rank_change_count statistic.
    input_fns: List[str] = []
    for hit in vector_hits:
        if not isinstance(hit, dict):
            input_fns.append("")
            continue
        input_fns.append(hit.get("function_name") or "")
    input_pos: Dict[str, int] = {}
    for idx, fn in enumerate(input_fns):
        u = fn.upper()
        if u and u not in input_pos:
            input_pos[u] = idx

    seeds = [fn for fn in input_fns[:seed_count] if fn]
    actual_seed_count = len(seeds)

    expanded = expand_one_hop(seeds, edge_index, per_seed_cap=per_seed_cap)
    expanded_count = len(expanded)

    # Per-request hierarchy memoization. score_candidate looks up the
    # candidate plus every seed; without memoization that's
    # O(candidates × seeds) Redis ops per request.
    _hier_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def _hier_for(fn: str) -> Optional[Dict[str, Any]]:
        if not fn:
            return None
        u = fn.upper()
        if u in _hier_cache:
            return _hier_cache[u]
        graph: Optional[Dict[str, Any]] = None
        try:
            graph = get_function_graph(redis_client, resolved_schema, u)
        except Exception:
            graph = None
        hierarchy = None
        if isinstance(graph, dict):
            h = graph.get("hierarchy")
            if isinstance(h, dict):
                hierarchy = h
        _hier_cache[u] = hierarchy
        return hierarchy

    # Build the rerank pool: union of vector_hits and the expansion
    # set, deduped by uppercased function name. Preserve first-seen
    # ordering so the result is deterministic across runs.
    seen: set[str] = set()
    pool: List[str] = []
    for fn in input_fns:
        u = fn.upper()
        if u and u not in seen:
            seen.add(u)
            pool.append(fn)
    for fn in expanded:
        u = fn.upper()
        if u and u not in seen:
            seen.add(u)
            pool.append(fn)

    # Per-fn vector signal (1-based rank + raw cosine score). First
    # occurrence wins on duplicate names.
    vec_signal: Dict[str, Tuple[int, float]] = {}
    for pos, hit in enumerate(vector_hits):
        if not isinstance(hit, dict):
            continue
        name = hit.get("function_name") or ""
        if not name:
            continue
        u = name.upper()
        if u in vec_signal:
            continue
        try:
            score_val = float(hit.get("score", 0.0))
        except (TypeError, ValueError):
            score_val = 0.0
        vec_signal[u] = (pos + 1, score_val)
    worst_vec_rank = len(vector_hits) + 1

    # Score everything in the pool. Candidates not 1-hop-reachable
    # from any seed land in the without-reach bucket and only the
    # vector signal carries them through RRF.
    candidates: List[Candidate] = []
    for fn in pool:
        cand = score_candidate(fn, seeds, edge_index, _hier_for, weights=weights)
        u = fn.upper()
        v_rank, v_score = vec_signal.get(u, (worst_vec_rank, 0.0))
        cand.vector_rank = v_rank
        cand.vector_score = v_score
        candidates.append(cand)

    with_reach: List[Candidate] = []
    without_reach: List[Candidate] = []
    for c in candidates:
        if getattr(c, "_graph_score", 0.0) > 0.0:
            with_reach.append(c)
        else:
            without_reach.append(c)

    with_reach.sort(
        key=lambda c: (
            -getattr(c, "_graph_score", 0.0),
            c.vector_rank,
            c.function_name.upper(),
        )
    )
    for rank_idx, c in enumerate(with_reach, start=1):
        c.graph_rank = rank_idx
    for c in without_reach:
        c.graph_rank = _NO_GRAPH_REACH_RANK

    def _rrf(c: Candidate) -> float:
        return (1.0 / (k + c.vector_rank)) + (1.0 / (k + c.graph_rank))

    ranked = sorted(
        candidates,
        key=lambda c: (-_rrf(c), c.vector_rank, c.function_name.upper()),
    )
    for rank_idx, c in enumerate(ranked, start=1):
        c.final_rank = rank_idx

    capped = ranked[:keep_top]

    # Re-emit dicts. Original vector_hit dicts pass through unchanged
    # for already-known fns; expansion-only fns get a synthesized stub
    # matching the W95 anchor-inject shape so fetch_multi_logic can
    # resolve them downstream.
    by_upper: Dict[str, Dict[str, Any]] = {}
    for hit in vector_hits:
        if not isinstance(hit, dict):
            continue
        name = hit.get("function_name") or ""
        if not name:
            continue
        u = name.upper()
        if u not in by_upper:
            by_upper[u] = hit

    output_hits: List[Dict[str, Any]] = []
    for c in capped:
        u = c.function_name.upper()
        existing = by_upper.get(u)
        if existing is not None:
            output_hits.append(existing)
        else:
            output_hits.append({
                "function_name": c.function_name,
                "schema": resolved_schema,
                "module": "",
                "description": "",
                "tables_read": "",
                "tables_written": "",
                "key_columns": "",
                "score": 0.0,
                "graph_rerank_added": True,
            })

    rank_change_count = 0
    for i, hit in enumerate(output_hits):
        if not isinstance(hit, dict):
            continue
        u = (hit.get("function_name") or "").upper()
        prior = input_pos.get(u)
        if prior is None:
            rank_change_count += 1  # expansion-added
        elif prior != i:
            rank_change_count += 1

    stats: Dict[str, Any] = {
        "seed_count": actual_seed_count,
        "expanded_count": expanded_count,
        "kept_count": len(output_hits),
        "rank_change_count": rank_change_count,
    }

    logger.debug(
        "rerank_with_rrf: schema=%s seeds=%d expanded=%d kept=%d rank_changes=%d",
        resolved_schema, actual_seed_count, expanded_count,
        len(output_hits), rank_change_count,
    )

    return output_hits, stats
