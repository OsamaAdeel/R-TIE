"""W35 Phase 7 — business-identifier (BI) routing.

Covers the orchestrator helpers introduced for the CAP-code routing fix
(W36) and the deterministic derivation banner the logic explainer
prepends when a BI-routed query has a Phase 6 derivation summary.

These are pure-function tests: detection is regex over the user query,
resolution is keyed off the in-memory literal index (we monkey-patch
``get_literal_index`` so no Redis is required), and the banner renderer
reads only ``state["bi_routing"]``.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.agents import orchestrator as orch_mod
from src.agents.orchestrator import (
    apply_bi_routing,
    detect_business_identifiers,
    resolve_bi_to_function,
)
from src.agents.logic_explainer import render_derivation_header


# ---------------------------------------------------------------------------
# Sentinel Redis stand-in. resolve_bi_to_function reads through
# get_literal_index(redis_client, schema, identifier); we monkey-patch
# get_literal_index in the orchestrator module so any non-None value
# satisfies the redis_client check.
# ---------------------------------------------------------------------------

_SENTINEL_REDIS = object()


# ===========================================================================
# detect_business_identifiers
# ===========================================================================

class TestDetectBusinessIdentifiers:
    """Reuse Phase 5's ``CAP\\d{3}`` pattern; case-sensitive; ordered."""

    def test_single_cap_code(self):
        assert detect_business_identifiers("How is CAP943 calculated?") == ["CAP943"]

    def test_multiple_cap_codes_ordered_by_query_position(self):
        # First-occurrence-wins, sorted by position in raw_query.
        assert detect_business_identifiers(
            "Compare CAP943 with CAP309"
        ) == ["CAP943", "CAP309"]

    def test_lowercase_does_not_match(self):
        # CAP-codes are uppercase by convention; lowercase form is NOT
        # an identifier.
        assert detect_business_identifiers("How does cap973 work?") == []

    def test_no_cap_code_returns_empty(self):
        assert detect_business_identifiers(
            "What is the FCT_STD_ACCT_HEAD value?"
        ) == []

    def test_empty_query(self):
        assert detect_business_identifiers("") == []

    def test_duplicates_collapsed_first_occurrence_wins(self):
        # Same identifier mentioned twice — appears once, at first
        # position.
        assert detect_business_identifiers(
            "CAP943 then CAP309 then CAP943 again"
        ) == ["CAP943", "CAP309"]

    def test_no_partial_matches_inside_larger_token(self):
        # Word-boundary anchoring prevents matching inside a longer
        # alphanumeric run.
        assert detect_business_identifiers("XCAP943Y is a token") == []

    def test_explicit_pattern_config_overrides_default(self):
        patterns = {"abl_codes": {"regex": r"ABL\d{3}"}}
        # Default CAP\d{3} pattern is replaced — CAP-codes no longer
        # match, ABL-codes do.
        assert detect_business_identifiers("CAP943 vs ABL013", patterns) == ["ABL013"]

    def test_empty_pattern_config_falls_back_to_default(self):
        # An empty dict / None means "use the default CAP\d{3} pattern".
        assert detect_business_identifiers("CAP943 question", {}) == ["CAP943"]
        assert detect_business_identifiers("CAP943 question", None) == ["CAP943"]


# ===========================================================================
# resolve_bi_to_function
# ===========================================================================

def _patched_index(monkeypatch, table: Dict[str, Dict[str, List[Dict[str, Any]]]]):
    """Patch get_literal_index in the orchestrator + schema_discovery so
    resolve_bi_to_function reads from *table* instead of Redis.

    Shape: ``{schema: {identifier: [records]}}``. discovered_schemas is
    also patched so it returns the keys of *table* in deterministic
    order.
    """
    def fake_get_literal_index(_redis, schema, identifier):
        return (table.get(schema) or {}).get(identifier)

    def fake_discovered_schemas(_redis):
        return sorted(table.keys())

    monkeypatch.setattr(orch_mod, "get_literal_index", fake_get_literal_index)
    monkeypatch.setattr(orch_mod, "discovered_schemas", fake_discovered_schemas)


