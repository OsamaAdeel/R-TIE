"""W80b — per-query-type top-K routing for vector retrieval.

Pre-W80b the vector search call hardcoded ``top_k=5`` at every site,
which truncated dense semantic clusters (significant-investment has
15 functions in OFSERM, but only the top 5 ever reached the consumer).
W80b raises top_k for query types that benefit from recall —
VARIABLE_TRACE (multi-stage chain traces) and COLUMN_LOGIC (column
may have many writers) — while keeping FUNCTION_LOGIC narrow because
that route is anchored upstream.

These tests pin the lookup table and the dict.get() fallback behavior
so a future ticket can't silently downgrade a query type without
breaking a test.
"""

from __future__ import annotations

import pytest

from src.agents.retrieval_config import (
    W80B_DEFAULT_TOP_K,
    W80B_TOP_K_BY_QUERY_TYPE,
    resolve_top_k,
)


# ---------------------------------------------------------------------------
# Lookup table values — direct dict probes
# ---------------------------------------------------------------------------

class TestLookupTable:
    def test_function_logic_uses_top_k_5(self):
        """Anchored upstream by W76 / BI routing / W87. One function is
        the answer; raising top_k only adds narrative-LLM noise.
        """
        assert resolve_top_k("FUNCTION_LOGIC") == 5

    def test_column_logic_uses_top_k_15(self):
        """A column can have many writers across a dense schema; raise
        to surface the full writer set rather than truncating at 5.
        """
        assert resolve_top_k("COLUMN_LOGIC") == 15

    def test_variable_trace_uses_top_k_20(self):
        """Multi-stage chain traces visit many functions. 20 is the
        floor that covers the 15-function significant-investment
        cluster with headroom.
        """
        assert resolve_top_k("VARIABLE_TRACE") == 20

    def test_value_trace_uses_default_top_k(self):
        """Phase 2 row-first path — vector search is advisory at best,
        not load-bearing. Keeps the same floor as FUNCTION_LOGIC.
        """
        assert resolve_top_k("VALUE_TRACE") == W80B_DEFAULT_TOP_K

    def test_difference_explanation_uses_default_top_k(self):
        """Phase 2 row-first path; same rationale as VALUE_TRACE."""
        assert resolve_top_k("DIFFERENCE_EXPLANATION") == W80B_DEFAULT_TOP_K

    def test_data_query_uses_default_top_k(self):
        """Option A — uses schema catalogs and SQL generation, not
        vector retrieval. The top_k value is irrelevant on this path
        but should still resolve cleanly to a default.
        """
        assert resolve_top_k("DATA_QUERY") == W80B_DEFAULT_TOP_K

    def test_unsupported_uses_default_top_k(self):
        """Short-circuits before vector search runs. Same default."""
        assert resolve_top_k("UNSUPPORTED") == W80B_DEFAULT_TOP_K


# ---------------------------------------------------------------------------
# Dict.get fallback behavior — must never raise KeyError
# ---------------------------------------------------------------------------

class TestFallbackBehavior:
    def test_top_k_lookup_is_dict_get_not_subscript(self):
        """An unknown query_type (e.g. a future W-ticket adds a new
        type before this config is updated) must NOT raise KeyError.
        Pin the dict.get() shape over direct subscript.
        """
        # If implementation switches to W80B_TOP_K_BY_QUERY_TYPE[qt]
        # this test will fail. Both branches matter: empty string AND
        # genuine unknown.
        assert resolve_top_k("MADE_UP_FUTURE_TYPE_XYZ") == W80B_DEFAULT_TOP_K

    def test_none_query_type_falls_to_default(self):
        """state.get('query_type') can return None early in the
        stream (before classification stamps it). The lookup must
        not raise on None.
        """
        assert resolve_top_k(None) == W80B_DEFAULT_TOP_K

    def test_empty_string_query_type_falls_to_default(self):
        """An empty-but-present query_type (e.g. a defensive
        state['query_type'] = '' init) must fall to the default.
        """
        assert resolve_top_k("") == W80B_DEFAULT_TOP_K


# ---------------------------------------------------------------------------
# Configuration integrity — guard against accidental downgrades
# ---------------------------------------------------------------------------

class TestConfigInvariants:
    def test_default_top_k_is_5(self):
        """The default matches the pre-W80b hardcoded value, so any
        query_type not yet promoted keeps the legacy recall floor.
        """
        assert W80B_DEFAULT_TOP_K == 5

    def test_variable_trace_exceeds_function_logic(self):
        """The asymmetry is structural — chain queries need higher
        recall than anchored queries. Catches accidental flattening
        of the table (e.g. a copy-paste setting all values to 5).
        """
        assert (
            W80B_TOP_K_BY_QUERY_TYPE["VARIABLE_TRACE"]
            > W80B_TOP_K_BY_QUERY_TYPE["FUNCTION_LOGIC"]
        )

    def test_variable_trace_covers_significant_investment_cluster(self):
        """The significant-investment OFSERM cluster has 15 functions
        (verified via FT.SEARCH on '@description:(significant investment)').
        VARIABLE_TRACE top_k must give us headroom over the cluster
        size so the W80 v1 canary can surface beyond the floor.
        """
        assert W80B_TOP_K_BY_QUERY_TYPE["VARIABLE_TRACE"] >= 15

    def test_all_entity_seeking_types_are_present(self):
        """The three entity-seeking types (FUNCTION_LOGIC,
        COLUMN_LOGIC, VARIABLE_TRACE) all need explicit entries
        because they each have different recall needs. Missing one
        would silently fall through to the default and undermine
        the per-type tuning.
        """
        for qt in ("FUNCTION_LOGIC", "COLUMN_LOGIC", "VARIABLE_TRACE"):
            assert qt in W80B_TOP_K_BY_QUERY_TYPE, (
                f"Entity-seeking type {qt!r} missing from config — "
                f"would silently fall to default and lose per-type tuning"
            )
