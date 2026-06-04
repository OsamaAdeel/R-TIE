"""W155 — CAP↔function association gate (fix F-GT-1).

The W130 pre-classifier hook force-routes registry-CAP queries to a
DATA_QUERY aggregate, badged VERIFIED with no function-association check.
For the cap_code ANCHOR arm that lets a query naming an UNRELATED corpus
function alongside a registry CAP bluff a confident number (e.g.
"How is CAP169 calculated in FN_G_TEST_CSTM?" — CAP169 is not computed in
FN_G_TEST_CSTM). W155 gates the force-route on the named function actually
computing/containing the CAP; otherwise it falls through to the classifier.

These are pure-function tests for the two helpers the hook calls:
  * w155_named_functions_in_query — corpus functions named in the query
  * w155_cap_associated_with_named_fn — CAP-in-named-function membership

Redis is stubbed by monkey-patching the orchestrator's store reads
(get_function_graph for existence, get_literal_index + discovered_schemas
for resolution) — mirrors tests/unit/agents/test_phase7_bi_routing.py.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.agents import orchestrator as orch_mod
from src.agents.orchestrator import (
    w155_named_functions_in_query,
    w155_cap_associated_with_named_fn,
)
from src.agents.computation_router import detect_named_computation


_SENTINEL_REDIS = object()


def _patch_graph(monkeypatch, table: Dict[str, Dict[str, List[Dict[str, Any]]]],
                 existing_funcs: set[str]):
    """Patch the orchestrator store reads so the W155 helpers run Redis-free.

    *table* feeds resolve_bi_to_function (shape ``{schema: {cap: [records]}}``);
    *existing_funcs* (upper-cased) is the set of function names that
    function_exists_in_graph should report as present.
    """
    def fake_get_literal_index(_redis, schema, identifier):
        return (table.get(schema) or {}).get(identifier)

    def fake_discovered_schemas(_redis):
        # union of schemas across both index tables, deterministic order
        return sorted(table.keys()) or ["OFSERM"]

    def fake_get_function_graph(_redis, _schema, func_upper):
        return {"_": True} if func_upper in existing_funcs else None

    monkeypatch.setattr(orch_mod, "get_literal_index", fake_get_literal_index)
    monkeypatch.setattr(orch_mod, "discovered_schemas", fake_discovered_schemas)
    monkeypatch.setattr(orch_mod, "get_function_graph", fake_get_function_graph)


# ---------------------------------------------------------------------------
# Registry sanity: the codes W155 scopes to actually carry the expected arm /
# filter_kind, so the hook's `arm == "anchor" and filter_kind == "cap_code"`
# guard fires for them (and NOT for BIA / decline).
# ---------------------------------------------------------------------------

class TestRegistryArmsForGate:
    def test_cap169_is_cap_code_anchor(self):
        m = detect_named_computation("How is CAP169 calculated?", "DATA_QUERY")
        assert m is not None
        assert m.definition.arm == "anchor"
        assert m.definition.filter_kind == "cap_code"
        assert m.definition.filter_code == "CAP169"

    def test_cap960_is_cap_code_anchor(self):
        m = detect_named_computation("How is CAP960 calculated?", "DATA_QUERY")
        assert m is not None
        assert m.definition.arm == "anchor"
        assert m.definition.filter_kind == "cap_code"

    def test_bia_is_method_skey_not_cap_code(self):
        m = detect_named_computation("How is BIA calculated?", "DATA_QUERY")
        assert m is not None
        assert m.definition.arm == "anchor"
        assert m.definition.filter_kind == "method_skey"

    def test_leverage_is_decline_arm(self):
        m = detect_named_computation("How is the Leverage Ratio calculated?", "DATA_QUERY")
        assert m is not None
        assert m.definition.arm == "decline"


# ---------------------------------------------------------------------------
# w155_named_functions_in_query
# ---------------------------------------------------------------------------

class TestNamedFunctionsInQuery:
    def test_real_corpus_function_returned(self, monkeypatch):
        _patch_graph(monkeypatch, {}, existing_funcs={"FN_G_TEST_CSTM"})
        out = w155_named_functions_in_query(
            "How is CAP169 calculated in FN_G_TEST_CSTM?", _SENTINEL_REDIS
        )
        assert out == ["FN_G_TEST_CSTM"]

    def test_nonexistent_function_dropped(self, monkeypatch):
        # KNOWN RESIDUAL: a fake function is not in the graph -> dropped ->
        # empty -> caller skips the gate.
        _patch_graph(monkeypatch, {}, existing_funcs=set())
        out = w155_named_functions_in_query(
            "How is CAP169 calculated in SOME_FAKE_FN?", _SENTINEL_REDIS
        )
        assert out == []

    def test_bare_cap_no_function_named(self, monkeypatch):
        _patch_graph(monkeypatch, {}, existing_funcs={"FN_G_TEST_CSTM"})
        out = w155_named_functions_in_query(
            "How is CAP169 calculated?", _SENTINEL_REDIS
        )
        assert out == []

    def test_redis_none_fails_open_empty(self):
        # function_exists_in_graph returns False for a None client.
        out = w155_named_functions_in_query(
            "How is CAP169 calculated in FN_G_TEST_CSTM?", None
        )
        assert out == []


# ---------------------------------------------------------------------------
# w155_cap_associated_with_named_fn
# ---------------------------------------------------------------------------

class TestCapAssociation:
    def test_unrelated_named_function_not_associated(self, monkeypatch):
        # CAP169 resolves (literal index) to its own POP function, NOT to the
        # named FN_G_TEST_CSTM -> not associated -> caller falls through. This
        # is the F-GT-1 fix.
        records = [
            {"function": "ABL_CAPITAL_SOURCE_STANDARD_ACCT_HEAD_DATA_POP",
             "line": 24, "role": "filter"},
        ]
        _patch_graph(
            monkeypatch,
            {"OFSERM": {"CAP169": records}},
            existing_funcs={"FN_G_TEST_CSTM"},
        )
        assert w155_cap_associated_with_named_fn(
            "CAP169", ["FN_G_TEST_CSTM"], _SENTINEL_REDIS
        ) is False

    def test_own_computing_function_associated(self, monkeypatch):
        # CAP960 named in its own computing function -> associated -> the
        # genuine CAP-in-own-function case is preserved.
        records = [
            {"function": "CS_COMMON_EQUITY_TIER_1_CAPITAL_RATIO",
             "line": 24, "role": "case_when_target"},
            {"function": "SOME_OTHER_POP", "line": 8, "role": "filter"},
        ]
        _patch_graph(
            monkeypatch,
            {"OFSERM": {"CAP960": records}},
            existing_funcs={"CS_COMMON_EQUITY_TIER_1_CAPITAL_RATIO"},
        )
        assert w155_cap_associated_with_named_fn(
            "CAP960", ["CS_COMMON_EQUITY_TIER_1_CAPITAL_RATIO"], _SENTINEL_REDIS
        ) is True

    def test_empty_named_funcs_returns_false_without_resolving(self, monkeypatch):
        # Guard: empty named_funcs short-circuits to False (caller skips gate
        # anyway). resolve_bi_to_function must not even be consulted.
        def _boom(*_a, **_k):
            raise AssertionError("resolve_bi_to_function should not be called")
        monkeypatch.setattr(orch_mod, "get_literal_index", _boom)
        assert w155_cap_associated_with_named_fn(
            "CAP169", [], _SENTINEL_REDIS
        ) is False

    def test_cap_resolves_to_nothing_not_associated(self, monkeypatch):
        # If the CAP has no literal-index records at all, resolve -> None ->
        # empty candidate set -> not associated.
        _patch_graph(
            monkeypatch,
            {"OFSERM": {}},
            existing_funcs={"FN_G_TEST_CSTM"},
        )
        assert w155_cap_associated_with_named_fn(
            "CAP169", ["FN_G_TEST_CSTM"], _SENTINEL_REDIS
        ) is False

    def test_membership_uses_full_candidate_list_not_just_primary(self, monkeypatch):
        # The named function is a lower-priority candidate (filter), not the
        # primary (case_when_target). Membership over the FULL candidate list
        # must still find it -> associated.
        records = [
            {"function": "PRIMARY_TARGET_FN", "line": 24, "role": "case_when_target"},
            {"function": "FN_G_TEST_CSTM", "line": 30, "role": "filter"},
        ]
        _patch_graph(
            monkeypatch,
            {"OFSERM": {"CAP169": records}},
            existing_funcs={"FN_G_TEST_CSTM"},
        )
        assert w155_cap_associated_with_named_fn(
            "CAP169", ["FN_G_TEST_CSTM"], _SENTINEL_REDIS
        ) is True


# ---------------------------------------------------------------------------
# End-to-end gate decision (helper composition the hook performs). This mirrors
# the exact arm/filter_kind + named_funcs + association sequence in
# src/main.py's W130 hook, without importing the FastAPI app.
# ---------------------------------------------------------------------------

def _gate_falls_through(query: str, redis, *, monkeypatch,
                        table=None, existing_funcs=None) -> bool:
    """Replicate the hook's decision: True == w88_pre set to None (fall through)."""
    if table is not None:
        _patch_graph(monkeypatch, table, existing_funcs or set())
    m = detect_named_computation(raw_query=query, query_type="DATA_QUERY")
    if m is None:
        return True  # no W88 match at all -> classifier path
    defn = m.definition
    if not (defn.arm == "anchor" and defn.filter_kind == "cap_code"):
        return False  # gate not in scope -> proceed (force DATA_QUERY)
    named = w155_named_functions_in_query(query, redis)
    if named and not w155_cap_associated_with_named_fn(defn.filter_code, named, redis):
        return True  # fall through
    return False  # proceed