class TestResolveBiToFunction:
    """Picks the highest-priority record per role, with derivation
    preferred over no-derivation at the case_when_target tier."""

    def test_case_when_target_with_derivation_wins(self, monkeypatch):
        # CAP943 setup mirrors live Redis: 7 records, the first
        # case_when_target carries an embedded derivation.
        derivation = {
            "operation": "SUBTRACT",
            "source_literals": ["CAP309", "CAP863"],
            "target_column": "N_STD_ACCT_HEAD_AMT",
        }
        records = [
            {
                "function": "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
                "line": 24, "role": "case_when_target",
                "derivation": dict(derivation),
            },
            {
                "function": "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
                "line": 24, "role": "filter",
            },
            {
                "function": "CS_PHASE_IN_TREATMENT_RW_ASSIGNMENT",
                "line": 24, "role": "case_when_target",
            },
            {
                "function": "REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP",
                "line": 24, "role": "in_list_member",
            },
        ]
        _patched_index(monkeypatch, {"OFSERM": {"CAP943": records}})

        resolved = resolve_bi_to_function("CAP943", _SENTINEL_REDIS)
        assert resolved is not None
        assert resolved["function"] == "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"
        assert resolved["schema"] == "OFSERM"
        assert resolved["role"] == "case_when_target"
        assert resolved["derivation"] == derivation
        # candidates list contains every record (ranked).
        assert len(resolved["candidates"]) == 4

    def test_case_when_target_without_derivation(self, monkeypatch):
        # CAP973 has a case_when_target on the COMPUTER but no
        # derivation extracted; routing still flips off the loader.
        records = [
            {
                "function": "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT",
                "line": 24, "role": "case_when_target",
            },
            {
                "function": "REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP",
                "line": 24, "role": "in_list_member",
            },
        ]
        _patched_index(monkeypatch, {"OFSERM": {"CAP973": records}})

        resolved = resolve_bi_to_function("CAP973", _SENTINEL_REDIS)
        assert resolved is not None
        assert resolved["function"] == "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT"
        assert resolved["role"] == "case_when_target"
        assert resolved["derivation"] is None

    def test_case_when_source_picked_when_no_target(self, monkeypatch):
        # CAP309 in live Redis: only case_when_source records exist.
        records = [
            {
                "function": "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
                "line": 24, "role": "case_when_source",
            },
        ]
        _patched_index(monkeypatch, {"OFSERM": {"CAP309": records}})

        resolved = resolve_bi_to_function("CAP309", _SENTINEL_REDIS)
        assert resolved is not None
        assert resolved["role"] == "case_when_source"

    def test_filter_only_picks_filter(self, monkeypatch):
        records = [
            {
                "function": "FCT_CAP_CONS_RATIO_POP",
                "line": 24, "role": "filter",
            },
        ]
        _patched_index(monkeypatch, {"OFSERM": {"CAPxxx": records}})

        resolved = resolve_bi_to_function("CAPxxx", _SENTINEL_REDIS)
        assert resolved is not None
        assert resolved["role"] == "filter"

    def test_unknown_identifier_returns_none(self, monkeypatch):
        _patched_index(monkeypatch, {"OFSERM": {}})
        assert resolve_bi_to_function("CAP999", _SENTINEL_REDIS) is None

    def test_schema_restriction_excludes_other_schemas(self, monkeypatch):
        # CAP943 only exists in OFSERM. Restricting to OFSMDM yields None.
        records = [
            {
                "function": "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
                "line": 24, "role": "case_when_target",
            },
        ]
        _patched_index(monkeypatch, {"OFSERM": {"CAP943": records}})

        assert resolve_bi_to_function(
            "CAP943", _SENTINEL_REDIS, schemas=["OFSMDM"]
        ) is None

    def test_tie_break_by_function_name_then_line(self, monkeypatch):
        # Two case_when_target records, neither with a derivation —
        # alphabetical function name wins.
        records = [
            {"function": "ZZZ_LATER", "line": 5, "role": "case_when_target"},
            {"function": "AAA_EARLIER", "line": 99, "role": "case_when_target"},
        ]
        _patched_index(monkeypatch, {"OFSERM": {"CAP500": records}})
        resolved = resolve_bi_to_function("CAP500", _SENTINEL_REDIS)
        assert resolved is not None
        assert resolved["function"] == "AAA_EARLIER"

    def test_redis_none_returns_none(self):
        assert resolve_bi_to_function("CAP943", None) is None

    def test_empty_identifier_returns_none(self, monkeypatch):
        _patched_index(monkeypatch, {"OFSERM": {}})
        assert resolve_bi_to_function("", _SENTINEL_REDIS) is None


