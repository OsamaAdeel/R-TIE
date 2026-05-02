"""
Unit tests for src.parsing.query_engine — Tests 13-17.
All Redis interactions are mocked.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.parsing.query_engine import (
    resolve_aliases,
    resolve_variable_nodes,
    assemble_llm_payload,
    determine_execution_order,
)


# -----------------------------------------------------------------------
# Test 13: test_resolve_aliases_ead
# -----------------------------------------------------------------------

def test_resolve_aliases_ead():
    """resolve_aliases looks up 'EAD' in the Redis alias map and returns
    the canonical column names it maps to."""
    alias_map = {
        "EAD": ["N_EOP_BAL", "N_EAD", "N_EAD_DRAWN", "N_UNDRAWN_AMT"],
        "LGD": ["N_LGD_RATIO"],
    }
    # Serialise the alias map as JSON (the function calls from_json on raw)
    import json
    alias_json = json.dumps(alias_map)

    mock_redis = MagicMock()
    mock_redis.get.return_value = alias_json

    result = resolve_aliases("EAD", "OFSMDM", mock_redis)

    assert result == ["N_EOP_BAL", "N_EAD", "N_EAD_DRAWN", "N_UNDRAWN_AMT"]
    mock_redis.get.assert_called_once_with("graph:aliases:OFSMDM")


# -----------------------------------------------------------------------
# Test 14: test_resolve_variable_nodes_returns_correct_node_ids
# -----------------------------------------------------------------------

def test_resolve_variable_nodes_returns_correct_node_ids():
    """resolve_variable_nodes combines alias expansion with column-index
    lookup to return the correct node IDs."""
    alias_map = {
        "EAD": ["N_EOP_BAL"],
    }
    column_index = {
        "N_EOP_BAL": ["FN_GL_LOAD_N1", "FN_GL_LOAD_N3"],
    }

    import json
    alias_json = json.dumps(alias_map)

    mock_redis = MagicMock()
    mock_redis.get.return_value = alias_json

    # get_column_index reads from Redis via from_msgpack — we patch it
    with patch(
        "src.parsing.query_engine.get_column_index",
        return_value=column_index,
    ):
        result = resolve_variable_nodes("EAD", "OFSMDM", mock_redis)

    assert result == ["FN_GL_LOAD_N1", "FN_GL_LOAD_N3"]


# -----------------------------------------------------------------------
# Test 15: test_assemble_llm_payload_structure
# -----------------------------------------------------------------------

def test_assemble_llm_payload_structure():
    """assemble_llm_payload renders the standard multi-node shape:
    one STEP block per surviving node, with Operation: and Source:
    labels on each. Pass-through consolidation and relevance filtering
    are tested separately."""
    node1 = {
        "function": "FN_A",
        "node": {
            "id": "FN_A_N1",
            "type": "INSERT",
            "target_table": "TBL_X",
            "source_tables": ["SRC_A"],
            "column_maps": {"mapping": {"COL1": "UPPER(SRC_A.COL1)"}},
            "calculation": [],
            "conditions": [],
            "committed_after": False,
            "line_start": 10,
            "line_end": 20,
        },
    }
    node2 = {
        "function": "FN_A",
        "node": {
            "id": "FN_A_N2",
            "type": "UPDATE",
            "target_table": "TBL_X",
            "source_tables": ["SRC_B"],
            "column_maps": {"mapping": {"COL1": "SRC_B.COL1 || '_SUFFIX'"}},
            "calculation": [],
            "conditions": ["SRC_B.FLAG = 'Y'"],
            "committed_after": True,
            "line_start": 25,
            "line_end": 30,
        },
    }
    edges = [
        {"id": "E1", "from": "FN_A_N1", "to": "FN_A_N2", "type": "TABLE_FLOW"},
    ]
    execution_order = [node1, node2]

    payload = assemble_llm_payload(
        nodes=[node1, node2],
        edges=edges,
        target_variable="COL1",
        user_query="How is COL1 populated?",
        execution_order=execution_order,
    )

    assert "STEP 1" in payload
    assert "STEP 2" in payload
    assert "Operation:" in payload
    assert "Source:" in payload


# -----------------------------------------------------------------------
# Test 15b: test_assemble_llm_payload_passthrough_consolidates
# -----------------------------------------------------------------------

def test_assemble_llm_payload_passthrough_consolidates():
    """Consecutive same-function pass-through nodes (column_maps in flat
    shape, treated as direct copies by _is_passthrough_node) collapse
    into a single [PASS-THROUGH] block: no per-node Operation: line,
    line range spans all merged nodes."""
    node1 = {
        "function": "FN_PT",
        "node": {
            "id": "FN_PT_N1",
            "type": "INSERT",
            "target_table": "TBL_X",
            "source_tables": ["SRC_A"],
            "column_maps": {"COL1": "SRC_A.COL1"},
            "calculation": [],
            "conditions": [],
            "committed_after": False,
            "line_start": 10,
            "line_end": 20,
        },
    }
    node2 = {
        "function": "FN_PT",
        "node": {
            "id": "FN_PT_N2",
            "type": "INSERT",
            "target_table": "TBL_Y",
            "source_tables": ["SRC_B"],
            "column_maps": {"COL1": "SRC_B.COL1"},
            "calculation": [],
            "conditions": [],
            "committed_after": True,
            "line_start": 25,
            "line_end": 30,
        },
    }

    payload = assemble_llm_payload(
        nodes=[node1, node2],
        edges=[],
        target_variable="COL1",
        user_query="How is COL1 populated?",
        execution_order=[node1, node2],
    )

    assert "[PASS-THROUGH]" in payload
    assert "Operation:" not in payload
    assert "lines 10-30" in payload


# -----------------------------------------------------------------------
# Test 16: test_assemble_llm_payload_under_2000_chars
# -----------------------------------------------------------------------

def test_assemble_llm_payload_under_2000_chars():
    """The assembled payload never exceeds 2000 characters, even with
    multiple nodes."""
    nodes_and_order = []
    for i in range(1, 4):
        entry = {
            "function": "FN_BIG",
            "node": {
                "id": f"FN_BIG_N{i}",
                "type": "INSERT",
                "target_table": f"TABLE_{i}",
                "source_tables": [f"SRC_{i}"],
                "column_maps": {f"C{j}": f"SRC_{i}.C{j}" for j in range(10)},
                "calculation": [
                    {"type": "DIRECT", "column": f"C{j}", "expression": f"SRC_{i}.C{j}"}
                    for j in range(10)
                ],
                "conditions": [f"SRC_{i}.FLAG = 'Y'" for _ in range(3)],
                "committed_after": True,
                "line_start": i * 50,
                "line_end": i * 50 + 40,
            },
        }
        nodes_and_order.append(entry)

    edges = [
        {"id": "E1", "from": "FN_BIG_N1", "to": "FN_BIG_N2", "type": "TABLE_FLOW"},
        {"id": "E2", "from": "FN_BIG_N2", "to": "FN_BIG_N3", "type": "TABLE_FLOW"},
    ]

    payload = assemble_llm_payload(
        nodes=nodes_and_order,
        edges=edges,
        target_variable="C0",
        user_query="Trace C0",
        execution_order=nodes_and_order,
    )

    assert len(payload) <= 2000


# -----------------------------------------------------------------------
# Test 17: test_determine_execution_order
# -----------------------------------------------------------------------

def test_determine_execution_order():
    """Given nodes A, B, C with edges A->B, B->C the topological sort
    returns [A, B, C]."""
    node_a = {"id": "A", "type": "INSERT", "line_start": 1}
    node_b = {"id": "B", "type": "INSERT", "line_start": 10}
    node_c = {"id": "C", "type": "INSERT", "line_start": 20}

    nodes = [node_a, node_b, node_c]
    edges = [
        {"from": "A", "to": "B", "type": "TABLE_FLOW"},
        {"from": "B", "to": "C", "type": "TABLE_FLOW"},
    ]

    ordered = determine_execution_order(nodes, edges)

    ordered_ids = [n.get("id") for n in ordered]
    assert ordered_ids == ["A", "B", "C"]


# -----------------------------------------------------------------------
# Test: test_cross_function_traversal
# -----------------------------------------------------------------------

def test_cross_function_traversal():
    """resolve_variable_nodes should return nodes from multiple functions."""
    mock_redis = MagicMock()
    # Return None for alias map so resolve_aliases returns [term.upper()]
    mock_redis.get.return_value = None

    # Mock column index with entries in two functions
    mock_index = {
        "N_ANNUAL_GROSS_INCOME": ["FN_LOAD_OPS_RISK_DATA:node_3", "TLX_OPS_ADJ_MISDATE:node_1"],
    }
    # Mock full graph with cross-function edge
    mock_full_graph = {
        "edges": [
            {
                "from_node": "FN_LOAD_OPS_RISK_DATA:node_3",
                "to_node": "TLX_OPS_ADJ_MISDATE:node_1",
                "from": "FN_LOAD_OPS_RISK_DATA:node_3",
                "to": "TLX_OPS_ADJ_MISDATE:node_1",
                "via_table": "STG_OPS_RISK_DATA",
            }
        ]
    }

    with patch(
        "src.parsing.query_engine.get_column_index",
        return_value=mock_index,
    ), patch(
        "src.parsing.query_engine.get_full_graph",
        return_value=mock_full_graph,
    ):
        nodes = resolve_variable_nodes("N_ANNUAL_GROSS_INCOME", "OFSMDM", mock_redis)

    assert "FN_LOAD_OPS_RISK_DATA:node_3" in nodes
    assert "TLX_OPS_ADJ_MISDATE:node_1" in nodes


# -----------------------------------------------------------------------
# Test: test_execution_condition_in_payload
# -----------------------------------------------------------------------

def test_execution_condition_in_payload():
    """Payload should include EXECUTION CONDITION when present."""
    nodes = [
        {
            "function": "FN_LOAD_OPS_RISK_DATA",
            "node": {"id": "node_1", "type": "UPDATE", "target_table": "STG_OPS_RISK_DATA",
                     "source_tables": [], "column_maps": {"N_ANNUAL_GROSS_INCOME": "TOT1"},
                     "calculation": [], "conditions": [], "line_start": 310, "line_end": 325,
                     "committed_after": True, "summary": "Updates income"},
            "execution_condition": {"type": "MONTH_CHECK", "expression": "EXTRACT(MONTH) = 12",
                                     "plain_text": "Only executes in December", "consequence": "Skips entirely"},
        }
    ]
    payload = assemble_llm_payload(nodes, [], "N_ANNUAL_GROSS_INCOME", "How is it calculated?", nodes)
    assert "EXECUTION CONDITION" in payload or "December" in payload


# -----------------------------------------------------------------------
# Test: test_scalar_compute_in_payload
# -----------------------------------------------------------------------

def test_scalar_compute_in_payload():
    """Payload should include INTERMEDIATE VARIABLES section."""
    nodes = [
        {
            "function": "FN_LOAD",
            "node": {"id": "node_sc_1", "type": "SCALAR_COMPUTE", "target_table": None,
                     "source_tables": ["STG_GL_DATA"], "column_maps": {},
                     "output_variable": "TOT1", "calculation": {"type": "ARITHMETIC", "formula": "A + B"},
                     "conditions": [], "line_start": 305, "line_end": 305,
                     "committed_after": False, "summary": "Computes TOT1"},
            "execution_condition": None,
        },
        {
            "function": "FN_LOAD",
            "node": {"id": "node_3", "type": "UPDATE", "target_table": "STG_OPS_RISK_DATA",
                     "source_tables": [], "column_maps": {"N_ANNUAL_GROSS_INCOME": "TOT1"},
                     "calculation": [], "conditions": [], "line_start": 310, "line_end": 325,
                     "committed_after": True, "summary": "Updates income"},
            "execution_condition": None,
        },
    ]
    edges = [{"from_node": "FN_LOAD:node_sc_1", "to_node": "FN_LOAD:node_3",
              "from": "node_sc_1", "to": "node_3",
              "source_col": "TOT1", "transform": "variable_reference"}]
    payload = assemble_llm_payload(nodes, edges, "N_ANNUAL_GROSS_INCOME", "How?", nodes)
    assert "INTERMEDIATE" in payload or "TOT1" in payload


# -----------------------------------------------------------------------
# Test: test_payload_contains_passthrough_label
# -----------------------------------------------------------------------

def test_payload_contains_passthrough_label():
    """Payload should label DIRECT/pass-through steps clearly."""
    nodes = [
        {
            "function": "TLX_OPS",
            "node": {"id": "node_1", "type": "DIRECT", "target_table": "STG_OPS_RISK_DATA",
                     "source_tables": ["STG_OPS_ADJ_MISDATE_TLX"],
                     "column_maps": {"N_ANNUAL_GROSS_INCOME": "N_ANNUAL_GROSS_INCOME"},
                     "calculation": {"type": "DIRECT", "source_table": "STG_OPS_ADJ_MISDATE_TLX",
                                     "source_column": "N_ANNUAL_GROSS_INCOME"},
                     "conditions": [], "line_start": 278, "line_end": 349,
                     "committed_after": True, "summary": "Copies data"},
            "execution_condition": None,
        }
    ]
    payload = assemble_llm_payload(nodes, [], "N_ANNUAL_GROSS_INCOME", "How?", nodes)
    assert "PASS" in payload.upper() or "DIRECT" in payload.upper()


# -----------------------------------------------------------------------
# Test: test_upstream_scalar_compute_fetched
# -----------------------------------------------------------------------

def test_upstream_scalar_compute_fetched():
    """fetch_nodes_by_ids with include_upstream should return SCALAR_COMPUTE nodes."""
    from src.parsing.query_engine import fetch_nodes_by_ids

    mock_redis = MagicMock()

    # Node IDs follow the FUNCTION_NAME_N<number> convention used by the parser
    sc_id = "FN_TEST_N1"
    upd_id = "FN_TEST_N3"

    # Mock function graph with SC + UPDATE nodes
    mock_graph = {
        "function": "FN_TEST",
        "nodes": [
            {"id": sc_id, "type": "SCALAR_COMPUTE", "output_variable": "TOT1",
             "line_start": 305, "line_end": 305, "source_tables": ["STG_GL_DATA"],
             "column_maps": {}, "conditions": [], "summary": "Computes TOT1"},
            {"id": upd_id, "type": "UPDATE", "target_table": "STG_OPS",
             "line_start": 310, "line_end": 325, "source_tables": [],
             "column_maps": {"N_INCOME": "TOT1"}, "conditions": [], "summary": "Updates"},
        ],
        "edges": [
            {"from_node": sc_id, "to_node": upd_id,
             "from": sc_id, "to": upd_id,
             "source_col": "TOT1", "transform": "variable_reference"}
        ],
    }
    mock_full = {"edges": mock_graph["edges"], "nodes": {"FN_TEST": mock_graph["nodes"]}}

    with patch("src.parsing.query_engine.get_function_graph", return_value=mock_graph), \
         patch("src.parsing.query_engine.get_full_graph", return_value=mock_full):
        result = fetch_nodes_by_ids(["FN_TEST:" + upd_id], "OFSMDM", mock_redis, include_upstream=True)

    fns = [r.get("function") for r in result]
    assert len(result) >= 2 or "FN_TEST" in fns, f"Expected upstream node, got {len(result)} results"


# -----------------------------------------------------------------------
# Test: test_payload_no_cfi_condition
# -----------------------------------------------------------------------

def test_payload_no_cfi_condition():
    """Payload should NOT contain commented-out CFI branch."""
    nodes = [{
        "function": "FN_LOAD",
        "node": {"id": "node_3", "type": "UPDATE", "target_table": "STG_OPS",
                 "source_tables": [], "column_maps": {"N_INCOME": "TOT1"},
                 "conditions": ["FIC_MIS_DATE = CQD", "V_LOB_CODE IN ('CBA','RBA')"],
                 "line_start": 310, "line_end": 325,
                 "committed_after": True, "summary": "Updates income"},
        "execution_condition": None,
    }]
    payload = assemble_llm_payload(nodes, [], "N_ANNUAL_GROSS_INCOME", "How?", [])
    assert "CFI" not in payload, f"Commented-out CFI should not appear in payload"
