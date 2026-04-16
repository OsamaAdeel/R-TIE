"""
Builds cross-function graph and global column index from all function graphs.
"""

from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any


# ===================================================================
# 1. Cross-function graph
# ===================================================================

def build_cross_function_graph(function_graphs: list[dict]) -> dict:
    """Merge all per-function graphs into a single cross-function graph.

    Adds cross-function edges when Function A writes to table T and
    Function B reads from T (with matching columns).

    Returns a dict with keys:
        schema, built_at (ISO 8601), function_count, node_count,
        edge_count, nodes ({fn_name: [nodes]}), edges (cross + intra).
    """
    schema = ""
    all_nodes: dict[str, list[dict]] = {}
    all_edges: list[dict] = []
    cross_edge_idx = 0

    # -- Collect per-function nodes and intra-function edges --
    for fg in function_graphs:
        fn_name = fg.get("function", "")
        if not schema:
            schema = fg.get("schema", "")
        all_nodes[fn_name] = fg.get("nodes", [])
        all_edges.extend(fg.get("edges", []))

    # -- Build write-map: table -> [(fn_name, node, written_columns)] --
    write_map: dict[str, list[tuple[str, dict, set[str]]]] = defaultdict(list)
    # -- Build read-map:  table -> [(fn_name, node, read_columns)] --
    read_map: dict[str, list[tuple[str, dict, set[str]]]] = defaultdict(list)

    for fn_name, nodes in all_nodes.items():
        for node in nodes:
            target = (node.get("target_table") or "").upper()
            if target:
                written_cols = _extract_written_columns(node)
                write_map[target].append((fn_name, node, written_cols))

            for src in (node.get("source_tables") or []):
                src_upper = src.upper()
                read_cols = _extract_read_columns(node)
                read_map[src_upper].append((fn_name, node, read_cols))

    # -- Derive cross-function edges --
    for table, writers in write_map.items():
        readers = read_map.get(table, [])
        for w_fn, w_node, w_cols in writers:
            for r_fn, r_node, r_cols in readers:
                if w_fn == r_fn:
                    continue  # intra-function edges already captured
                matching = w_cols & r_cols if (w_cols and r_cols) else set()
                cross_edge_idx += 1
                all_edges.append({
                    "id": f"CROSS_E{cross_edge_idx}",
                    "from": w_node["id"],
                    "to": r_node["id"],
                    "type": "CROSS_FUNCTION_TABLE_FLOW",
                    "table": table,
                    "from_function": w_fn,
                    "to_function": r_fn,
                    "matching_columns": sorted(matching),
                })

    total_nodes = sum(len(ns) for ns in all_nodes.values())

    return {
        "schema": schema,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "function_count": len(all_nodes),
        "node_count": total_nodes,
        "edge_count": len(all_edges),
        "nodes": all_nodes,
        "edges": all_edges,
    }


# ===================================================================
# 2. Global column index
# ===================================================================

def build_global_column_index(function_graphs: list[dict]) -> dict:
    """Merge all per-function column_index dicts into a global index.

    Also indexes table names to all nodes that read/write that table.

    Returns ``{col_or_table_name: ["FN_NAME:node_id", ...]}``.
    """
    index: dict[str, list[str]] = defaultdict(list)

    for fg in function_graphs:
        fn_name = fg.get("function", "")

        # -- Merge the function-level column_index --
        for col, node_ids in (fg.get("column_index") or {}).items():
            col_upper = col.strip().upper()
            if not col_upper:
                continue
            for nid in node_ids:
                qualified = f"{fn_name}:{nid}"
                if qualified not in index[col_upper]:
                    index[col_upper].append(qualified)

        # -- Index table names -> nodes that touch them --
        for node in fg.get("nodes", []):
            nid = node.get("id", "")
            qualified = f"{fn_name}:{nid}"

            target = (node.get("target_table") or "").upper()
            if target:
                if qualified not in index[target]:
                    index[target].append(qualified)

            for src in (node.get("source_tables") or []):
                src_upper = src.upper()
                if qualified not in index[src_upper]:
                    index[src_upper].append(qualified)

    return dict(index)