# ===========================================================================
# apply_bi_routing
# ===========================================================================

def _state(
    query_type: str,
    raw_query: str,
    target_variable: str = "",
) -> Dict[str, Any]:
    """Minimal LogicState dict for routing tests."""
    return {
        "raw_query": raw_query,
        "query_type": query_type,
        "object_name": "",
        "schema": "",
        "target_variable": target_variable,
    }


class TestApplyBiRouting:
    """End-to-end orchestration of the BI routing decision."""

    def test_fires_on_column_logic_with_cap_code(self, monkeypatch):
        records = [
            {
                "function": "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
                "line": 24, "role": "case_when_target",
                "derivation": {
                    "operation": "SUBTRACT",
                    "source_literals": ["CAP309", "CAP863"],
                    "target_column": "N_STD_ACCT_HEAD_AMT",
                },
            },
        ]
        _patched_index(monkeypatch, {"OFSERM": {"CAP943": records}})
        # function_exists_in_graph and extract_function_candidates run
        # against Redis directly; with no functions named in the query
        # they return empty/False even when redis is mocked.
        monkeypatch.setattr(
            orch_mod, "function_exists_in_graph",
            lambda _name, _redis, schemas=None: False,
        )

        state = _state("COLUMN_LOGIC", "How is CAP943 calculated?")
        apply_bi_routing(state, state["raw_query"], _SENTINEL_REDIS)

        bi = state.get("bi_routing")
        assert bi is not None
        assert bi["identifier"] == "CAP943"
        assert bi["function"] == "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"
        assert bi["schema"] == "OFSERM"
        assert bi["role"] == "case_when_target"
        assert bi["derivation"]["operation"] == "SUBTRACT"
        # State stamped for downstream retrieval.
        assert state["object_name"] == bi["function"]
        assert state["schema"] == "OFSERM"

    def test_fires_on_function_logic_alias(self, monkeypatch):
        # FUNCTION_LOGIC is the forward-compat alias kept in
        # _REQUIRES_CITATIONS; BI must fire here too.
        records = [{"function": "X", "line": 1, "role": "case_when_target"}]
        _patched_index(monkeypatch, {"OFSERM": {"CAP500": records}})
        monkeypatch.setattr(
            orch_mod, "function_exists_in_graph",
            lambda _name, _redis, schemas=None: False,
        )

        state = _state("FUNCTION_LOGIC", "How is CAP500 derived?")
        apply_bi_routing(state, state["raw_query"], _SENTINEL_REDIS)
        assert state.get("bi_routing", {}).get("function") == "X"

    def test_variable_trace_with_non_bi_target_does_not_fire(self, monkeypatch):
        # W36 follow-up: VARIABLE_TRACE with a non-CAP target must NOT
        # fire BI routing — only CAP-code-shaped targets should.
        records = [{"function": "X", "line": 1, "role": "case_when_target"}]
        _patched_index(monkeypatch, {"OFSERM": {"CAP500": records}})

        state = _state(
            "VARIABLE_TRACE", "what writes N_EOP_BAL",
            target_variable="N_EOP_BAL",
        )
        apply_bi_routing(state, state["raw_query"], _SENTINEL_REDIS)
        assert not state.get("bi_routing")
        # Query type unchanged — variable-tracer agent runs as before.
        assert state["query_type"] == "VARIABLE_TRACE"
        assert state["object_name"] == ""
        assert state["schema"] == ""

    def test_variable_trace_with_bi_target_fires_and_promotes(self, monkeypatch):
        # W36 follow-up: VARIABLE_TRACE + target_variable="CAP943" gets
        # promoted to FUNCTION_LOGIC so the streaming endpoint emits the
        # derivation banner instead of routing through the variable-tracer.
        derivation = {
            "operation": "SUBTRACT",
            "source_literals": ["CAP309", "CAP863"],
            "target_column": "N_STD_ACCT_HEAD_AMT",
        }
        records = [
            {
                "function": "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
                "line": 24, "role": "case_when_target",
                "derivation": dict(derivation),
            },
        ]
        _patched_index(monkeypatch, {"OFSERM": {"CAP943": records}})
        monkeypatch.setattr(
            orch_mod, "function_exists_in_graph",
            lambda _name, _redis, schemas=None: False,
        )

        state = _state(
            "VARIABLE_TRACE", "How is CAP943 calculated?",
            target_variable="CAP943",
        )
        apply_bi_routing(state, state["raw_query"], _SENTINEL_REDIS)

        # bi_routing populated.
        bi = state.get("bi_routing")
        assert bi is not None
        assert bi["identifier"] == "CAP943"
        assert bi["function"] == "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"
        assert bi["schema"] == "OFSERM"
        assert bi["derivation"] == derivation
        # Query type promoted — happens only on the VARIABLE_TRACE branch.
        assert state["query_type"] == "FUNCTION_LOGIC"
        # Schema pivot — happy by-product of the routing fix; the
        # classifier defaults schema to OFSMDM but CAP943 lives in OFSERM.
        assert state["schema"] == "OFSERM"
        assert state["object_name"] == "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"

    def test_variable_trace_with_unknown_bi_target_does_not_fire(self, monkeypatch):
        # W36 follow-up: target_variable="CAP999" matches the BI pattern
        # but is absent from every literal index. Resolver returns None,
        # state stays untouched (no promotion, no bi_routing).
        _patched_index(monkeypatch, {"OFSERM": {}})  # no CAP999 entry
        monkeypatch.setattr(
            orch_mod, "function_exists_in_graph",
            lambda _name, _redis, schemas=None: False,
        )

        state = _state(
            "VARIABLE_TRACE", "How is CAP999 calculated?",
            target_variable="CAP999",
        )
        apply_bi_routing(state, state["raw_query"], _SENTINEL_REDIS)
        assert not state.get("bi_routing")
        # No promotion when resolver returns None.
        assert state["query_type"] == "VARIABLE_TRACE"
        assert state["object_name"] == ""
        assert state["schema"] == ""

    def test_variable_trace_with_empty_target_does_not_fire(self, monkeypatch):
        # Defensive: VARIABLE_TRACE with no target_variable should fall
        # through silently — no detection, no resolution, no promotion.
        records = [{"function": "X", "line": 1, "role": "case_when_target"}]
        _patched_index(monkeypatch, {"OFSERM": {"CAP500": records}})

        state = _state(
            "VARIABLE_TRACE", "what writes CAP500",
            target_variable="",
        )
        apply_bi_routing(state, state["raw_query"], _SENTINEL_REDIS)
        assert not state.get("bi_routing")
        assert state["query_type"] == "VARIABLE_TRACE"

    def test_skipped_for_data_query(self, monkeypatch):
        records = [{"function": "X", "line": 1, "role": "case_when_target"}]
        _patched_index(monkeypatch, {"OFSERM": {"CAP500": records}})

        state = _state("DATA_QUERY", "What is the value of CAP500?")
        apply_bi_routing(state, state["raw_query"], _SENTINEL_REDIS)
        assert not state.get("bi_routing")

    def test_skipped_when_explicit_function_named(self, monkeypatch):
        # If the user named a function that exists in the indexed corpus,
        # honour their choice over the literal-index lookup.
        records = [{"function": "X", "line": 1, "role": "case_when_target"}]
        _patched_index(monkeypatch, {"OFSERM": {"CAP943": records}})
        monkeypatch.setattr(
            orch_mod, "function_exists_in_graph",
            lambda name, _redis, schemas=None:
                name.upper() == "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
        )

        state = _state(
            "COLUMN_LOGIC",
            "How does CS_Deferred_Tax_Asset_Net_of_DTL_Calculation work?",
        )
        apply_bi_routing(state, state["raw_query"], _SENTINEL_REDIS)
        assert not state.get("bi_routing")

    def test_unknown_identifier_falls_through(self, monkeypatch):
        _patched_index(monkeypatch, {"OFSERM": {}})
        monkeypatch.setattr(
            orch_mod, "function_exists_in_graph",
            lambda _name, _redis, schemas=None: False,
        )

        state = _state("COLUMN_LOGIC", "How is CAP999 calculated?")
        apply_bi_routing(state, state["raw_query"], _SENTINEL_REDIS)
        assert not state.get("bi_routing")
        # State untouched — falls through to existing classification.
        assert state["object_name"] == ""

    def test_redis_none_short_circuits(self):
        state = _state("COLUMN_LOGIC", "How is CAP943 calculated?")
        # No monkey-patch needed: with redis_client=None apply_bi_routing
        # returns immediately without ever touching the index.
        apply_bi_routing(state, state["raw_query"], None)
        assert not state.get("bi_routing")


