"""
Serializes graph structures to JSON and MessagePack.
MessagePack is used for Redis storage (3-5x smaller than JSON).
"""

import json

import msgpack


def to_json(graph: dict, pretty: bool = False) -> str:
    """Serialize a graph dict to a JSON string."""
    if pretty:
        return json.dumps(graph, indent=2, ensure_ascii=False, default=str)
    return json.dumps(graph, ensure_ascii=False, default=str)


def from_json(json_str: str) -> dict:
    """Deserialize a JSON string back to a graph dict."""
    return json.loads(json_str)


def to_msgpack(graph: dict) -> bytes:
    """Serialize a graph dict to MessagePack bytes (compact binary format)."""
    return msgpack.packb(graph, use_bin_type=True, default=str)


def from_msgpack(data: bytes) -> dict:
    """Deserialize MessagePack bytes back to a graph dict."""
    return msgpack.unpackb(data, raw=False)


def calculate_compression_ratio(original_lines: int, graph: dict) -> dict:
    """
    Calculate compression metrics comparing raw source lines to graph representation.

    Returns a dict with:
        - original_lines: number of source lines
        - graph_nodes: number of nodes in the graph
        - json_chars: character count of the JSON representation
        - compression_pct: percentage reduction from raw to graph (token-based)
        - token_estimate_raw: estimated tokens for the raw source (chars / 4)
        - token_estimate_graph: estimated tokens for the graph JSON (chars / 4)
    """
    json_str = to_json(graph)
    json_chars = len(json_str)
    graph_nodes = len(graph.get("nodes", []))

    # Rough estimate: 1 token ~ 4 characters
    # For raw source, estimate ~40 chars per line on average
    avg_chars_per_line = 40
    raw_chars = original_lines * avg_chars_per_line
    token_estimate_raw = raw_chars / 4
    token_estimate_graph = json_chars / 4

    if token_estimate_raw > 0:
        compression_pct = round(
            (1 - token_estimate_graph / token_estimate_raw) * 100, 1
        )
    else:
        compression_pct = 0.0

    return {
        "original_lines": original_lines,
        "graph_nodes": graph_nodes,
        "json_chars": json_chars,
        "compression_pct": compression_pct,
        "token_estimate_raw": token_estimate_raw,
        "token_estimate_graph": token_estimate_graph,
    }
