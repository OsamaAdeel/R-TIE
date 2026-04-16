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
    """assemble_llm_payload renders a text payload that contains STEP
    markers, Operation, and Source labels for each node."""
    node1 = {
        "function": "FN_A",
        "node": {
            "id": "FN_A_N1",
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
        "function": "FN_A",
        "node": {
            "id": "FN_A_N2",
            "type": "UPDATE",
            "target_table": "TBL_X",
            "source_tables": [],
            "column_maps": {"COL2": "'FIXED'"},
            "calculation": [],
            "conditions": ["COL1 IS NOT NULL"],
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