# ===========================================================================
# render_derivation_header
# ===========================================================================

class TestRenderDerivationHeader:
    """Banner is rendered programmatically — same shape every time."""

    def test_subtract_operation(self):
        state = {
            "bi_routing": {
                "identifier": "CAP943",
                "function": "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION",
                "schema": "OFSERM",
                "role": "case_when_target",
                "derivation": {
                    "operation": "SUBTRACT",
                    "source_literals": ["CAP309", "CAP863"],
                    "target_column": "N_STD_ACCT_HEAD_AMT",
                },
            },
        }
        rendered = render_derivation_header(state)
        assert "## Derivation" in rendered
        assert "**CAP943 = CAP309 - CAP863**" in rendered
        assert "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION" in rendered
        assert "OFSERM" in rendered

    def test_direct_assign_operation(self):
        state = {
            "bi_routing": {
                "identifier": "CAP500",
                "function": "CS_PASSTHROUGH",
                "schema": "OFSERM",
                "role": "case_when_target",
                "derivation": {
                    "operation": "DIRECT_ASSIGN",
                    "source_literals": ["CAP200"],
                    "target_column": "N_STD_ACCT_HEAD_AMT",
                },
            },
        }
        rendered = render_derivation_header(state)
        assert "## Derivation" in rendered
        assert "**CAP500 is assigned the value of CAP200**" in rendered
        assert "is assigned" in rendered

    def test_missing_derivation_returns_empty(self):
        # case_when_target without an embedded derivation summary —
        # routing fired but no banner is rendered.
        state = {
            "bi_routing": {
                "identifier": "CAP829",
                "function": "CS_REQUIRED_BUFFER_FROM_CET1_CAPITAL",
                "schema": "OFSERM",
                "role": "case_when_target",
                "derivation": None,
            },
        }
        assert render_derivation_header(state) == ""

    def test_no_bi_routing_returns_empty(self):
        # State without bi_routing at all.
        assert render_derivation_header({}) == ""
        assert render_derivation_header({"bi_routing": {}}) == ""

    def test_unknown_operation_returns_empty(self):
        state = {
            "bi_routing": {
                "identifier": "CAP943",
                "function": "X",
                "schema": "OFSERM",
                "role": "case_when_target",
                "derivation": {
                    "operation": "MULTIPLY",  # not in OP_DESCRIPTIONS
                    "source_literals": ["A", "B"],
                    "target_column": "C",
                },
            },
        }
        assert render_derivation_header(state) == ""

    def test_subtract_needs_two_operands(self):
        # Defensive: SUBTRACT with only one operand should not render.
        state = {
            "bi_routing": {
                "identifier": "CAP943",
                "function": "X",
                "schema": "OFSERM",
                "role": "case_when_target",
                "derivation": {
                    "operation": "SUBTRACT",
                    "source_literals": ["ONLY_ONE"],
                    "target_column": "C",
                },
            },
        }
        assert render_derivation_header(state) == ""

    def test_section_order_hierarchy_above_derivation(self):
        # Banner content alone — actual ordering vs hierarchy is enforced
        # by main.py's stream emit order. Here we just confirm the banner
        # leads with "## Derivation" so a caller's prepend lands correctly.
        state = {
            "bi_routing": {
                "identifier": "CAP943",
                "function": "F",
                "schema": "OFSERM",
                "role": "case_when_target",
                "derivation": {
                    "operation": "SUBTRACT",
                    "source_literals": ["A", "B"],
                    "target_column": "C",
                },
            },
        }
        rendered = render_derivation_header(state)
        # Header is the first non-blank line.
        first_line = rendered.lstrip().splitlines()[0]
        assert first_line == "## Derivation"
