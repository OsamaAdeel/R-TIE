"""
Stub tests for src.parsing.store — store/get with mock Redis.
"""

import pytest
from unittest.mock import MagicMock, patch

from src.parsing.store import (
    store_function_graph,
    get_function_graph,
    store_column_index,
    get_column_index,
    store_raw_source,
    get_raw_source,
    store_literal_index,
    get_literal_index,
)
from src.parsing.serializer import to_msgpack, from_msgpack


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


# ---------------------------------------------------------------------------
# W35 Phase 5 — business identifier literal index
# ---------------------------------------------------------------------------

def test_store_literal_index_writes_one_key_per_identifier():
    """store_literal_index issues one Redis SET per identifier in the
    given index, returning the count of keys written."""
    storage = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    index = {
        "CAP943": [
            {"function": "CS_DEFERRED_TAX", "line": 4, "role": "case_when_target"},
            {"function": "REGULATORY_ADJUSTMENT_DATA_POP", "line": 24,
             "role": "in_list_member"},
        ],
        "CAP309": [
            {"function": "CS_DEFERRED_TAX", "line": 5, "role": "case_when_source"},
        ],
    }

    written = store_literal_index(mock_redis, "OFSERM", index)
    assert written == 2
    assert "graph:literal:OFSERM:CAP943" in storage
    assert "graph:literal:OFSERM:CAP309" in storage


def test_store_literal_index_skips_empty_buckets():
    """An identifier with an empty record list is not written."""
    storage = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})

    index = {"CAP943": [], "CAP309": [
        {"function": "FN", "line": 1, "role": "filter"},
    ]}

    written = store_literal_index(mock_redis, "OFSERM", index)
    assert written == 1
    assert "graph:literal:OFSERM:CAP943" not in storage
    assert "graph:literal:OFSERM:CAP309" in storage


def test_round_trip_literal_index():
    """store_literal_index then get_literal_index returns the same
    records (msgpack round-trip)."""
    storage = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})
    mock_redis.get.side_effect = lambda k: storage.get(k)

    records = [
        {"function": "CS_DEFERRED_TAX", "line": 4, "role": "case_when_target"},
        {"function": "REGULATORY_ADJUSTMENT_DATA_POP", "line": 24,
         "role": "in_list_member"},
    ]
    store_literal_index(mock_redis, "OFSERM", {"CAP943": records})

    got = get_literal_index(mock_redis, "OFSERM", "CAP943")
    assert got == records


def test_get_literal_index_missing_returns_none():
    mock_redis = MagicMock()
    mock_redis.get.return_value = None

    assert get_literal_index(mock_redis, "OFSERM", "CAP000") is None


def test_cross_schema_literal_keys_distinct():
    """Same identifier in two schemas → two separate Redis keys."""
    storage = {}
    mock_redis = MagicMock()
    mock_redis.set.side_effect = lambda k, v: storage.update({k: v})

    index = {"CAP943": [{"function": "FN", "line": 1, "role": "filter"}]}
    store_literal_index(mock_redis, "OFSERM", index)
    store_literal_index(mock_redis, "OFSMDM", index)

    assert "graph:literal:OFSERM:CAP943" in storage
    assert "graph:literal:OFSMDM:CAP943" in storage
