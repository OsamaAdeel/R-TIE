"""Unit tests for W80c — hybrid graph + vector RRF rerank helper.

The fixture mirrors the significant-investment cluster from
``docs/w80c_diagnostic.md`` Section 2.B: five canary targets (T1..T5)
with the documented cross-function edges, plus three decoy functions
(D1..D3) that vector search ranks high but graph reachability should
de-prioritize. The canary-shaped acceptance test asserts that T3, T4,
T5 (vector ranks 8, 12, 18) land in the top-5 of the reranked slate.

Tests use a small in-memory ``_FakeRedis`` shim (same pattern as
``tests/unit/agents/test_w89_chain_ordering.py``) that returns
MessagePack-encoded payloads for the keys the helper reads
(``graph:full:<schema>`` and ``graph:<schema>:<fn>``). No real Redis,
no fakeredis dependency.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import msgpack
import pytest

from src.agents.graph_rerank import (
    Candidate,
    Edge,
    EdgeIndex,
    _reset_cache_for_test,
    expand_one_hop,
    rerank_with_rrf,
    score_candidate,
)


# ---------------------------------------------------------------------
# Fake Redis: msgpack-encoded values keyed by string. ``.get(key)``
# returns bytes (matching the production ``decode_responses=False``
# client) or None.
# ---------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, data: Dict[str, Any]) -> None:
        self._encoded: Dict[str, bytes] = {
            k: msgpack.packb(v, use_bin_type=True) for k, v in data.items()
        }

    def get(self, key: str) -> Optional[bytes]:
        return self._encoded.get(key)


def _full_graph_blob(
    *,
    schema: str,
    edge_records: List[Tuple[str, str, str, List[str], str]],
    extra_non_cross_edges: int = 0,
    function_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a ``graph:full:<schema>`` payload from edge tuples.

    Each edge tuple is ``(from_function, to_function, table,
    matching_columns, edge_type)``. ``edge_type`` is usually
    ``CROSS_FUNCTION_TABLE_FLOW``; tests can pass other values to
    exercise the type filter.
    """
    edges = []
    for i, (from_fn, to_fn, table, cols, etype) in enumerate(edge_records):
        edges.append({
            "id": f"E{i + 1}",
            "type": etype,
            "from": "",
            "to": "",
            "table": table,
            "from_function": from_fn,
            "to_function": to_fn,
            "matching_columns": list(cols),
        })
    nodes = {fn: [] for fn in (function_names or [])}
    return {
        "schema": schema,
        "built_at": "test",
        "function_count": len(nodes),
        "node_count": 0,
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def _function_graph_blob(hierarchy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {"hierarchy": hierarchy} if hierarchy is not None else {}


@pytest.fixture(autouse=True)
def _clean_edge_cache():
    """Reset the EdgeIndex cache before AND after every test.

    The cache is module-level and survives test boundaries by design;
    without this fixture a fake Redis built in one test would still be
    visible to the next.
    """
    _reset_cache_for_test()
    yield
    _reset_cache_for_test()


# =====================================================================
# 1. EdgeIndex.for_schema: empty when graph:full key is missing
# =====================================================================


def test_for_schema_returns_empty_when_key_missing():
    redis = _FakeRedis({})
    idx = EdgeIndex.for_schema(redis, "OFSERM")
    assert isinstance(idx, EdgeIndex)
    assert idx.neighbors("ANY_FN") == []


# =====================================================================
# 2. EdgeIndex.for_schema: decodes only CROSS_FUNCTION_TABLE_FLOW edges
# =====================================================================


def test_for_schema_indexes_only_cross_edges():
    edges = [
        ("FN_A", "FN_B", "TBL_1", ["COL_X"], "CROSS_FUNCTION_TABLE_FLOW"),
        ("FN_A", "FN_C", "TBL_2", [], "CROSS_FUNCTION_TABLE_FLOW"),
        ("FN_B", "FN_C", "TBL_3", ["COL_Y", "COL_Z"], "CROSS_FUNCTION_TABLE_FLOW"),
        # Non-cross edge — must be ignored.
        ("FN_A", "FN_B", "TBL_1", ["IGNORED"], "ASSIGN"),
    ]
    redis = _FakeRedis({
        "graph:full:OFSERM": _full_graph_blob(
            schema="OFSERM",
            edge_records=edges,
            function_names=["FN_A", "FN_B", "FN_C"],
        ),
    })
    idx = EdgeIndex.for_schema(redis, "OFSERM")

    # FN_A has two out-edges; FN_C has two in-edges (FN_A, FN_B).
    fn_a_neighbors = idx.neighbors("FN_A")
    assert {e.to_function for e in fn_a_neighbors} == {"FN_B", "FN_C"}
    assert all(e.direction == "out" for e in fn_a_neighbors)

    fn_c_neighbors = idx.neighbors("FN_C")
    assert {e.to_function for e in fn_c_neighbors} == {"FN_A", "FN_B"}
    assert all(e.direction == "in" for e in fn_c_neighbors)

    # No edge marked as ASSIGN-derived: column set "IGNORED" never appears.
    all_cols: set[str] = set()
    for fn in ("FN_A", "FN_B", "FN_C"):
        for e in idx.neighbors(fn):
            all_cols.update(e.matching_columns)
    assert "IGNORED" not in all_cols


# =====================================================================
# 3. EdgeIndex caches per-schema (identity)
# =====================================================================


def test_for_schema_caches_per_schema_returns_same_instance():
    redis_a = _FakeRedis({
        "graph:full:OFSERM": _full_graph_blob(
            schema="OFSERM",
            edge_records=[
                ("FN_X", "FN_Y", "TBL", ["C"], "CROSS_FUNCTION_TABLE_FLOW"),
            ],
        ),
        "graph:full:OFSMDM": _full_graph_blob(
            schema="OFSMDM",
            edge_records=[
                ("FN_M", "FN_N", "TBL_M", ["CM"], "CROSS_FUNCTION_TABLE_FLOW"),
            ],
        ),
    })
    a1 = EdgeIndex.for_schema(redis_a, "OFSERM")
    a2 = EdgeIndex.for_schema(redis_a, "OFSERM")
    b = EdgeIndex.for_schema(redis_a, "OFSMDM")
    assert a1 is a2
    assert a1 is not b
    # Sanity: each instance reflects its schema's edges.
    assert {e.to_function for e in a1.neighbors("FN_X")} == {"FN_Y"}
    assert {e.to_function for e in b.neighbors("FN_M")} == {"FN_N"}


# =====================================================================
# 4. EdgeIndex.neighbors is case-insensitive and returns both directions
# =====================================================================


def test_neighbors_case_insensitive_both_directions():
    edges = [
        ("FN_A", "FN_B", "TBL_1", ["C1"], "CROSS_FUNCTION_TABLE_FLOW"),
        ("FN_C", "FN_A", "TBL_2", ["C2"], "CROSS_FUNCTION_TABLE_FLOW"),
    ]
    redis = _FakeRedis({
        "graph:full:OFSERM": _full_graph_blob(
            schema="OFSERM", edge_records=edges,
        ),
    })
    idx = EdgeIndex.for_schema(redis, "OFSERM")
    # Lookup by lowercased name works.
    n = idx.neighbors("fn_a")
    directions = {(e.to_function, e.direction) for e in n}
    assert directions == {("FN_B", "out"), ("FN_C", "in")}


# =====================================================================
# 5. expand_one_hop dedupes and excludes seeds
# =====================================================================


def test_expand_one_hop_excludes_seeds_and_dedupes():
    edges = [
        ("S1", "X", "TBL", [], "CROSS_FUNCTION_TABLE_FLOW"),
        ("S2", "X", "TBL", [], "CROSS_FUNCTION_TABLE_FLOW"),
        ("S1", "S2", "TBL", [], "CROSS_FUNCTION_TABLE_FLOW"),  # seed→seed
        ("X", "Y", "TBL", [], "CROSS_FUNCTION_TABLE_FLOW"),
    ]
    redis = _FakeRedis({
        "graph:full:OFSERM": _full_graph_blob(
            schema="OFSERM", edge_records=edges,
        ),
    })
    idx = EdgeIndex.for_schema(redis, "OFSERM")

    # ``per_seed_cap=0`` disables the cap so this test verifies the
    # dedupe + seed-exclusion behavior independently of the cap's
    # sort-then-slice mechanism.
    out = expand_one_hop(["S1", "S2"], idx, per_seed_cap=0)
    # X is reachable from both seeds but should appear once. Y is
    # reachable only via S1 (in-edge from X→Y) since the index records
    # both directions; that's still 1 hop from S1. Seeds themselves
    # must not appear.
    assert "S1" not in out and "S2" not in out
    assert out.count("X") == 1
    # First-seen order: S1's out-neighbors first, then S2's.
    assert out.index("X") < (out.index("Y") if "Y" in out else len(out))


# =====================================================================
# 5b. expand_one_hop per_seed_cap keeps strongest-match edges
# =====================================================================


def test_expand_one_hop_per_seed_cap_keeps_strongest_matching_edges():
    """W80c PR 2 retune — seeds with many neighbours get bounded.

    Single seed S with 5 neighbours of matching-column counts
    [3, 0, 2, 0, 1]. With ``per_seed_cap=3`` the cap keeps S's three
    strongest-matching-column edges (to A, C, E) and drops the two
    0-col passthrough edges (to B, D). Output is high-to-low
    matching-count order because :func:`expand_one_hop` sorts each
    seed's neighbour list (stable; ties preserve edge insertion
    order) before slicing.

    Production rationale: PR 2 wire-in canary measured 137 expansion
    candidates from 3 seeds touching ``FCT_ENTITY_INFO`` / ``DIM_*``
    tables. ~100+ of those were 0-col DIM passthrough. Cap=20 (the
    production default) keeps every load-bearing edge in the
    significant-investment cluster (5-col T2→T4, 3-col T2→T5, 2-col
    T3→T2, 1-col T1→T2) within reach while shedding the long tail
    that flooded the keep_top window.
    """
    edges = [
        ("S", "A", "TBL_A", ["c1", "c2", "c3"], "CROSS_FUNCTION_TABLE_FLOW"),
        ("S", "B", "TBL_B", [], "CROSS_FUNCTION_TABLE_FLOW"),
        ("S", "C", "TBL_C", ["c1", "c2"], "CROSS_FUNCTION_TABLE_FLOW"),
        ("S", "D", "TBL_D", [], "CROSS_FUNCTION_TABLE_FLOW"),
        ("S", "E", "TBL_E", ["c1"], "CROSS_FUNCTION_TABLE_FLOW"),
    ]
    redis = _FakeRedis({
        "graph:full:OFSERM": _full_graph_blob(
            schema="OFSERM", edge_records=edges,
        ),
    })
    idx = EdgeIndex.for_schema(redis, "OFSERM")

    capped = expand_one_hop(["S"], idx, per_seed_cap=3)
    assert capped == ["A", "C", "E"], (
        "Cap=3 should keep top-3 by matching_columns count (A=3, C=2, "
        "E=1) and drop the two 0-col passthrough edges (B, D)."
    )

    # Uncapped (per_seed_cap=0) returns all neighbours; the seed
    # exclusion + dedupe behaviour is unchanged.
    uncapped = expand_one_hop(["S"], idx, per_seed_cap=0)
    assert set(uncapped) == {"A", "B", "C", "D", "E"}

    # Default cap (20) doesn't trigger here — only 5 neighbours exist.
    default = expand_one_hop(["S"], idx)
    assert set(default) == {"A", "B", "C", "D", "E"}


# =====================================================================
# 6. score_candidate sums matching_columns across edges to multiple seeds
# =====================================================================


def test_score_candidate_sums_matching_columns_across_seeds():
    edges = [
        # C → S1 with two matching columns.
        ("C", "S1", "TBL_1", ["COL_A", "COL_B"], "CROSS_FUNCTION_TABLE_FLOW"),
        # C → S2 with three different matching columns.
        ("C", "S2", "TBL_2", ["COL_C", "COL_D", "COL_E"], "CROSS_FUNCTION_TABLE_FLOW"),
        # C → NON_SEED — must NOT contribute.
        ("C", "OTHER", "TBL_3", ["COL_X", "COL_Y"], "CROSS_FUNCTION_TABLE_FLOW"),
    ]
    redis = _FakeRedis({
        "graph:full:OFSERM": _full_graph_blob(
            schema="OFSERM", edge_records=edges,
        ),
    })
    idx = EdgeIndex.for_schema(redis, "OFSERM")

    hier = {
        "C": {"sub_process_path": ["P1"], "process": "PROC_C"},
        "S1": {"sub_process_path": ["P2"], "process": "PROC_S"},
        "S2": {"sub_process_path": ["P3"], "process": "PROC_T"},
        "OTHER": None,
    }
    lookup = lambda fn: hier.get(fn.upper())

    cand = score_candidate("C", ["S1", "S2", "OTHER_SEED_NOT_IN_GRAPH"], idx, lookup)
    # 2 + 3 = 5; "OTHER" not in seeds so not counted.
    assert cand.matching_column_sum == 5
    assert cand.seed_reach_count == 2  # reached S1 and S2 only
    assert cand.same_sub_process_path is False
    assert cand.same_process is False


# =====================================================================
# 7. same_sub_process_path requires EXACT tuple match (not prefix)
# =====================================================================


def test_same_sub_process_path_requires_exact_tuple_match():
    edges = [
        ("C", "S_EXACT", "TBL", ["X"], "CROSS_FUNCTION_TABLE_FLOW"),
        ("C", "S_PREFIX", "TBL", ["Y"], "CROSS_FUNCTION_TABLE_FLOW"),
    ]
    redis = _FakeRedis({
        "graph:full:OFSERM": _full_graph_blob(
            schema="OFSERM", edge_records=edges,
        ),
    })
    idx = EdgeIndex.for_schema(redis, "OFSERM")

    hier = {
        # C's path is two levels deep.
        "C": {"sub_process_path": ["LEVEL_A", "LEVEL_B"], "process": "P"},
        # S_EXACT shares the exact tuple.
        "S_EXACT": {"sub_process_path": ["LEVEL_A", "LEVEL_B"], "process": "P"},
        # S_PREFIX shares only the first element — must NOT count.
        "S_PREFIX": {"sub_process_path": ["LEVEL_A"], "process": "P"},
    }
    lookup = lambda fn: hier.get(fn.upper())

    # Same-prefix case alone: no exact match.
    only_prefix = score_candidate("C", ["S_PREFIX"], idx, lookup)
    assert only_prefix.same_sub_process_path is False

    # With both seeds, the exact match flips the flag True.
    with_exact = score_candidate("C", ["S_EXACT", "S_PREFIX"], idx, lookup)
    assert with_exact.same_sub_process_path is True


# =====================================================================
# 8. rerank_with_rrf returns input unchanged when schema is unresolvable
# =====================================================================


def test_rerank_with_rrf_unresolved_schema_returns_input_unchanged():
    # Every hit has an empty schema, and no override is passed in.
    hits = [
        {"function_name": "FN_A", "schema": "", "score": 0.1},
        {"function_name": "FN_B", "schema": "", "score": 0.2},
    ]
    out, stats = rerank_with_rrf(hits, redis_client=_FakeRedis({}))
    assert out == hits
    assert stats == {
        "seed_count": 0,
        "expanded_count": 0,
        "kept_count": 2,
        "rank_change_count": 0,
    }


# =====================================================================
# Significant-investment fixture — shared between tests 9, 10, 11.
# Mirrors docs/w80c_diagnostic.md Section 2.B.
# =====================================================================


_SCHEMA = "OFSERM"

_FN_T1 = "CAP_CONSL_NON_REGULATORY_ENTITY_SIGNIFICANT_INVESTMENT_IDENTIFICATION"
_FN_T2 = "ABL_SIGNIFCNT_INVSTMNT_IN_NON_REG_CONSL_ENTITY_DATA_POP"
_FN_T3 = "SIGNIFICANT_INVESTMENT_IN_PARTY_FOR_REPORTING_BANK_IDENTIFICATION"
_FN_T4 = "SIGNIFICANT_INVST_THRESHOLD_TREATMENT_DATA_POP"
_FN_T5 = "SIGNFCNT_INVSTMNT_CAP_DEDUCTION_EXPOSURES"
_FN_D1 = "DECOY_RANK3_HIGH_COSINE_ISOLATED"
_FN_D2 = "DECOY_RANK4_HIGH_COSINE_ISOLATED"
_FN_D3 = "DECOY_RANK5_HIGH_COSINE_ISOLATED"

# 10 edges total. Canary cluster (T1..T5) matches the diagnostic doc's
# Section 2.B adjacency: T2→T4 carries the 5-column data flow that
# drives T4 to the top of the graph rank; T3→T2 and T3→T5 close the
# triangle that puts T3 and T5 in the top-3. The decoy cluster
# (D1..D3) is intentionally graph-isolated from the canary cluster, so
# no edge between any D-fn and any T-fn exists — D1 is a seed (vector
# rank 3) with zero graph reach, and D2/D3 are non-seed candidates
# with zero graph reach. The two intra-decoy edges (D2↔D3) bring the
# count near the spec's ~12 without affecting canary outcome.
_CANARY_EDGES = [
    (_FN_T1, _FN_T2, "FCT_ENTITY_INFO",
        ["F_SIGNIFICANT_INVESTMENT_IND"], "CROSS_FUNCTION_TABLE_FLOW"),
    (_FN_T1, _FN_T3, "FCT_ENTITY_INFO", [], "CROSS_FUNCTION_TABLE_FLOW"),
    (_FN_T1, _FN_T5, "FCT_ENTITY_INFO", [], "CROSS_FUNCTION_TABLE_FLOW"),
    (_FN_T2, _FN_T4, "FSI_NON_REG_CONSL_ENTITY_INVST",
        [
            "F_SIGNIFICANT_INVESTMENT_IND",
            "N_CET1_INVESTMENT_AMOUNT",
            "N_GAAP_SKEY",
            "N_MIS_DATE_SKEY",
            "N_RUN_SKEY",
        ],
        "CROSS_FUNCTION_TABLE_FLOW"),
    (_FN_T2, _FN_T5, "FSI_NON_REG_CONSL_ENTITY_INVST",
        ["N_GAAP_SKEY", "N_MIS_DATE_SKEY", "N_RUN_SKEY"],
        "CROSS_FUNCTION_TABLE_FLOW"),
    (_FN_T3, _FN_T2, "FCT_PARTY_SHR_HLD_PERCENT",
        ["F_SIGNIFICANT_INVESTMENT_IND", "N_PARTY_SKEY"],
        "CROSS_FUNCTION_TABLE_FLOW"),
    (_FN_T3, _FN_T5, "FCT_PARTY_SHR_HLD_PERCENT",
        ["F_SIGNIFICANT_INVESTMENT_IND", "N_PARTY_SKEY", "N_GROUP_ENTITY_SKEY"],
        "CROSS_FUNCTION_TABLE_FLOW"),
    # Intra-canary edge — T4↔T5 share FSI_THRESHOLD_TREATMENT. Does
    # not change canary outcome (T4 isn't in the seed set), included
    # to push edge count nearer the spec's "~12".
    (_FN_T4, _FN_T5, "FSI_THRESHOLD_TREATMENT",
        ["N_GAAP_SKEY"], "CROSS_FUNCTION_TABLE_FLOW"),
    # Decoy intra-cluster edges, isolated from T1..T5.
    (_FN_D2, _FN_D3, "DECOY_TBL", [], "CROSS_FUNCTION_TABLE_FLOW"),
    (_FN_D3, _FN_D2, "DECOY_TBL_2", [], "CROSS_FUNCTION_TABLE_FLOW"),
]

# Hierarchy per Section 2.A. T1/T3 share CONSOLIDATION_DATA_POPULATION
# so T3 gets the same_sub_process_path bonus; T2/T5 share the ABL
# process. Decoys have a distinct path so they never trigger the
# same-path bonus through any seed.
_HIERARCHY = {
    _FN_T1: {
        "batch": "ABL_CAR_CSTM_V4",
        "process": "CONSL_DATA_PROC",
        "sub_process_path": ["CONSOLIDATION_DATA_POPULATION"],
        "task_order": 2,
    },
    _FN_T2: {
        "batch": "ABL_CAR_CSTM_V4",
        "process": "ABL_PROC",
        "sub_process_path": ["ABL_SIGNIFICANT_INVESTMENT_IN_ENTITIES_PROC"],
        "task_order": 2,
    },
    _FN_T3: {
        "batch": "ABL_CAR_CSTM_V4",
        "process": "CONSL_DATA_PROC",
        "sub_process_path": ["CONSOLIDATION_DATA_POPULATION"],
        "task_order": 6,
    },
    _FN_T4: {
        "batch": "ABL_CAR_CSTM_V4",
        "process": "THRESHOLD_PROC",
        "sub_process_path": ["THRESHOLD_TREATMENT_CALCULATIONS"],
        "task_order": 1,
    },
    _FN_T5: {
        "batch": "ABL_CAR_CSTM_V4",
        "process": "ABL_PROC",
        "sub_process_path": ["ABL_CAPITAL_STRUCTURE_DEDUCTIONS_RWA_EXPOSURES"],
        "task_order": 5,
    },
    # Decoys get distinct hierarchies so neither path nor process
    # collides with any seed. (D1 IS a seed; D2/D3 must not earn a
    # same_sub_process_path bonus by sharing D1's manifest entry, or
    # the hierarchy weighting alone would lift them into with_reach
    # and crowd T4/T5 out of the top-5.)
    _FN_D1: {
        "batch": "OTHER_BATCH",
        "process": "DECOY_PROC_D1",
        "sub_process_path": ["DECOY_PATH_D1"],
        "task_order": 1,
    },
    _FN_D2: {
        "batch": "OTHER_BATCH",
        "process": "DECOY_PROC_D2",
        "sub_process_path": ["DECOY_PATH_D2"],
        "task_order": 2,
    },
    _FN_D3: {
        "batch": "OTHER_BATCH",
        "process": "DECOY_PROC_D3",
        "sub_process_path": ["DECOY_PATH_D3"],
        "task_order": 3,
    },
}


def _canary_redis() -> _FakeRedis:
    data: Dict[str, Any] = {
        f"graph:full:{_SCHEMA}": _full_graph_blob(
            schema=_SCHEMA,
            edge_records=_CANARY_EDGES,
            function_names=[
                _FN_T1, _FN_T2, _FN_T3, _FN_T4, _FN_T5,
                _FN_D1, _FN_D2, _FN_D3,
            ],
        ),
    }
    for fn, hier in _HIERARCHY.items():
        data[f"graph:{_SCHEMA}:{fn.upper()}"] = _function_graph_blob(hier)
    return _FakeRedis(data)


# Padding function names — present in vector_hits but absent from the
# graph and from the hierarchy. They sink to the back of the rerank.
_PADDING = [f"PADDING_FN_{n:02d}" for n in range(1, 11)]


def _canary_vector_hits() -> List[Dict[str, Any]]:
    """20-entry vector_hits placing T3/T4/T5 at ranks 8/12/18.

    Cosine scores are monotonic with position (smaller-is-closer).
    Decoys D1..D3 occupy ranks 3..5 with strong cosine but no graph
    reach. The padding entries fill ranks 6..7, 9..11, 13..17, 19..20.
    """
    layout: List[Tuple[str, float]] = [
        (_FN_T1, 0.10),           # 1
        (_FN_T2, 0.20),           # 2
        (_FN_D1, 0.30),           # 3
        (_FN_D2, 0.40),           # 4
        (_FN_D3, 0.50),           # 5
        (_PADDING[0], 0.60),      # 6
        (_PADDING[1], 0.70),      # 7
        (_FN_T3, 0.80),           # 8  ← canary target
        (_PADDING[2], 0.90),      # 9
        (_PADDING[3], 1.00),      # 10
        (_PADDING[4], 1.10),      # 11
        (_FN_T4, 1.20),           # 12 ← canary target
        (_PADDING[5], 1.30),      # 13
        (_PADDING[6], 1.40),      # 14
        (_PADDING[7], 1.50),      # 15
        (_PADDING[8], 1.60),      # 16
        (_PADDING[9], 1.70),      # 17
        (_FN_T5, 1.80),           # 18 ← canary target
        ("PADDING_FN_TAIL_A", 1.90),  # 19
        ("PADDING_FN_TAIL_B", 2.00),  # 20
    ]
    return [
        {"function_name": fn, "schema": _SCHEMA, "score": score}
        for fn, score in layout
    ]


# =====================================================================
# 9. CANARY-SHAPED acceptance: T3, T4, T5 must land in top-5
# =====================================================================


def test_rerank_lands_significant_investment_targets_in_top5():
    hits = _canary_vector_hits()
    out, _stats = rerank_with_rrf(
        hits,
        redis_client=_canary_redis(),
        seed_count=3,
        keep_top=30,
    )
    top5_names = [r["function_name"] for r in out[:5]]
    assert _FN_T3 in top5_names, (
        f"T3 missing from top-5; reranked head: {top5_names}"
    )
    assert _FN_T4 in top5_names, (
        f"T4 missing from top-5; reranked head: {top5_names}"
    )
    assert _FN_T5 in top5_names, (
        f"T5 missing from top-5; reranked head: {top5_names}"
    )


# =====================================================================
# 10. Stats: seed_count / expanded_count / kept_count / rank_change_count
# =====================================================================


def test_rerank_stats_match_independent_recount():
    hits = _canary_vector_hits()
    out, stats = rerank_with_rrf(
        hits,
        redis_client=_canary_redis(),
        seed_count=3,
        keep_top=30,
    )

    # 3 non-empty seeds (T1, T2, D1).
    assert stats["seed_count"] == 3

    # Expansion from {T1, T2, D1}:
    #   T1 reaches T2 (seed, dropped), T3, T5 — adds T3, T5.
    #   T2 reaches T1 (seed, dropped), T3 (added), T4, T5 (added) — adds T4.
    #   D1 has no graph reach.
    # → expansion produces exactly {T3, T5, T4} = 3 fns.
    assert stats["expanded_count"] == 3

    # No expansion adds a brand-new fn (T3/T4/T5 are already in vector_hits),
    # and keep_top exceeds the pool size, so kept_count == len(input).
    assert stats["kept_count"] == len(hits)

    # rank_change_count: count of fns whose output position differs
    # from input position. Recount independently from the actual output.
    input_pos = {h["function_name"].upper(): i for i, h in enumerate(hits)}
    moves = 0
    for i, r in enumerate(out):
        u = r["function_name"].upper()
        prior = input_pos.get(u)
        if prior is None or prior != i:
            moves += 1
    assert stats["rank_change_count"] == moves


# =====================================================================
# 11. Purity: calling twice with the same input yields the same output
# =====================================================================


def test_rerank_is_pure_across_repeated_calls():
    hits1 = _canary_vector_hits()
    hits2 = _canary_vector_hits()
    redis = _canary_redis()
    out1, stats1 = rerank_with_rrf(hits1, redis_client=redis, seed_count=3)
    out2, stats2 = rerank_with_rrf(hits2, redis_client=redis, seed_count=3)

    # Input lists must not be mutated.
    assert hits1 == _canary_vector_hits()
    assert hits2 == _canary_vector_hits()

    # Outputs must be identical (same function order, same stats).
    fns1 = [r["function_name"] for r in out1]
    fns2 = [r["function_name"] for r in out2]
    assert fns1 == fns2
    assert stats1 == stats2
