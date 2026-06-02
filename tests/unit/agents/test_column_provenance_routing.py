"""Column-provenance routing — extend BI-routing's pre-search lookup to columns.

A query like "How is N_EOP_BAL written / populated?" names a *column*. BI
routing (CAP codes) and the W76 named-function anchor both decline on it, so
pre-fix it fell through to unanchored narrow semantic search and retrieved
functions that never write the column. This pass resolves the column's
WRITER function(s) from the column index, classifies write-direction per-node
from the structured graph, and routes to the VARIABLE_TRACE trace path with
the writer set force-included.

Pure-function tests: the column index, per-function graphs, and discovered
schemas are monkey-patched so no Redis is required. Direction-awareness is the
load-bearing property — a reader-only reference must NEVER anchor.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from src.agents import orchestrator as orch_mod
from src.agents.orchestrator import (
    apply_column_provenance_anchor,
    detect_column_tokens,
    resolve_column_writers,
    _classify_node_operation,
    _column_write_operation,
    _node_target_columns,
)
from src.agents.anchor_resolution import ensure_column_writers_in_search_results


_SENTINEL_REDIS = object()


# ---------------------------------------------------------------------------
# Fixtures: a small corpus mirroring the N_EOP_BAL failure case.
#   - POPULATE_PP_FROMGL      writes N_EOP_BAL (INSERT, target column)
#   - POPULATE_PP_FROMGL_AMC  writes N_EOP_BAL (INSERT, target column)
#   - CAP_LEVERAGE_FN         only READS N_EOP_BAL (it is a source value)
# N_AMOUNT_LCY is a pure source (read) in the writer functions.
# ---------------------------------------------------------------------------

def _insert_node(node_id: str, target_table: str, target_cols: List[str], source_cols: List[str]) -> Dict[str, Any]:
    return {
        "id": node_id,
        "type": "INSERT",
        "target_table": target_table,
        "column_maps": {
            "columns": list(target_cols),
            "values": list(source_cols),
            "mapping": {t: (source_cols[i] if i < len(source_cols) else "X")
                        for i, t in enumerate(target_cols)},
        },
    }


def _corpus() -> Dict[str, Any]:
    pp = {"function": "POPULATE_PP_FROMGL", "nodes": [
        _insert_node("N1", "STG_PRODUCT_PROCESSOR", ["N_EOP_BAL", "V_ACCT"], ["N_AMOUNT_LCY", "V_X"])]}
    pp_amc = {"function": "POPULATE_PP_FROMGL_AMC", "nodes": [
        _insert_node("N1", "STG_PRODUCT_PROCESSOR", ["N_EOP_BAL"], ["N_AMOUNT_LCY"])]}
    # CAP_LEVERAGE_FN writes N_OTHER, reads N_EOP_BAL as a source value.
    cap = {"function": "CAP_LEVERAGE_FN", "nodes": [
        _insert_node("N1", "FCT_LEVERAGE", ["N_OTHER"], ["N_EOP_BAL"])]}
    graphs = {
        "OFSMDM": {
            "POPULATE_PP_FROMGL": pp,
            "POPULATE_PP_FROMGL_AMC": pp_amc,
            "CAP_LEVERAGE_FN": cap,
        }
    }
    # Column index is direction-blind: every reference (write OR read) is
    # registered identically as "FN:node_id".
    index = {
        "OFSMDM": {
            "N_EOP_BAL": [
                "POPULATE_PP_FROMGL:N1",
                "POPULATE_PP_FROMGL_AMC:N1",
                "CAP_LEVERAGE_FN:N1",       # reader only
            ],
            "N_AMOUNT_LCY": [
                "POPULATE_PP_FROMGL:N1",    # source value only — reader
                "POPULATE_PP_FROMGL_AMC:N1",
            ],
            "N_OTHER": ["CAP_LEVERAGE_FN:N1"],
        }
    }
    return {"graphs": graphs, "index": index}


def _patch_corpus(monkeypatch, corpus: Dict[str, Any]) -> None:
    graphs = corpus["graphs"]
    index = corpus["index"]

    def fake_get_column_index(_redis, schema):
        return index.get(schema)

    def fake_get_function_graph(_redis, schema, fn):
        return (graphs.get(schema) or {}).get(fn)

    def fake_discovered_schemas(_redis):
        return sorted(graphs.keys())

    monkeypatch.setattr(orch_mod, "get_column_index", fake_get_column_index)
    monkeypatch.setattr(orch_mod, "get_function_graph", fake_get_function_graph)
    monkeypatch.setattr(orch_mod, "discovered_schemas", fake_discovered_schemas)


# ===========================================================================
# detect_column_tokens
# ===========================================================================

class TestDetectColumnTokens:
    def test_column_token_detected(self):
        assert detect_column_tokens("How is N_EOP_BAL written?") == ["N_EOP_BAL"]

    def test_function_name_is_not_a_column(self):
        # Multi-letter prefixes (FN_, STG_, FCT_) are not single-letter
        # type-prefix columns — never selected here.
        assert detect_column_tokens("How does FN_LOAD_OPS_RISK_DATA work?") == []

    def test_table_prefix_not_a_column(self):
        assert detect_column_tokens("data from STG_GL_DATA and FCT_X") == []

    def test_multiple_columns_first_occurrence_wins(self):
        assert detect_column_tokens("N_EOP_BAL and F_FLAG and N_EOP_BAL") == ["N_EOP_BAL", "F_FLAG"]

    def test_empty(self):
        assert detect_column_tokens("") == []


# ===========================================================================
# _node_target_columns / _classify_node_operation — structured direction
# ===========================================================================

class TestNodeLevelClassification:
    def test_insert_targets_exclude_source_values(self):
        node = _insert_node("N1", "T", ["N_EOP_BAL"], ["N_AMOUNT_LCY"])
        targets = _node_target_columns(node)
        assert "N_EOP_BAL" in targets
        assert "N_AMOUNT_LCY" not in targets  # source value, not a target

    def test_update_flat_map_targets_are_keys(self):
        node = {"id": "N", "type": "UPDATE", "target_table": "T",
                "column_maps": {"N_FOO": "A + B"}}
        targets = _node_target_columns(node)
        assert targets == {"N_FOO"}

    def test_scalar_compute_output_variable_is_target(self):
        node = {"id": "N", "type": "SCALAR_COMPUTE", "output_variable": "LN_TOT",
                "column_maps": {}}
        assert "LN_TOT" in _node_target_columns(node)

    def test_classify_node_operation_reuses_variable_tracer(self, monkeypatch):
        # The operation label must come from VariableTracer._classify_operation
        # — NOT a duplicated keyword detector in the orchestrator. Spy on it.
        from src.agents.variable_tracer import VariableTracer
        calls: list = []
        real = VariableTracer._classify_operation

        def spy(self, text_upper, matched_aliases):
            calls.append(text_upper)
            return real(self, text_upper, matched_aliases)

        monkeypatch.setattr(VariableTracer, "_classify_operation", spy)
        assert _classify_node_operation({"type": "INSERT", "target_table": "T"}) == "INSERT"
        assert _classify_node_operation({"type": "SCALAR_COMPUTE", "output_variable": "V"}) == "SELECT_INTO"
        assert calls, "VariableTracer._classify_operation was never invoked"

    def test_loop_inner_op_recursion(self):
        inner = _insert_node("N1", "T", ["N_EOP_BAL"], ["SRC"])
        loop = {"id": "L", "type": "FOR_LOOP", "column_maps": {}, "inner_operations": [inner]}
        assert _column_write_operation("N_EOP_BAL", loop) == "INSERT"

    def test_reader_in_insert_is_not_a_writer(self):
        # N_EOP_BAL appearing as a SOURCE value in an INSERT must not classify
        # as a writer — this is the exact reader/writer trap the fix avoids.
        node = _insert_node("N1", "T", ["N_OTHER"], ["N_EOP_BAL"])
        assert _column_write_operation("N_EOP_BAL", node) is None


# ===========================================================================
# resolve_column_writers — writer SET resolution, reader exclusion
# ===========================================================================

class TestResolveColumnWriters:
    def test_resolves_only_writers(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        writers = resolve_column_writers("N_EOP_BAL", _SENTINEL_REDIS)
        fns = sorted(w["function"] for w in writers)
        assert fns == ["POPULATE_PP_FROMGL", "POPULATE_PP_FROMGL_AMC"]
        # CAP_LEVERAGE_FN reads N_EOP_BAL but never writes it — excluded.
        assert "CAP_LEVERAGE_FN" not in fns
        assert all(w["operation"] == "INSERT" for w in writers)
        assert all(w["schema"] == "OFSMDM" for w in writers)

    def test_multi_writer_returns_all(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        writers = resolve_column_writers("N_EOP_BAL", _SENTINEL_REDIS)
        assert len(writers) == 2

    def test_reader_only_column_returns_empty(self, monkeypatch):
        # N_AMOUNT_LCY appears only as a source value — no writer.
        _patch_corpus(monkeypatch, _corpus())
        assert resolve_column_writers("N_AMOUNT_LCY", _SENTINEL_REDIS) == []

    def test_column_absent_from_index_returns_empty(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        assert resolve_column_writers("N_DOES_NOT_EXIST", _SENTINEL_REDIS) == []

    def test_redis_none_returns_empty(self):
        assert resolve_column_writers("N_EOP_BAL", None) == []

    def test_column_index_read_failure_is_swallowed(self, monkeypatch):
        # A Redis/deserialization failure on one schema degrades to skip,
        # never leaks an exception (mirrors apply_bi_routing's contract).
        def boom(_redis, _schema):
            raise RuntimeError("redis down")

        monkeypatch.setattr(orch_mod, "get_column_index", boom)
        monkeypatch.setattr(orch_mod, "discovered_schemas", lambda _r: ["OFSMDM"])
        assert resolve_column_writers("N_EOP_BAL", _SENTINEL_REDIS) == []


# ===========================================================================
# apply_column_provenance_anchor — routing
# ===========================================================================

def _base_state(query: str, query_type: str = "COLUMN_LOGIC", **extra) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "raw_query": query,
        "query_type": query_type,
        "target_variable": "",
        "schema": "OFSMDM",
    }
    state.update(extra)
    return state


class TestApplyColumnProvenanceAnchor:
    def test_fires_and_routes_to_variable_trace(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        state = _base_state("How is N_EOP_BAL written?", target_variable="N_EOP_BAL")
        out = apply_column_provenance_anchor(state, state["raw_query"], _SENTINEL_REDIS)

        assert out["query_type"] == "VARIABLE_TRACE"
        assert out["target_variable"] == "N_EOP_BAL"
        prov = out["column_provenance"]
        assert prov["column"] == "N_EOP_BAL"
        assert sorted(prov["writer_functions"]) == ["POPULATE_PP_FROMGL", "POPULATE_PP_FROMGL_AMC"]
        assert out["schema"] == "OFSMDM"  # single writer schema stamped

    def test_column_token_from_raw_query_without_target_variable(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        # Classifier didn't extract a target_variable; the column is scanned
        # from the raw query instead.
        state = _base_state("Explain the N_EOP_BAL writing process")
        out = apply_column_provenance_anchor(state, state["raw_query"], _SENTINEL_REDIS)
        assert out["query_type"] == "VARIABLE_TRACE"
        assert out["target_variable"] == "N_EOP_BAL"

    def test_reader_only_column_is_noop(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        state = _base_state("How is N_AMOUNT_LCY written?", target_variable="N_AMOUNT_LCY")
        out = apply_column_provenance_anchor(state, state["raw_query"], _SENTINEL_REDIS)
        # No writer → no anchor; query_type untouched, no provenance stamped.
        assert out["query_type"] == "COLUMN_LOGIC"
        assert "column_provenance" not in out

    def test_non_column_query_is_noop(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        state = _base_state("How does FN_LOAD_OPS_RISK_DATA work?")
        out = apply_column_provenance_anchor(state, state["raw_query"], _SENTINEL_REDIS)
        assert out["query_type"] == "COLUMN_LOGIC"
        assert "column_provenance" not in out

    def test_explicit_function_name_short_circuits(self, monkeypatch):
        # User named a real function that ALSO touches a column — honour the
        # explicit choice, do not re-route on the column.
        corpus = _corpus()
        corpus["graphs"]["OFSMDM"]["FN_REAL_FUNC"] = {"function": "FN_REAL_FUNC", "nodes": []}
        _patch_corpus(monkeypatch, corpus)
        state = _base_state("In FN_REAL_FUNC, how is N_EOP_BAL written?")
        out = apply_column_provenance_anchor(state, state["raw_query"], _SENTINEL_REDIS)
        assert out["query_type"] == "COLUMN_LOGIC"
        assert "column_provenance" not in out

    def test_bi_routing_already_fired_is_skipped(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        state = _base_state("How is N_EOP_BAL written?", target_variable="N_EOP_BAL",
                            bi_routing={"function": "SOME_CAP_FN"})
        out = apply_column_provenance_anchor(state, state["raw_query"], _SENTINEL_REDIS)
        assert out["query_type"] == "COLUMN_LOGIC"
        assert "column_provenance" not in out

    def test_w76_anchor_already_fired_is_skipped(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        state = _base_state("How is N_EOP_BAL written?", target_variable="N_EOP_BAL",
                            w76_anchor={"function": "SOME_FN"})
        out = apply_column_provenance_anchor(state, state["raw_query"], _SENTINEL_REDIS)
        assert out["query_type"] == "COLUMN_LOGIC"
        assert "column_provenance" not in out

    def test_wrong_query_type_is_noop(self, monkeypatch):
        _patch_corpus(monkeypatch, _corpus())
        state = _base_state("How is N_EOP_BAL written?", query_type="DATA_QUERY",
                            target_variable="N_EOP_BAL")
        out = apply_column_provenance_anchor(state, state["raw_query"], _SENTINEL_REDIS)
        assert out["query_type"] == "DATA_QUERY"
        assert "column_provenance" not in out

    def test_redis_unavailable_is_noop(self):
        state = _base_state("How is N_EOP_BAL written?", target_variable="N_EOP_BAL")
        out = apply_column_provenance_anchor(state, state["raw_query"], None)
        assert out["query_type"] == "COLUMN_LOGIC"
        assert "column_provenance" not in out


# ===========================================================================
# ensure_column_writers_in_search_results — force-inclusion
# ===========================================================================

class TestEnsureColumnWritersInSearchResults:
    def test_injects_all_missing_writers(self):
        state = {
            "search_results": [{"function_name": "UNRELATED_FN", "score": 0.1}],
            "column_provenance": {
                "column": "N_EOP_BAL",
                "writers": [
                    {"function": "POPULATE_PP_FROMGL", "schema": "OFSMDM", "operation": "INSERT"},
                    {"function": "POPULATE_PP_FROMGL_AMC", "schema": "OFSMDM", "operation": "INSERT"},
                ],
            },
        }
        out = ensure_column_writers_in_search_results(state)
        names = [r["function_name"] for r in out["search_results"]]
        assert "POPULATE_PP_FROMGL" in names
        assert "POPULATE_PP_FROMGL_AMC" in names
        # Appended (W147 contract) — the pre-existing result keeps position 0.
        assert names[0] == "UNRELATED_FN"

    def test_does_not_duplicate_present_writer(self):
        state = {
            "search_results": [{"function_name": "POPULATE_PP_FROMGL", "score": 0.2}],
            "column_provenance": {
                "column": "N_EOP_BAL",
                "writers": [{"function": "POPULATE_PP_FROMGL", "schema": "OFSMDM", "operation": "INSERT"}],
            },
        }
        out = ensure_column_writers_in_search_results(state)
        names = [r["function_name"] for r in out["search_results"]]
        assert names.count("POPULATE_PP_FROMGL") == 1

    def test_noop_without_provenance(self):
        state = {"search_results": [{"function_name": "X"}]}
        out = ensure_column_writers_in_search_results(state)
        assert out["search_results"] == [{"function_name": "X"}]

    def test_noop_with_empty_writers(self):
        state = {"search_results": [], "column_provenance": {"column": "N_X", "writers": []}}
        out = ensure_column_writers_in_search_results(state)
        assert out["search_results"] == []
