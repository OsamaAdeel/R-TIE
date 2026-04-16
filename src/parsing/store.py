"""
Redis storage and retrieval for all graph structures.
All reads/writes go through this module.
"""

import json
import os
import logging
from datetime import datetime, timezone

from src.parsing.serializer import to_msgpack, from_msgpack, to_json, from_json
from src.logger import get_logger

logger = get_logger(__name__)

REDIS_KEYS = {
    "function_graph": "graph:{schema}:{function_name}",
    "full_graph": "graph:full:{schema}",
    "column_index": "graph:index:{schema}",
    "alias_map": "graph:aliases:{schema}",
    "raw_source": "graph:source:{schema}:{function_name}",
    "parse_metadata": "graph:meta:{schema}:{function_name}",
}


def _key(pattern_name: str, **kwargs) -> str:
    """Build a Redis key from a pattern name and substitution values."""
    template = REDIS_KEYS[pattern_name]
    return template.format(**kwargs)


# ---------------------------------------------------------------------------
# Function graph
# ---------------------------------------------------------------------------

def store_function_graph(redis_client, schema: str, function_name: str, graph: dict) -> bool:
    """Store a single function's graph in Redis using MessagePack encoding."""
    try:
        key = _key("function_graph", schema=schema, function_name=function_name)
        data = to_msgpack(graph)
        redis_client.set(key, data)

        # Also store parse metadata alongside the graph
        meta_key = _key("parse_metadata", schema=schema, function_name=function_name)
        metadata = {
            "parsed_at": datetime.now(timezone.utc).isoformat(),
            "schema": schema,
            "function_name": function_name,
            "node_count": len(graph.get("nodes", [])),
            "edge_count": len(graph.get("edges", [])),
        }
        redis_client.set(meta_key, to_msgpack(metadata))
        return True
    except Exception as e:
        logger.warning("Failed to store function graph %s.%s: %s", schema, function_name, e)
        return False


def get_function_graph(redis_client, schema: str, function_name: str) -> dict | None:
    """Retrieve a single function's graph from Redis."""
    try:
        key = _key("function_graph", schema=schema, function_name=function_name)
        data = redis_client.get(key)
        if data is None:
            return None
        return from_msgpack(data)
    except Exception as e:
        logger.warning("Failed to get function graph %s.%s: %s", schema, function_name, e)
        return None


# ---------------------------------------------------------------------------
# Full graph
# ---------------------------------------------------------------------------

def store_full_graph(redis_client, schema: str, graph: dict) -> bool:
    """Store the full merged graph for a schema."""
    try:
        key = _key("full_graph", schema=schema)
        data = to_msgpack(graph)
        redis_client.set(key, data)
        return True
    except Exception as e:
        logger.warning("Failed to store full graph for %s: %s", schema, e)
        return False


def get_full_graph(redis_client, schema: str) -> dict | None:
    """Retrieve the full merged graph for a schema."""
    try:
        key = _key("full_graph", schema=schema)
        data = redis_client.get(key)
        if data is None:
            return None
        return from_msgpack(data)
    except Exception as e:
        logger.warning("Failed to get full graph for %s: %s", schema, e)
        return None


# ---------------------------------------------------------------------------
# Column index
# ---------------------------------------------------------------------------

def store_column_index(redis_client, schema: str, index: dict) -> bool:
    """Store the column-to-function index for a schema."""
    try:
        key = _key("column_index", schema=schema)
        data = to_msgpack(index)
        redis_client.set(key, data)
        return True
    except Exception as e:
        logger.warning("Failed to store column index for %s: %s", schema, e)
        return False


def get_column_index(redis_client, schema: str) -> dict | None:
    """Retrieve the column-to-function index for a schema."""
    try:
        key = _key("column_index", schema=schema)
        data = redis_client.get(key)
        if data is None:
            return None
        return from_msgpack(data)
    except Exception as e:
        logger.warning("Failed to get column index for %s: %s", schema, e)
        return None


# ---------------------------------------------------------------------------
# Raw source
# ---------------------------------------------------------------------------

def store_raw_source(redis_client, schema: str, function_name: str, lines: list[str]) -> bool:
    """Store the raw SQL source lines for a function."""
    try:
        key = _key("raw_source", schema=schema, function_name=function_name)
        data = to_msgpack(lines)
        redis_client.set(key, data)
        return True
    except Exception as e:
        logger.warning("Failed to store raw source %s.%s: %s", schema, function_name, e)
        return False


def get_raw_source(redis_client, schema: str, function_name: str) -> list[str] | None:
    """Retrieve the raw SQL source lines for a function."""
    try:
        key = _key("raw_source", schema=schema, function_name=function_name)
        data = redis_client.get(key)
        if data is None:
            return None
        return from_msgpack(data)
    except Exception as e:
        logger.warning("Failed to get raw source %s.%s: %s", schema, function_name, e)
        return None


# ---------------------------------------------------------------------------
# Parse metadata
# ---------------------------------------------------------------------------

def get_parse_metadata(redis_client, schema: str, function_name: str) -> dict | None:
    """Retrieve parse metadata (parsed_at, node/edge counts, etc.) for a function."""
    try:
        key = _key("parse_metadata", schema=schema, function_name=function_name)
        data = redis_client.get(key)
        if data is None:
            return None
        return from_msgpack(data)
    except Exception as e:
        logger.warning("Failed to get parse metadata %s.%s: %s", schema, function_name, e)
        return None


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------

def is_graph_stale(redis_client, schema: str, function_name: str, file_path: str) -> bool:
    """
    Check whether the cached graph is stale by comparing the file's mtime
    against the stored parsed_at timestamp.

    Returns True if the graph should be re-parsed (stale or missing).
    Returns True on any error (safe default: re-parse rather than serve stale data).
    """
    try:
        if not os.path.exists(file_path):
            return True

        file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path), tz=timezone.utc)

        metadata = get_parse_metadata(redis_client, schema, function_name)
        if metadata is None:
            return True

        parsed_at_str = metadata.get("parsed_at")
        if parsed_at_str is None:
            return True

        parsed_at = datetime.fromisoformat(parsed_at_str)
        # If the file was modified after we last parsed it, the graph is stale
        return file_mtime > parsed_at
    except Exception as e:
        logger.warning(
            "Failed to check staleness for %s.%s (%s): %s",
            schema, function_name, file_path, e,
        )
        return True


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def clear_all_graphs(redis_client, schema: str) -> int:
    """
    Delete all graph:* keys for a given schema.

    Returns the number of keys deleted, or 0 on failure.
    """
    try:
        pattern = f"graph:*{schema}*"
        cursor = 0
        deleted = 0
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=200)
            if keys:
                deleted += redis_client.delete(*keys)
            if cursor == 0:
                break
        logger.info("Cleared %d graph keys for schema %s", deleted, schema)
        return deleted
    except Exception as e:
        logger.warning("Failed to clear graphs for schema %s: %s", schema, e)
        return 0
