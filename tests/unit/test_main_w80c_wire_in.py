"""W80c PR 2 — unit tests for the rerank wire-in helper in main.py.

The helper :func:`apply_w80c_rerank` is the gate + try/except wrapper
around :func:`src.agents.graph_rerank.rerank_with_rrf`. The underlying
ranker has its own tests in ``test_graph_rerank.py``; this file only
verifies the wire-in contract:

  * query-type gate (VARIABLE_TRACE / COLUMN_LOGIC only)
  * redis-availability gate
  * empty-results gate
  * happy-path mutation of ``search_results`` and ``graph_rerank_stats``
  * exception handling (best-effort augmentation must never corrupt
    ``search_results``)

``rerank_with_rrf`` is monkeypatched so each test can pin its return
value (or raise) without needing a fake Redis with msgpack-encoded
edges. The integration canary in ``tests/integration/test_live_stream.py``
covers the end-to-end shape against a live backend.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pytest

from src import main as main_mod


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _make_hits(*names: str) -> List[Dict[str, Any]]:
    return [
        {"function_name": name, "schema": "OFSERM", "score": 0.1 * i}
        for i, name in enumerate(names, start=1)
    ]


def _make_state(
    *,
    query_type: str = "VARIABLE_TRACE",
    hits: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    return {
        "query_type": query_type,
        "search_results": (hits if hits is not None else _make_hits("FN_A", "FN_B", "FN_C")),
    }


class _SentinelRedis:
    """Identity-only stand-in for a Redis client.

    The helper passes this through to ``rerank_with_rrf`` (which the
    tests monkeypatch), so no methods are called on it — but it must
    be a non-None object so the redis-availability gate passes.
    """

    pass


def _stub_rerank(
    return_hits: List[Dict[str, Any]],
    return_stats: Dict[str, Any],
):
    """Build a monkeypatch replacement for ``rerank_with_rrf``."""
    def _stub(vector_hits, **kwargs):
        return return_hits, return_stats
    return _stub


# =====================================================================
# 1. VARIABLE_TRACE: rerank invoked, search_results reordered, stats stamped
# =====================================================================


def test_variable_trace_invokes_rerank_and_stamps_stats(monkeypatch):
    reordered = _make_hits("FN_C", "FN_A", "FN_B")  # rerank rotated the order
    stats = {
        "seed_count": 3,
        "expanded_count": 1,
        "kept_count": 3,
        "rank_change_count": 2,
    }
    captured: Dict[str, Any] = {}

    def _spy(vector_hits, **kwargs):
        captured["vector_hits"] = vector_hits
        captured["kwargs"] = kwargs
        return reordered, stats

    monkeypatch.setattr(main_mod, "rerank_with_rrf", _spy)

    state = _make_state(query_type="VARIABLE_TRACE")
    main_mod.apply_w80c_rerank(
        state,
        redis_client=_SentinelRedis(),
        correlation_id="cid-abc",
        schema_scope="OFSERM",
    )

    assert state["search_results"] == reordered
    assert state["graph_rerank_stats"]["status"] == "ok"
    assert state["graph_rerank_stats"]["seed_count"] == 3
    assert state["graph_rerank_stats"]["expanded_count"] == 1
    assert state["graph_rerank_stats"]["kept_count"] == 3
    assert state["graph_rerank_stats"]["rank_change_count"] == 2

    # W80c-v2 retune: keep_top = resolve_top_k("VARIABLE_TRACE") + 20
    # = 20 + 20 = 40 (was top_k+10 in PR 2 before the T3 chase).
    assert captured["kwargs"]["seed_count"] == 3
    assert captured["kwargs"]["keep_top"] == 40
    assert captured["kwargs"]["per_seed_cap"] == 20


# =====================================================================
# 2. COLUMN_LOGIC: gate also opens for column-logic queries
# =====================================================================


def test_column_logic_invokes_rerank(monkeypatch):
    reordered = _make_hits("FN_B", "FN_A")
    stats = {
        "seed_count": 2,
        "expanded_count": 0,
        "kept_count": 2,
        "rank_change_count": 2,
    }
    monkeypatch.setattr(
        main_mod, "rerank_with_rrf",
        _stub_rerank(reordered, stats),
    )

    state = _make_state(query_type="COLUMN_LOGIC", hits=_make_hits("FN_A", "FN_B"))
    main_mod.apply_w80c_rerank(
        state,
        redis_client=_SentinelRedis(),
        correlation_id="cid-col",
        schema_scope="OFSERM",
    )

    assert state["search_results"] == reordered
    assert state["graph_rerank_stats"]["status"] == "ok"


# =====================================================================
# 3. FUNCTION_LOGIC: gate stays closed; rerank NOT invoked
# =====================================================================


def test_function_logic_skipped_no_rerank_call(monkeypatch):
    invocations: List[Tuple[Any, Dict[str, Any]]] = []

    def _should_not_be_called(vector_hits, **kwargs):
        invocations.append((vector_hits, kwargs))
        return [], {}

    monkeypatch.setattr(main_mod, "rerank_with_rrf", _should_not_be_called)

    original_hits = _make_hits("FN_A", "FN_B")
    state = _make_state(query_type="FUNCTION_LOGIC", hits=original_hits)
    main_mod.apply_w80c_rerank(
        state,
        redis_client=_SentinelRedis(),
        correlation_id="cid-fn",
        schema_scope="OFSERM",
    )

    assert invocations == []
    assert state["search_results"] == original_hits
    assert state["graph_rerank_stats"] == {"status": "skipped_query_type"}


# =====================================================================
# 4. _graph_redis is None: gate stays closed, no exception, no mutation
# =====================================================================


def test_no_redis_client_skipped(monkeypatch):
    def _should_not_be_called(vector_hits, **kwargs):
        raise AssertionError("rerank_with_rrf must not be invoked when redis is None")

    monkeypatch.setattr(main_mod, "rerank_with_rrf", _should_not_be_called)

    original_hits = _make_hits("FN_A", "FN_B", "FN_C")
    state = _make_state(query_type="VARIABLE_TRACE", hits=original_hits)
    main_mod.apply_w80c_rerank(
        state,
        redis_client=None,
        correlation_id="cid-noredis",
        schema_scope="OFSERM",
    )

    assert state["search_results"] == original_hits
    assert state["graph_rerank_stats"] == {"status": "skipped_no_redis"}


# =====================================================================
# 5. Empty search_results: gate stays closed (nothing to rerank)
# =====================================================================


def test_empty_search_results_skipped(monkeypatch):
    monkeypatch.setattr(
        main_mod, "rerank_with_rrf",
        _stub_rerank([], {"seed_count": 0, "expanded_count": 0,
                          "kept_count": 0, "rank_change_count": 0}),
    )

    state = _make_state(query_type="VARIABLE_TRACE", hits=[])
    main_mod.apply_w80c_rerank(
        state,
        redis_client=_SentinelRedis(),
        correlation_id="cid-empty",
        schema_scope="OFSERM",
    )

    assert state["search_results"] == []
    assert state["graph_rerank_stats"] == {"status": "skipped_empty_results"}


# =====================================================================
# 6. rerank_with_rrf raises: log warning, leave search_results unchanged,
#    stamp error status on stats. Best-effort augmentation must never
#    corrupt the downstream fetch input.
# =====================================================================


def test_rerank_exception_leaves_state_unchanged(monkeypatch, caplog):
    def _exploding_rerank(vector_hits, **kwargs):
        raise RuntimeError("synthetic redis blip")

    monkeypatch.setattr(main_mod, "rerank_with_rrf", _exploding_rerank)

    original_hits = _make_hits("FN_A", "FN_B", "FN_C")
    state = _make_state(query_type="VARIABLE_TRACE", hits=original_hits)

    # Helper must not raise.
    main_mod.apply_w80c_rerank(
        state,
        redis_client=_SentinelRedis(),
        correlation_id="cid-err",
        schema_scope="OFSERM",
    )

    # search_results untouched — fetch_multi_logic still gets the
    # original vector slate. graph_rerank_stats records the failure
    # for the canary to spot the regression.
    assert state["search_results"] == original_hits
    assert state["graph_rerank_stats"]["status"] == "error"
    assert state["graph_rerank_stats"]["error"] == "RuntimeError"
