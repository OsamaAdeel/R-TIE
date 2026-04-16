"""
Stub tests for src.parsing.serializer — roundtrip serialisation.
"""

import pytest

from src.parsing.serializer import (
    to_json,
    from_json,
    to_msgpack,
    from_msgpack,
)


_SAMPLE_GRAPH = {
    "function": "FN_TEST",
    "schema": "OFSMDM",
    "nodes": [
        {"id": "FN_TEST_N1", "type": "INSERT", "target_table": "TBL_A"},
        {"id": "FN_TEST_N2", "type": "UPDATE", "target_table": "TBL_B"},
    ],
    "edges": [
        {"from": "FN_TEST_N1", "to": "FN_TEST_N2", "type": "TABLE_FLOW"},
    ],
}


def test_json_roundtrip():
    """to_json -> from_json returns an equivalent dict."""
    json_str = to_json(_SAMPLE_GRAPH)
    restored = from_json(json_str)

    assert restored["function"] == _SAMPLE_GRAPH["function"]
    assert len(restored["nodes"]) == len(_SAMPLE_GRAPH["nodes"])
    assert restored["edges"] == _SAMPLE_GRAPH["edges"]


def test_json_pretty_roundtrip():
    """Pretty-printed JSON also deserialises correctly."""
    json_str = to_json(_SAMPLE_GRAPH, pretty=True)
    restored = from_json(json_str)

    assert restored == _SAMPLE_GRAPH


def test_msgpack_roundtrip():
    """to_msgpack -> from_msgpack returns an equivalent dict."""
    packed = to_msgpack(_SAMPLE_GRAPH)
    assert isinstance(packed, bytes)

    restored = from_msgpack(packed)

    assert restored["function"] == _SAMPLE_GRAPH["function"]
    assert len(restored["nodes"]) == len(_SAMPLE_GRAPH["nodes"])
    assert restored["edges"] == _SAMPLE_GRAPH["edges"]


def test_msgpack_smaller_than_json():
    """MessagePack representation should be smaller than JSON."""
    json_bytes = to_json(_SAMPLE_GRAPH).encode("utf-8")
    msgpack_bytes = to_msgpack(_SAMPLE_GRAPH)

    assert len(msgpack_bytes) < len(json_bytes)
