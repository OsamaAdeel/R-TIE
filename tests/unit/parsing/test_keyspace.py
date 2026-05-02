"""Unit tests for SchemaAwareKeyspace (W35 Phase 1 Step 1).

Locks in the Redis key strings so that:
- changing them later forces an update to the test (which forces an
  update to the diagnostic doc and Redis migration plan), and
- every consumer that constructs keys via this helper produces the
  same string the loader/store wrote them under.
"""

import pytest

from src.parsing.keyspace import SchemaAwareKeyspace as K


# ---------------------------------------------------------------------------
# graph_key
# ---------------------------------------------------------------------------

class TestGraphKey:
    def test_format_ofserm(self):
        assert K.graph_key("OFSERM", "CS_DEFERRED_TAX") == "graph:OFSERM:CS_DEFERRED_TAX"

    def test_format_ofsmdm(self):
        assert (
            K.graph_key("OFSMDM", "FN_LOAD_OPS_RISK_DATA")
            == "graph:OFSMDM:FN_LOAD_OPS_RISK_DATA"
        )

    def test_matches_existing_store_layout(self):
        # Mirrors src/parsing/store.py:REDIS_KEYS["function_graph"].
        from src.parsing.store import REDIS_KEYS
        expected = REDIS_KEYS["function_graph"].format(schema="OFSERM", function_name="X")
        assert K.graph_key("OFSERM", "X") == expected

    def test_empty_schema_raises(self):
        with pytest.raises(ValueError):
            K.graph_key("", "FN_X")

    def test_empty_function_raises(self):
        with pytest.raises(ValueError):
            K.graph_key("OFSERM", "")

    def test_none_schema_raises(self):
        with pytest.raises(ValueError):
            K.graph_key(None, "FN_X")  # type: ignore[arg-type]

    def test_none_function_raises(self):
        with pytest.raises(ValueError):
            K.graph_key("OFSERM", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# graph_index_key
# ---------------------------------------------------------------------------

class TestGraphIndexKey:
    def test_format(self):
        assert K.graph_index_key("OFSERM") == "graph:index:OFSERM"
        assert K.graph_index_key("OFSMDM") == "graph:index:OFSMDM"

    def test_matches_existing_store_layout(self):
        from src.parsing.store import REDIS_KEYS
        expected = REDIS_KEYS["column_index"].format(schema="OFSERM")
        assert K.graph_index_key("OFSERM") == expected

    def test_empty_schema_raises(self):
        with pytest.raises(ValueError):
            K.graph_index_key("")


# ---------------------------------------------------------------------------
# graph_full_key
# ---------------------------------------------------------------------------

class TestGraphFullKey:
    def test_format(self):
        assert K.graph_full_key("OFSERM") == "graph:full:OFSERM"

    def test_matches_existing_store_layout(self):
        from src.parsing.store import REDIS_KEYS
        expected = REDIS_KEYS["full_graph"].format(schema="OFSMDM")
        assert K.graph_full_key("OFSMDM") == expected

    def test_empty_schema_raises(self):
        with pytest.raises(ValueError):
            K.graph_full_key("")


# ---------------------------------------------------------------------------
# source_key
# ---------------------------------------------------------------------------

class TestSourceKey:
    def test_format(self):
        assert K.source_key("OFSERM", "CS_X") == "graph:source:OFSERM:CS_X"

    def test_matches_existing_store_layout(self):
        from src.parsing.store import REDIS_KEYS
        expected = REDIS_KEYS["raw_source"].format(schema="OFSERM", function_name="CS_X")
        assert K.source_key("OFSERM", "CS_X") == expected

    def test_empty_function_raises(self):
        with pytest.raises(ValueError):
            K.source_key("OFSERM", "")


# ---------------------------------------------------------------------------
# graph_aliases_key
# ---------------------------------------------------------------------------

class TestGraphAliasesKey:
    def test_format(self):
        assert K.graph_aliases_key("OFSERM") == "graph:aliases:OFSERM"
        assert K.graph_aliases_key("OFSMDM") == "graph:aliases:OFSMDM"

    def test_matches_existing_store_layout(self):
        from src.parsing.store import REDIS_KEYS
        expected = REDIS_KEYS["alias_map"].format(schema="OFSERM")
        assert K.graph_aliases_key("OFSERM") == expected

    def test_empty_schema_raises(self):
        with pytest.raises(ValueError):
            K.graph_aliases_key("")


# ---------------------------------------------------------------------------
# graph_prefix
# ---------------------------------------------------------------------------

class TestGraphPrefix:
    def test_format(self):
        assert K.graph_prefix("OFSERM") == "graph:OFSERM:"
        assert K.graph_prefix("OFSMDM") == "graph:OFSMDM:"

    def test_no_trailing_wildcard(self):
        # graph_prefix is the prefix used to strip from SCAN results.
        # graph_scan_pattern is the version with a trailing '*'.
        assert not K.graph_prefix("OFSERM").endswith("*")

    def test_can_recover_function_name_via_slice(self):
        # The prefix is used to slice the function name out of raw keys.
        prefix = K.graph_prefix("OFSERM")
        key = K.graph_key("OFSERM", "CS_DEFERRED_TAX")
        assert key.startswith(prefix)
        assert key[len(prefix):] == "CS_DEFERRED_TAX"

    def test_empty_schema_raises(self):
        with pytest.raises(ValueError):
            K.graph_prefix("")


# ---------------------------------------------------------------------------
# graph_scan_pattern
# ---------------------------------------------------------------------------

class TestGraphScanPattern:
    def test_format(self):
        assert K.graph_scan_pattern("OFSERM") == "graph:OFSERM:*"
        assert K.graph_scan_pattern("OFSMDM") == "graph:OFSMDM:*"

    def test_is_prefix_plus_star(self):
        # Documents the explicit relationship between the two helpers.
        for schema in ("OFSERM", "OFSMDM"):
            assert K.graph_scan_pattern(schema) == K.graph_prefix(schema) + "*"

    def test_does_not_collide_with_family_keys(self):
        # The pattern matches per-function keys but not family keys whose
        # second segment is reserved.
        import fnmatch
        pattern = K.graph_scan_pattern("OFSERM")
        # per-fn key MATCHES
        assert fnmatch.fnmatchcase(K.graph_key("OFSERM", "CS_X"), pattern)
        # family keys DO NOT match (different second segment)
        assert not fnmatch.fnmatchcase(K.graph_full_key("OFSERM"), pattern)
        assert not fnmatch.fnmatchcase(K.graph_index_key("OFSERM"), pattern)
        assert not fnmatch.fnmatchcase(K.graph_aliases_key("OFSERM"), pattern)
        # graph:meta:OFSERM:X has a different second segment ("meta") so
        # the SCAN match against `graph:OFSERM:*` does not catch it.
        assert not fnmatch.fnmatchcase("graph:meta:OFSERM:CS_X", pattern)

    def test_empty_schema_raises(self):
        with pytest.raises(ValueError):
            K.graph_scan_pattern("")


# ---------------------------------------------------------------------------
# literal_key (Phase 5)
# ---------------------------------------------------------------------------

class TestLiteralKey:
    def test_format(self):
        assert K.literal_key("OFSERM", "CAP943") == "graph:literal:OFSERM:CAP943"
        assert K.literal_key("OFSMDM", "CAP973") == "graph:literal:OFSMDM:CAP973"

    def test_distinct_per_schema(self):
        # Cross-schema literals (same identifier in two schemas) get two
        # separate keys, matching the Phase 5 prompt requirement.
        a = K.literal_key("OFSERM", "CAP943")
        b = K.literal_key("OFSMDM", "CAP943")
        assert a != b

    def test_empty_schema_raises(self):
        with pytest.raises(ValueError):
            K.literal_key("", "CAP943")

    def test_empty_identifier_raises(self):
        with pytest.raises(ValueError):
            K.literal_key("OFSERM", "")

    def test_none_schema_raises(self):
        with pytest.raises(ValueError):
            K.literal_key(None, "CAP943")  # type: ignore[arg-type]

    def test_none_identifier_raises(self):
        with pytest.raises(ValueError):
            K.literal_key("OFSERM", None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# logic_cache_key was retired in Phase 8 (rtie:logic: cache unification).
# graph:source: is now the sole source-of-source. See SchemaAwareKeyspace.
# ---------------------------------------------------------------------------


def test_logic_cache_key_is_removed():
    """Regression: the legacy helper must not be reintroduced."""
    assert not hasattr(K, "logic_cache_key")


# ---------------------------------------------------------------------------
# origins_key
# ---------------------------------------------------------------------------

class TestOriginsKey:
    def test_no_parts(self):
        assert K.origins_key("OFSERM") == "graph:origins:OFSERM"

    def test_with_one_part(self):
        assert K.origins_key("OFSERM", "etl") == "graph:origins:OFSERM:etl"

    def test_with_multiple_parts(self):
        assert K.origins_key("OFSERM", "etl", "T24") == "graph:origins:OFSERM:etl:T24"

    def test_empty_schema_raises(self):
        with pytest.raises(ValueError):
            K.origins_key("")

    def test_empty_part_raises(self):
        with pytest.raises(ValueError):
            K.origins_key("OFSERM", "etl", "")


# ---------------------------------------------------------------------------
# parse_graph_key
# ---------------------------------------------------------------------------

class TestParseGraphKey:
    def test_round_trip_ofserm(self):
        key = K.graph_key("OFSERM", "CS_DEFERRED_TAX")
        assert K.parse_graph_key(key) == ("OFSERM", "CS_DEFERRED_TAX")

    def test_round_trip_ofsmdm(self):
        key = K.graph_key("OFSMDM", "FN_LOAD_OPS_RISK_DATA")
        assert K.parse_graph_key(key) == ("OFSMDM", "FN_LOAD_OPS_RISK_DATA")

    def test_meta_key_returns_none(self):
        # graph:meta:<schema>:<fn> is parse_metadata, not the per-function graph.
        assert K.parse_graph_key("graph:meta:OFSERM:CS_X") is None

    def test_full_key_returns_none(self):
        assert K.parse_graph_key("graph:full:OFSERM") is None

    def test_index_key_returns_none(self):
        assert K.parse_graph_key("graph:index:OFSERM") is None

    def test_source_key_returns_none(self):
        assert K.parse_graph_key("graph:source:OFSERM:CS_X") is None

    def test_aliases_key_returns_none(self):
        assert K.parse_graph_key("graph:aliases:OFSERM") is None

    def test_origins_key_returns_none(self):
        assert K.parse_graph_key("graph:origins:OFSERM") is None
        assert K.parse_graph_key("graph:origins:OFSERM:etl:T24") is None

    def test_literal_key_returns_none(self):
        # Phase 5 business-identifier index — not a per-function graph key.
        assert K.parse_graph_key("graph:literal:OFSERM:CAP943") is None

    def test_non_graph_prefix_returns_none(self):
        assert K.parse_graph_key("rtie:logic:OFSMDM:FN_X") is None
        assert K.parse_graph_key("hierarchy:ABL_CAR_CSTM_V4") is None
        assert K.parse_graph_key("rtie:vec:OFSMDM:FN_X") is None

    def test_empty_string_returns_none(self):
        assert K.parse_graph_key("") is None

    def test_too_few_segments_returns_none(self):
        # graph:OFSERM by itself is malformed — only 2 segments.
        assert K.parse_graph_key("graph:OFSERM") is None

    def test_just_prefix_returns_none(self):
        assert K.parse_graph_key("graph:") is None

    def test_non_string_returns_none(self):
        assert K.parse_graph_key(None) is None  # type: ignore[arg-type]
        assert K.parse_graph_key(123) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# normalize_function_name
# ---------------------------------------------------------------------------

class TestNormalizeFunctionName:
    def test_lowercase_to_uppercase(self):
        assert K.normalize_function_name("cs_deferred_tax") == "CS_DEFERRED_TAX"

    def test_mixed_case(self):
        # On-disk file is `CS_Deferred_Tax_Asset_Net_of_DTL_Calculation.sql`;
        # Redis key is uppercased.
        assert (
            K.normalize_function_name("CS_Deferred_Tax_Asset_Net_of_DTL_Calculation")
            == "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"
        )

    def test_already_normalized_passes_through(self):
        assert (
            K.normalize_function_name("FN_LOAD_OPS_RISK_DATA") == "FN_LOAD_OPS_RISK_DATA"
        )

    def test_spaces_become_underscores(self):
        assert K.normalize_function_name("BASEL III CAPITAL") == "BASEL_III_CAPITAL"

    def test_multiple_spaces_collapse_to_single_underscore(self):
        assert K.normalize_function_name("BASEL   III  CAPITAL") == "BASEL_III_CAPITAL"

    def test_tab_treated_as_whitespace(self):
        assert K.normalize_function_name("BASEL\tIII") == "BASEL_III"

    def test_leading_trailing_whitespace_stripped(self):
        assert K.normalize_function_name("  fn_x  ") == "FN_X"

    def test_phase0_duplicate_pair_collapses(self):
        # Diagnostic Section 2.5 issue #2: these two surface forms produced
        # two distinct Redis keys. Normalization must collapse them.
        a = K.normalize_function_name(
            "BASEL_III_CAPITAL_CONSOLIDATION_APPROACH_TYPE_RECLASSIFICATION_FOR_AN_ENTITY"
        )
        b = K.normalize_function_name(
            "BASEL III CAPITAL CONSOLIDATION APPROACH TYPE RECLASSIFICATION FOR AN ENTITY"
        )
        assert a == b
        assert (
            a == "BASEL_III_CAPITAL_CONSOLIDATION_APPROACH_TYPE_RECLASSIFICATION_FOR_AN_ENTITY"
        )

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            K.normalize_function_name("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            K.normalize_function_name("   ")

    def test_non_string_raises(self):
        with pytest.raises(ValueError):
            K.normalize_function_name(None)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            K.normalize_function_name(123)  # type: ignore[arg-type]