# ===================================================================
# 3. Execution order (topological sort via Kahn's algorithm)
# ===================================================================

def resolve_execution_order(function_graphs: list[dict]) -> list[str]:
    """Topological sort based on table write->read dependencies.

    If Function A writes table T and Function B reads T, A must execute
    before B.  Uses Kahn's algorithm.  Returns ordered function names.
    """
    # Collect all function names
    fn_names: list[str] = [fg.get("function", "") for fg in function_graphs]
    fn_set = set(fn_names)

    # Build write-map: table -> set of functions that write it
    writers: dict[str, set[str]] = defaultdict(set)
    # Build read-map:  table -> set of functions that read from it
    readers: dict[str, set[str]] = defaultdict(set)

    for fg in function_graphs:
        fn_name = fg.get("function", "")
        for node in fg.get("nodes", []):
            target = (node.get("target_table") or "").upper()
            if target:
                writers[target].add(fn_name)
            for src in (node.get("source_tables") or []):
                readers[src.upper()].add(fn_name)

    # Build adjacency list and in-degree map
    adj: dict[str, set[str]] = defaultdict(set)
    in_degree: dict[str, int] = {fn: 0 for fn in fn_names}

    for table, w_fns in writers.items():
        r_fns = readers.get(table, set())
        for w in w_fns:
            for r in r_fns:
                if w != r and r not in adj[w]:
                    adj[w].add(r)
                    in_degree[r] = in_degree.get(r, 0) + 1

    # Kahn's algorithm
    queue: deque[str] = deque()
    for fn in fn_names:
        if in_degree.get(fn, 0) == 0:
            queue.append(fn)

    ordered: list[str] = []
    while queue:
        # Sort the current zero-in-degree batch for deterministic output
        batch = sorted(queue)
        queue.clear()
        for fn in batch:
            ordered.append(fn)
            for neighbour in sorted(adj.get(fn, set())):
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)

    # If there are functions not yet placed (cycle), append them at the end
    remaining = [fn for fn in fn_names if fn not in set(ordered)]
    ordered.extend(sorted(remaining))

    return ordered


# ===================================================================
# 4. Alias map (hardcoded business-term aliases)
# ===================================================================

def build_alias_map() -> dict:
    """Return a hardcoded map of business-friendly aliases to column names."""
    return {
        "EAD": ["N_EOP_BAL", "N_EAD", "N_EAD_DRAWN", "N_UNDRAWN_AMT"],
        "exposure balance": ["N_EOP_BAL"],
        "annual gross income": ["N_ANNUAL_GROSS_INCOME"],
        "beta factor": ["N_BETA_FACTOR"],
        "provision": ["N_PROVISION_AMOUNT", "N_ANNUAL_GROSS_INCOME"],
        "lob": ["V_LOB_CODE"],
        "product code": ["V_PROD_CODE"],
        "maturity date": ["D_MATURITY_DATE"],
        "gl code": ["V_GL_CODE"],
        "currency": ["V_CCY_CODE"],
        "branch code": ["V_BRANCH_CODE"],
        "gaap code": ["V_GAAP_CODE"],
        "exposure category": ["V_EXP_CATEGORY_CODE"],
        "exchange rate": ["N_EXCHANGE_RATE"],
        "deduction ratio": ["N_DEDUCTION_RATIO", "LN_DEDUCITON_RATIO_1", "LN_DEDUCITON_RATIO_2"],
        "account number": ["V_ACCOUNT_NUMBER", "V_ORIG_ACCT_NO"],
        "ops risk": ["N_ANNUAL_GROSS_INCOME", "N_ALPHA_PERCENT", "N_BETA_FACTOR"],
    }


# ===================================================================
# Internal helpers
# ===================================================================

