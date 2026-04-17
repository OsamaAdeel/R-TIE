"""
Startup loader — orchestrates the complete parsing pipeline.
Called once when the application starts.
"""

import glob
import os
import traceback
from typing import Any

from src.parsing.parser import parse_function
from src.parsing.builder import build_function_graph
from src.parsing.indexer import (
    build_cross_function_graph,
    build_global_column_index,
    resolve_execution_order,
    build_alias_map,
)
from src.parsing.serializer import calculate_compression_ratio, to_json
from src.parsing.store import (
    store_function_graph,
    get_function_graph,
    store_full_graph,
    store_column_index,
    store_raw_source,
    is_graph_stale,
)
from src.logger import get_logger

logger = get_logger(__name__, concern="app")

# ---------------------------------------------------------------------------
# Project root resolution
# ---------------------------------------------------------------------------

# loader.py lives at src/parsing/loader.py
# RTIE root = 2 levels up from this file's directory
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_RTIE_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))


def _resolve_functions_dir(functions_dir: str) -> str | None:
    """Resolve *functions_dir* to an absolute path that exists.

    Checks (in order):
      1. The path as-is (already absolute or cwd-relative).
      2. Relative to the RTIE project root.

    Returns the first path that exists, or ``None``.
    """
    candidates = [
        functions_dir,
        os.path.join(_RTIE_ROOT, functions_dir),
    ]
    for candidate in candidates:
        resolved = os.path.abspath(candidate)
        if os.path.isdir(resolved):
            return resolved
    return None


def _function_name_from_file(file_path: str) -> str:
    """Derive a function name from the SQL file's basename (without extension)."""
    return os.path.splitext(os.path.basename(file_path))[0].upper()


# ===================================================================
# 1. Load all functions
# ===================================================================

def load_all_functions(
    functions_dir: str,
    schema: str,
    redis_client,
    force_reparse: bool = False,
) -> dict:
    """Scan *functions_dir* for ``*.sql`` files, parse each one, and build
    cross-function indices.

    Parameters
    ----------
    functions_dir:
        Directory containing ``.sql`` function files.  May be absolute or
        relative to the RTIE project root.
    schema:
        Oracle schema name used as a namespace in Redis keys.
    redis_client:
        Active Redis client instance.
    force_reparse:
        When ``True``, ignore cached graphs and re-parse every file.

    Returns
    -------
    dict
        Summary with keys: ``status``, ``functions_parsed``,
        ``functions_skipped``, ``functions_failed``, ``total_nodes``,
        ``total_edges``, ``compression_stats``, ``execution_order``,
        ``errors``.
    """
    resolved_dir = _resolve_functions_dir(functions_dir)

    if resolved_dir is None:
        msg = f"Functions directory not found: {functions_dir}"
        logger.error(msg)
        return {
            "status": "error",
            "functions_parsed": 0,
            "functions_skipped": 0,
            "functions_failed": 0,
            "total_nodes": 0,
            "total_edges": 0,
            "compression_stats": {},
            "execution_order": [],
            "errors": [msg],
        }

    sql_pattern = os.path.join(resolved_dir, "*.sql")
    sql_files = sorted(glob.glob(sql_pattern))

    if not sql_files:
        msg = f"No .sql files found in {resolved_dir}"
        logger.warning(msg)
        return {
            "status": "warning",
            "functions_parsed": 0,
            "functions_skipped": 0,
            "functions_failed": 0,
            "total_nodes": 0,
            "total_edges": 0,
            "compression_stats": {},
            "execution_order": [],
            "errors": [msg],
        }

    # ------------------------------------------------------------------
    # Per-function parse loop
    # ------------------------------------------------------------------
    all_graphs: dict[str, dict] = {}
    parsed_count = 0
    skipped_count = 0
    failed_count = 0
    errors: list[str] = []
    total_nodes = 0
    total_edges = 0
    compression_stats: list[dict] = []

    for sql_file in sql_files:
        func_name = _function_name_from_file(sql_file)
        try:
            # Staleness check
            if not force_reparse and not is_graph_stale(redis_client, schema, func_name, sql_file):
                # Use cached graph
                cached = get_function_graph(redis_client, schema, func_name)
                if cached is not None:
                    all_graphs[func_name] = cached
                    total_nodes += len(cached.get("nodes", []))
                    total_edges += len(cached.get("edges", []))
                    skipped_count += 1
                    logger.info("Skipped (cached) %s.%s", schema, func_name)
                    continue

            # Read source lines
            with open(sql_file, "r", encoding="utf-8") as fh:
                source_lines = fh.readlines()

            # Build function graph
            graph = build_function_graph(
                source_lines=source_lines,
                function_name=func_name,
                file_name=os.path.basename(sql_file),
                schema=schema,
            )

            # Store in Redis
            store_function_graph(redis_client, schema, func_name, graph)
            store_raw_source(redis_client, schema, func_name, source_lines)

            # Compression stats
            comp = calculate_compression_ratio(len(source_lines), graph)
            compression_stats.append({func_name: comp})

            all_graphs[func_name] = graph
            total_nodes += len(graph.get("nodes", []))
            total_edges += len(graph.get("edges", []))
            parsed_count += 1
            logger.info(
                "Parsed %s.%s — %d nodes, %d edges",
                schema,
                func_name,
                len(graph.get("nodes", [])),
                len(graph.get("edges", [])),
            )

        except Exception:
            tb = traceback.format_exc()
            err_msg = f"Failed to parse {func_name}: {tb}"
            errors.append(err_msg)
            failed_count += 1
            logger.error("Error parsing %s.%s:\n%s", schema, func_name, tb)

    # ------------------------------------------------------------------
    # Cross-function indices (only if we have at least one graph)
    # ------------------------------------------------------------------
    execution_order: list[str] = []

    if all_graphs:
        try:
            full_graph = build_cross_function_graph(list(all_graphs.values()))
            store_full_graph(redis_client, schema, full_graph)
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to build cross-function graph: {tb}")
            logger.error("Error building cross-function graph:\n%s", tb)

        try:
            column_index = build_global_column_index(list(all_graphs.values()))
            store_column_index(redis_client, schema, column_index)
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to build global column index: {tb}")
            logger.error("Error building global column index:\n%s", tb)

        try:
            execution_order = resolve_execution_order(list(all_graphs.values()))
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to resolve execution order: {tb}")
            logger.error("Error resolving execution order:\n%s", tb)

        try:
            alias_map = build_alias_map()
            # Store alias map in Redis using the standard key pattern
            from src.parsing.serializer import to_msgpack
            alias_key = f"graph:aliases:{schema}"
            redis_client.set(alias_key, to_msgpack(alias_map))
        except Exception:
            tb = traceback.format_exc()
            errors.append(f"Failed to build alias map: {tb}")
            logger.error("Error building alias map:\n%s", tb)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total_usable = parsed_count + skipped_count
    status = "success" if not errors else "partial" if total_usable > 0 else "error"

    summary = {
        "status": status,
        "functions_parsed": parsed_count,
        "functions_skipped": skipped_count,
        "functions_failed": failed_count,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "compression_stats": compression_stats,
        "execution_order": execution_order,
        "errors": errors,
    }

    logger.info(
        "Load complete for schema=%s — parsed=%d, skipped=%d, failed=%d, "
        "nodes=%d, edges=%d, status=%s",
        schema,
        parsed_count,
        skipped_count,
        failed_count,
        total_nodes,
        total_edges,
        status,
    )

    return summary


