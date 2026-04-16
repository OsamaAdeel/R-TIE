"""
Stub tests for src.tools.graph.store — store/get with mock Redis.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.tools.graph.store import (
    store_function_graph,
    get_function_graph,
    store_column_index,
    get_column_index,
    store_raw_source,
    get_raw_source,
)
from src.tools.graph.serializer import to_msgpack, from_msgpack


_SAMPLE_GRAPH = {
    "function": "FN_STORE_TEST",
    "nodes": [{"id": "FN_STORE_TEST_N1", "type": "INSERT"}],
    "edges": [],
}


def test_store_and_get_function_graph():
    """store_function_graph writes to Redis; get_function_graph reads it
    back and returns an equivalent dict."""
    storage = {}

    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    ok = store_function_graph(mock_redis, "SCH", "FN_STORE_TEST", _SAMPLE_GRAPH)
    assert ok is True

    retrieved = get_function_graph(mock_redis, "SCH", "FN_STORE_TEST")
    assert retrieved is not None
    assert retrieved["function"] == "FN_STORE_TEST"
    assert len(retrieved["nodes"]) == 1


def test_get_function_graph_returns_none_when_missing():
    """get_function_graph returns None when the key does not exist."""
    mock_redis = MagicMock()
    mock_redis.get.return_value = None

    result = get_function_graph(mock_redis, "SCH", "MISSING_FN")
    assert result is None


def test_store_and_get_column_index():
    """Round-trip for the column index."""
    storage = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    index = {"N_EOP_BAL": ["FN_A_N1", "FN_B_N2"], "V_CCY_CODE": ["FN_A_N3"]}

    ok = store_column_index(mock_redis, "SCH", index)
    assert ok is True

    retrieved = get_column_index(mock_redis, "SCH")
    assert retrieved is not None
    assert "N_EOP_BAL" in retrieved
    assert len(retrieved["N_EOP_BAL"]) == 2


def test_store_and_get_raw_source():
    """Round-trip for raw SQL source lines."""
    storage = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    lines = ["BEGIN\n", "  NULL;\n", "END;\n"]

    ok = store_raw_source(mock_redis, "SCH", "FN_RAW", lines)
    assert ok is True

    retrieved = get_raw_source(mock_redis, "SCH", "FN_RAW")
    assert retrieved is not None
    assert len(retrieved) == 3
    assert "BEGIN" in retrieved[0]