def _extract_written_columns(node: dict) -> set[str]:
    """Extract the set of column names that a node writes (target columns).

    Pulls from ``column_maps`` keys (for INSERT/UPDATE/MERGE nodes) and
    from ``calculation`` entries.
    """
    cols: set[str] = set()

    col_maps = node.get("column_maps") or {}
    if isinstance(col_maps, dict):
        # INSERT-style: column_maps may have a "mapping" sub-dict
        mapping = col_maps.get("mapping")
        if isinstance(mapping, dict):
            for col in mapping:
                cols.add(col.strip().upper())
        # UPDATE-style: column_maps may have "assignments" list
        assignments = col_maps.get("assignments")
        if isinstance(assignments, list):
            for pair in assignments:
                if isinstance(pair, (list, tuple)) and len(pair) >= 1:
                    cols.add(str(pair[0]).strip().upper())
        # Also handle flat dict style (builder.py build_insert_node uses flat dict)
        if not mapping and not assignments:
            for col in col_maps:
                if col not in ("columns", "values", "mapping", "assignments"):
                    cols.add(col.strip().upper())

    for calc in (node.get("calculation") or []):
        if isinstance(calc, dict):
            col = calc.get("column", "")
            if col:
                cols.add(col.strip().upper())

    return cols


def _extract_read_columns(node: dict) -> set[str]:
    """Extract the set of column names that a node reads (source columns).

    Pulls from ``column_maps`` values, ``conditions``, and calculation
    expressions.
    """
    cols: set[str] = set()

    col_maps = node.get("column_maps") or {}
    if isinstance(col_maps, dict):
        # INSERT mapping values
        mapping = col_maps.get("mapping")
        if isinstance(mapping, dict):
            for expr in mapping.values():
                if isinstance(expr, str):
                    cols.update(_column_refs_from_expr(expr))
        # UPDATE assignments
        assignments = col_maps.get("assignments")
        if isinstance(assignments, list):
            for pair in assignments:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    cols.update(_column_refs_from_expr(str(pair[1])))
        # Flat dict values
        if not mapping and not assignments:
            for key, val in col_maps.items():
                if key not in ("columns", "values", "mapping", "assignments"):
                    if isinstance(val, str):
                        cols.update(_column_refs_from_expr(val))

    for cond in (node.get("conditions") or []):
        cond_text = cond if isinstance(cond, str) else cond.get("expression", "")
        cols.update(_column_refs_from_expr(cond_text))

    for calc in (node.get("calculation") or []):
        if isinstance(calc, dict):
            expr = calc.get("expression", "")
            cols.update(_column_refs_from_expr(expr))

    return cols


# SQL keywords to exclude from column-reference extraction
_SQL_KEYWORDS = frozenset({
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "ON",
    "INSERT", "INTO", "UPDATE", "SET", "DELETE", "MERGE", "WHEN",
    "MATCHED", "THEN", "ELSE", "END", "CASE", "NULL", "IS",
    "BETWEEN", "LIKE", "EXISTS", "AS", "JOIN", "LEFT", "RIGHT",
    "INNER", "OUTER", "FULL", "CROSS", "GROUP", "BY", "ORDER",
    "HAVING", "UNION", "ALL", "NVL", "COALESCE", "DECODE",
    "BEGIN", "LOOP", "FOR", "WHILE", "IF", "ELSIF", "COMMIT",
    "SYSDATE", "DUAL", "EXTRACT", "MONTH", "YEAR", "DAY",
    "TO_NUMBER", "TO_CHAR", "TO_DATE", "TRIM", "UPPER", "LOWER",
    "COUNT", "SUM", "AVG", "MIN", "MAX", "DISTINCT", "VALUES",
})

import re

_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\b")


def _column_refs_from_expr(expression: str) -> set[str]:
    """Extract plausible column name references from a SQL expression."""
    refs: set[str] = set()
    if not expression:
        return refs
    for m in _IDENT_RE.finditer(expression):
        token = m.group(1)
        parts = token.split(".")
        col = parts[-1]
        if col.upper() not in _SQL_KEYWORDS and not col.isdigit():
            refs.add(col.upper())
    return refs
