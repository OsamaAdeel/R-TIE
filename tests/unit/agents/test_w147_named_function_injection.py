"""W147 — unit tests for ``ensure_named_functions_in_search_results``.

Fix B of W147: when a query names a function in plain prose ("What feeds
data into FN_G_TEST_CSTM?") it sets neither the W76 anchor nor BI routing,
so W95's ``ensure_anchor_in_search_results`` never injects it. If the named
function also fell outside the vector-search top-K its body is never loaded
into ``multi_source`` — and the W49 detector then falsely reports
PARTIAL_SOURCE_INDEXED (the W147 false positive).

This helper closes that retrieval-coverage gap by injecting any
graph-verified named function still missing from ``search_results``, so
``fetch_multi_logic`` loads its body. The non-negotiable guard is that ONLY
graph-verified names are injected — phantom / unverified names (whether they
fail the W58 extraction gates or simply have no graph metadata) must never
be injected.

Companion to ``test_w70_anchor_injection.py`` / ``test_w97_promote_anchor.py``
(W95/W97 anchor coverage + prominence); this file only covers the W147
named-function injection step. The end-to-end E3 behaviour (no
PARTIAL_SOURCE_INDEXED, grounded answer) is covered by the integration
canary in ``tests/integration/test_live_stream.py``.
"""

from __future__ import annotations

from typing import Any, Dict, Set, Tuple
from unittest.mock import MagicMock

from src.agents.anchor_resolution import ensure_named_functions_in_search_results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_graph_redis(function_keys: Set[Tuple[str, str]]):
    """Stub Redis exposing per-function graph keys ``graph:<schema>:<fn>``.

    Supports both the ``.scan`` discovered_schemas uses and the ``.get``
    that ``function_exists_in_graph`` → ``get_function_graph`` performs.
    """
    from src.parsing.serializer import to_msgpack

    stored: Dict[str, bytes] = {
        f"graph:{schema}:{fn}": to_msgpack(
            {"schema": schema, "function_name": fn, "nodes": [], "edges": []}
        )
        for schema, fn in function_keys
    }

    client = MagicMock()
    client.get.side_effect = lambda k: stored.get(
        k if isinstance(k, str) else k.decode()
    )

    def _scan(cursor=0, match="*", count=100):
        # Single-shot scan: return every stored key, then signal completion.
        return 0, list(stored.keys())

    client.scan.side_effect = _scan
    return client


def _result(fn: str, schema: str = "OFSERM", score: float = 0.1) -> Dict[str, Any]:
    return {"function_name": fn, "schema": schema, "score": score}


# ---------------------------------------------------------------------------
# Injection — the positive case (the W147 fix)
# ---------------------------------------------------------------------------


def test_injects_named_function_present_in_graph_but_missing_from_results():
    redis = _stub_graph_redis({("OFSERM", "FN_G_TEST_CSTM")})
    state: Dict[str, Any] = {
        "raw_query": "What feeds data into FN_G_TEST_CSTM?",
        "search_results": [_result("SOME_OTHER_FUNCTION_CSTM")],
        "schema": "OFSERM",
    }

    ensure_named_functions_in_search_results(state, redis_client=redis)

    names = [r["function_name"] for r in state["search_results"]]
    assert "FN_G_TEST_CSTM" in names
    injected = next(r for r in state["search_results"] if r["function_name"] == "FN_G_TEST_CSTM")
    assert injected["anchor_injected"] is True
    # Appended (not prepended) so a W95 anchor would keep position-0 primacy.
    assert names[0] == "SOME_OTHER_FUNCTION_CSTM"


# ---------------------------------------------------------------------------
# Guards — phantom / unverified names are NEVER injected
# ---------------------------------------------------------------------------


def test_does_not_inject_name_absent_from_graph():
    # Survives the W58 extraction gates but has NO graph metadata in any
    # schema → function_exists_in_graph returns False → not injected.
    redis = _stub_graph_redis({("OFSERM", "FN_G_TEST_CSTM")})
    state: Dict[str, Any] = {
        "raw_query": "What feeds data into ZZZ_FAKE_NONEXISTENT_FUNC?",
        "search_results": [_result("FN_G_TEST_CSTM")],
        "schema": "OFSERM",
    }

    ensure_named_functions_in_search_results(state, redis_client=redis)

    names = [r["function_name"] for r in state["search_results"]]
    assert "ZZZ_FAKE_NONEXISTENT_FUNC" not in names
    assert names == ["FN_G_TEST_CSTM"]  # unchanged


def test_does_not_inject_w58_excluded_token():
    # A table-prefixed token never even survives extract_function_candidates,
    # so nothing is injected regardless of graph membership.
    redis = _stub_graph_redis({("OFSERM", "FN_G_TEST_CSTM")})
    state: Dict[str, Any] = {
        "raw_query": "What feeds data into FCT_SOME_TABLE?",
        "search_results": [_result("FN_G_TEST_CSTM")],
        "schema": "OFSERM",
    }

    ensure_named_functions_in_search_results(state, redis_client=redis)

    names = [r["function_name"] for r in state["search_results"]]
    assert names == ["FN_G_TEST_CSTM"]


# ---------------------------------------------------------------------------
# Idempotence / no-ops
# ---------------------------------------------------------------------------


def test_noop_when_named_function_already_in_results():
    redis = _stub_graph_redis({("OFSERM", "FN_G_TEST_CSTM")})
    state: Dict[str, Any] = {
        "raw_query": "What feeds data into FN_G_TEST_CSTM?",
        # Already present (different casing) → must not duplicate.
        "search_results": [_result("fn_g_test_cstm")],
        "schema": "OFSERM",
    }

    ensure_named_functions_in_search_results(state, redis_client=redis)

    assert len(state["search_results"]) == 1


def test_noop_when_redis_client_is_none():
    # Can't verify graph membership → fail closed, never inject.
    state: Dict[str, Any] = {
        "raw_query": "What feeds data into FN_G_TEST_CSTM?",
        "search_results": [],
        "schema": "OFSERM",
    }
    ensure_named_functions_in_search_results(state, redis_client=None)
    assert state["search_results"] == []


def test_noop_when_no_function_candidates():
    redis = _stub_graph_redis({("OFSERM", "FN_G_TEST_CSTM")})
    state: Dict[str, Any] = {
        "raw_query": "How does goodwill consolidation work?",
        "search_results": [_result("FN_G_TEST_CSTM")],
        "schema": "OFSERM",
    }
    ensure_named_functions_in_search_results(state, redis_client=redis)
    assert len(state["search_results"]) == 1
