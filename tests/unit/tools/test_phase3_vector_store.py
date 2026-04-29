"""Phase 3 — vector store schema TAG, doc-key prefix, and search filter.

Two contracts:

1. Doc keys are now ``rtie:vec:<schema>:<fn>`` (was ``<module>:<fn>``).
   ``upsert_function`` accepts a ``schema`` kwarg, populates the new
   ``schema`` TAG field, and writes under the new prefix.

2. ``search`` accepts an optional ``schema_filter`` that combines with
   the existing ``module_filter`` as an AND clause and yields the
   ``@schema:{...} @module:{...}`` RediSearch pre-filter for KNN.
"""

from __future__ import annotations

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
