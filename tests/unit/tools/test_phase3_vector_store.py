"""Phase 3 — vector store schema TAG, doc-key prefix, and search filter.

Two contracts:

1. Doc keys are now ``rtie:vec:<schema>:<fn>`` (was ``<module>:<fn>``).
   ``upsert_function`` accepts a ``schema`` kwarg, populates the new
   ``schema`` TAG field, and writes under the new prefix.

2. ``search`` accepts an optional ``schema_filter`` that combines with
   the existing ``module_filter`` as an AND clause and yields the
   ``@schema:{...} @module:{...}`` RediSearch pre-filter for KNN.

W146 contract (separate from Phase 3): ``search`` honors ``top_k``
through to RediSearch's paging so callers requesting more than 10
results actually receive up to that count.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tools.vector_store import VectorStore


def test_doc_key_uses_schema_segment():
    """rtie:vec:<schema>:<fn> — schema replaces the legacy module slot."""
    vs = VectorStore(host="localhost", port=6379)
    assert vs._doc_key("OFSERM", "CS_DEFERRED_TAX") == "rtie:vec:OFSERM:CS_DEFERRED_TAX"
    assert vs._doc_key("OFSMDM", "FN_LOAD_OPS_RISK_DATA") == "rtie:vec:OFSMDM:FN_LOAD_OPS_RISK_DATA"


def test_build_filter_clause_no_filters_returns_match_all():
    assert VectorStore._build_filter_clause(None, None) == "*"


def test_build_filter_clause_schema_only():
    assert (
        VectorStore._build_filter_clause(module_filter=None, schema_filter="OFSERM")
        == "@schema:{OFSERM}"
    )


def test_build_filter_clause_module_only_preserves_phase1_behaviour():
    assert (
        VectorStore._build_filter_clause(module_filter="ABL_CAR_CSTM_V4", schema_filter=None)
        == "@module:{ABL_CAR_CSTM_V4}"
    )


def test_build_filter_clause_combines_schema_and_module_with_and():
    """Two TAG filters → space-separated AND clause."""
    clause = VectorStore._build_filter_clause(
        module_filter="ABL_CAR_CSTM_V4",
        schema_filter="OFSERM",
    )
    assert clause == "@schema:{OFSERM} @module:{ABL_CAR_CSTM_V4}"


def test_schema_field_constant_matches_redisearch_attribute():
    """The constant the indexer + search both rely on stays in sync."""
    assert VectorStore.SCHEMA_FIELD == "schema"


# ---------------------------------------------------------------------------
# W146 — search() must honor top_k beyond RediSearch's default LIMIT 10
#
# Pre-W146 the Query() was built without .paging(0, top_k), so RediSearch's
# default LIMIT (offset 0, num 10) silently capped result sets. Invisible
# pre-W122a because function-name-token repetition put the named target at
# KNN rank ≤10 reliably; W122a's name-redundancy removal exposed the cap
# at corpus scale (the C1 canary placed the target at rank #45 in pure
# KNN with the new descriptions, and the 10-cap was hiding it from the
# explainer entirely — W76 anchor injection was compensating downstream).
# ---------------------------------------------------------------------------


def _build_fake_doc(idx: int):
    """Construct one RediSearch-style result doc with all the fields the
    search() method's hits-builder reads."""
    doc = MagicMock()
    doc.function_name = f"FN_{idx}".encode()
    doc.schema = b"OFSERM"
    doc.module = b"TEST_MOD"
    doc.description = f"desc-{idx}".encode()
    doc.tables_read = b"T1,T2"
    doc.tables_written = b"T3"
    doc.key_columns = b"C1,C2"
    doc.score = "0.5"
    return doc


@pytest.mark.asyncio
async def test_search_returns_more_than_10_results_when_top_k_above_10():
    """W146 regression. With top_k=35 and 35 fake docs in the mocked
    RediSearch reply, search() must return all 35 — not 10.
    """
    vs = VectorStore(host="localhost", port=6379)
    vs._client = MagicMock()
    fake_results = MagicMock()
    fake_results.docs = [_build_fake_doc(i) for i in range(35)]

    fake_ft = MagicMock()
    fake_ft.search = AsyncMock(return_value=fake_results)
    vs._client.ft = MagicMock(return_value=fake_ft)

    # 1536-float zero vector — content doesn't matter, search() just
    # serializes it into the query_params blob.
    qv = [0.0] * 1536
    hits = await vs.search(query_embedding=qv, top_k=35)

    assert len(hits) == 35, (
        f"Expected 35 hits when top_k=35 and the RediSearch reply has "
        f"35 docs; got {len(hits)}. Pre-W146 this was always 10 because "
        f"Query() was built without .paging(0, top_k)."
    )

    # And confirm the Query that was sent to RediSearch carried paging
    # set to (0, top_k). We inspect the Query object on the search call.
    call_args = fake_ft.search.call_args
    query_obj = call_args.args[0]
    # The redis-py Query class exposes the paging values as `_offset`
    # and `_num` (internal but stable enough for a regression test).
    assert getattr(query_obj, "_offset", 0) == 0, (
        "search() must set paging offset to 0"
    )
    assert getattr(query_obj, "_num", 10) == 35, (
        f"search() must set paging num to top_k (35), got "
        f"{getattr(query_obj, '_num', None)}"
    )


@pytest.mark.asyncio
async def test_search_top_k_below_10_unchanged_behavior():
    """W146 must not change behavior for callers passing the common
    small top_k values (≤10) — they still receive up to top_k results.
    """
    vs = VectorStore(host="localhost", port=6379)
    vs._client = MagicMock()
    fake_results = MagicMock()
    fake_results.docs = [_build_fake_doc(i) for i in range(3)]
    fake_ft = MagicMock()
    fake_ft.search = AsyncMock(return_value=fake_results)
    vs._client.ft = MagicMock(return_value=fake_ft)

    hits = await vs.search(query_embedding=[0.0] * 1536, top_k=3)
    assert len(hits) == 3
    query_obj = fake_ft.search.call_args.args[0]
    assert getattr(query_obj, "_num", None) == 3