class TestGateDecision:
    def test_registry_cap_plus_unrelated_corpus_fn_falls_through(self, monkeypatch):
        records = [{"function": "ABL_CAPITAL_SOURCE_STANDARD_ACCT_HEAD_DATA_POP",
                    "line": 24, "role": "filter"}]
        assert _gate_falls_through(
            "How is CAP169 calculated in FN_G_TEST_CSTM?", _SENTINEL_REDIS,
            monkeypatch=monkeypatch,
            table={"OFSERM": {"CAP169": records}},
            existing_funcs={"FN_G_TEST_CSTM"},
        ) is True

    def test_registry_cap_plus_own_fn_proceeds(self, monkeypatch):
        records = [{"function": "CS_COMMON_EQUITY_TIER_1_CAPITAL_RATIO",
                    "line": 24, "role": "case_when_target"}]
        assert _gate_falls_through(
            "How is CAP960 calculated in CS_COMMON_EQUITY_TIER_1_CAPITAL_RATIO?",
            _SENTINEL_REDIS, monkeypatch=monkeypatch,
            table={"OFSERM": {"CAP960": records}},
            existing_funcs={"CS_COMMON_EQUITY_TIER_1_CAPITAL_RATIO"},
        ) is False

    def test_bare_registry_cap_no_function_proceeds(self, monkeypatch):
        records = [{"function": "ABL_CAPITAL_SOURCE_STANDARD_ACCT_HEAD_DATA_POP",
                    "line": 24, "role": "filter"}]
        assert _gate_falls_through(
            "How is CAP169 calculated?", _SENTINEL_REDIS, monkeypatch=monkeypatch,
            table={"OFSERM": {"CAP169": records}},
            existing_funcs={"FN_G_TEST_CSTM"},
        ) is False

    def test_decline_arm_gate_skipped(self, monkeypatch):
        # Leverage Ratio is decline arm -> gate not in scope -> proceed even
        # with a corpus function named.
        assert _gate_falls_through(
            "How is the Leverage Ratio calculated in FN_G_TEST_CSTM?",
            _SENTINEL_REDIS, monkeypatch=monkeypatch,
            table={}, existing_funcs={"FN_G_TEST_CSTM"},
        ) is False

    def test_bia_method_skey_gate_skipped(self, monkeypatch):
        # BIA is method_skey -> gate not in scope -> proceed.
        assert _gate_falls_through(
            "How is BIA calculated in FN_G_TEST_CSTM?",
            _SENTINEL_REDIS, monkeypatch=monkeypatch,
            table={}, existing_funcs={"FN_G_TEST_CSTM"},
        ) is False

    def test_graph_redis_none_fails_open_proceeds(self, monkeypatch):
        # _graph_redis None -> named_funcs empty -> gate skipped -> proceed
        # (the bare-CAP aggregate still returns, fail-open).
        assert _gate_falls_through(
            "How is CAP169 calculated in FN_G_TEST_CSTM?", None,
            monkeypatch=monkeypatch,
        ) is False

    def test_nonexistent_named_function_known_residual_proceeds(self, monkeypatch):
        # KNOWN RESIDUAL (W155): fake function not in graph -> named_funcs
        # empty -> gate skipped -> still aggregates. Accepted, not fixed.
        records = [{"function": "ABL_CAPITAL_SOURCE_STANDARD_ACCT_HEAD_DATA_POP",
                    "line": 24, "role": "filter"}]
        assert _gate_falls_through(
            "How is CAP169 calculated in SOME_FAKE_FN?", _SENTINEL_REDIS,
            monkeypatch=monkeypatch,
            table={"OFSERM": {"CAP169": records}},
            existing_funcs=set(),
        ) is False