# ===================================================================
# 2. Parse a single function
# ===================================================================

def parse_single_function(
    sql_file_path: str,
    schema: str,
    redis_client,
) -> dict:
    """Parse and store a single SQL function file.

    Used by the ``/refresh-cache`` command to re-parse one function
    without reloading the entire pipeline.

    Parameters
    ----------
    sql_file_path:
        Absolute or project-relative path to the ``.sql`` file.
    schema:
        Oracle schema name.
    redis_client:
        Active Redis client instance.

    Returns
    -------
    dict
        Result with keys: ``status``, ``function_name``, ``nodes``,
        ``edges``, ``compression``, ``error`` (if any).
    """
    # Resolve path — try as-is, then relative to project root
    resolved_path: str | None = None
    for candidate in [sql_file_path, os.path.join(_RTIE_ROOT, sql_file_path)]:
        abs_candidate = os.path.abspath(candidate)
        if os.path.isfile(abs_candidate):
            resolved_path = abs_candidate
            break

    if resolved_path is None:
        err = f"SQL file not found: {sql_file_path}"
        logger.error(err)
        return {
            "status": "error",
            "function_name": None,
            "nodes": 0,
            "edges": 0,
            "compression": {},
            "error": err,
        }

    func_name = _function_name_from_file(resolved_path)

    try:
        with open(resolved_path, "r", encoding="utf-8") as fh:
            source_lines = fh.readlines()

        graph = build_function_graph(
            source_lines=source_lines,
            function_name=func_name,
            file_name=os.path.basename(resolved_path),
            schema=schema,
        )

        store_function_graph(redis_client, schema, func_name, graph)
        store_raw_source(redis_client, schema, func_name, source_lines)

        comp = calculate_compression_ratio(len(source_lines), graph)
        node_count = len(graph.get("nodes", []))
        edge_count = len(graph.get("edges", []))

        logger.info(
            "Parsed single function %s.%s — %d nodes, %d edges",
            schema,
            func_name,
            node_count,
            edge_count,
        )

        return {
            "status": "success",
            "function_name": func_name,
            "nodes": node_count,
            "edges": edge_count,
            "compression": comp,
            "error": None,
        }

    except Exception:
        tb = traceback.format_exc()
        logger.error("Error parsing single function %s.%s:\n%s", schema, func_name, tb)
        return {
            "status": "error",
            "function_name": func_name,
            "nodes": 0,
            "edges": 0,
            "compression": {},
            "error": tb,
        }
