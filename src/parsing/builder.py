"""
Builds a complete function graph from parsed raw blocks.
Handles all node types, calculation types, and override patterns.
"""

import re
from datetime import datetime, timezone
from typing import Any

from src.parsing.parser import (
    parse_function,
    extract_table_names,
    extract_column_maps,
    extract_conditions,
)

# ---------------------------------------------------------------------------
# Node / calculation type constants
# ---------------------------------------------------------------------------

NODE_TYPES = {
    "INSERT", "UPDATE", "MERGE", "DELETE",
    "SCALAR_COMPUTE", "WHILE_LOOP", "FOR_LOOP",
}

CALC_TYPES = {"DIRECT", "ARITHMETIC", "CONDITIONAL", "FALLBACK"}

_ARITHMETIC_RE = re.compile(r"[+\-*/]")
_NVL_RE = re.compile(r"\bNVL\s*\(", re.IGNORECASE)
_COALESCE_RE = re.compile(r"\bCOALESCE\s*\(", re.IGNORECASE)
_DECODE_RE = re.compile(r"\bDECODE\s*\(", re.IGNORECASE)
_CASE_RE = re.compile(r"\bCASE\s+WHEN\b", re.IGNORECASE)
_UNION_RE = re.compile(r"\bUNION\s+ALL\b|\bUNION\b", re.IGNORECASE)
_COL_REF_RE = re.compile(
    r"(?:([A-Za-z_]\w*)\.)?([A-Za-z_]\w*)",
)

# PL/SQL keywords that should not be treated as variable references
_PLSQL_KEYWORDS = frozenset({
    "SELECT", "FROM", "WHERE", "SET", "INTO", "VALUES", "ON", "AND", "OR",
    "NOT", "NULL", "IS", "IN", "BETWEEN", "LIKE", "EXISTS", "CASE", "WHEN",
    "THEN", "ELSE", "END", "AS", "ALL", "DUAL", "MATCHED", "USING",
    "LOOP", "IF", "ELSIF", "BEGIN", "RETURN", "EXCEPTION", "COMMIT",
    "ROLLBACK", "DECLARE", "CURSOR", "OPEN", "FETCH", "CLOSE", "FOR",
    "WHILE", "EXIT", "PRAGMA", "SYSDATE", "SYSTIMESTAMP", "TRUE", "FALSE",
    "INSERT", "UPDATE", "DELETE", "MERGE", "JOIN", "LEFT", "RIGHT",
    "INNER", "OUTER", "FULL", "CROSS", "GROUP", "BY", "ORDER", "HAVING",
    "UNION", "NVL", "COALESCE", "DECODE", "EXTRACT", "MONTH", "YEAR",
})


# ===================================================================
# 1. Main entry
# ===================================================================

def build_function_graph(
    source_lines: list[str],
    function_name: str,
    file_name: str,
    schema: str,
    hierarchy: dict | None = None,
) -> dict:
    """Build a complete function graph from PL/SQL source lines.

    Calls ``parse_function`` to obtain raw blocks, then converts each
    block into a typed node, derives edges and a column index.

    Parameters
    ----------
    source_lines, function_name, file_name, schema:
        Required inputs as before.
    hierarchy:
        Optional dict of batch/process/sub-process/task metadata produced
        by :meth:`TaskEntry.to_node_hierarchy`. When provided it is stored
        at the top of the returned graph AND copied onto every node so
        downstream consumers (query_engine, logic_explainer) can filter
        by ``hierarchy.active`` and render hierarchy headers without an
        extra lookup.

    Returns:
        A dict with keys: function, file, schema, parsed_at,
        total_source_lines, execution_condition, nodes, edges,
        commented_out_nodes, column_index, and (optionally) hierarchy.
    """
    parsed = parse_function(source_lines, function_name)

    raw_blocks = parsed.get("raw_blocks", [])
    execution_condition = parsed.get("execution_condition", None)

    # Separate active blocks from commented-out blocks
    commented_blocks = [b for b in raw_blocks if b.get("is_commented_out")]
    raw_blocks = [b for b in raw_blocks if not b.get("is_commented_out")]

    nodes: list[dict] = []
    commented_out_nodes: list[dict] = []

    for idx, block in enumerate(raw_blocks):
        node_id = f"{function_name}_N{idx + 1}"
        node = build_node(block, node_id)
        if node is not None:
            if hierarchy is not None:
                node["hierarchy"] = dict(hierarchy)
            nodes.append(node)

    for idx, block in enumerate(commented_blocks):
        node_id = f"{function_name}_COMMENTED_{idx + 1}"
        node = build_node(block, node_id)
        if node is not None:
            if hierarchy is not None:
                node["hierarchy"] = dict(hierarchy)
            commented_out_nodes.append(node)

    edges = build_intra_function_edges(nodes, function_name)
    column_index = build_function_column_index(nodes, function_name)

    graph: dict = {
        "function": function_name,
        "file": file_name,
        "schema": schema,
        "parsed_at": datetime.now(timezone.utc).isoformat(),
        "total_source_lines": len(source_lines),
        "execution_condition": execution_condition,
        "nodes": nodes,
        "edges": edges,
        "commented_out_nodes": commented_out_nodes,
        "column_index": column_index,
    }
    if hierarchy is not None:
        graph["hierarchy"] = dict(hierarchy)
    return graph


# ===================================================================
# 2. Node router
# ===================================================================

_BUILDER_DISPATCH: dict[str, Any] = {}  # populated after function defs


def build_node(raw_block: dict, node_id: str) -> dict | None:
    """Route *raw_block* to the specialist builder for its ``block_type``."""
    block_type = raw_block.get("block_type", "").upper()

    builder_fn = _BUILDER_DISPATCH.get(block_type)
    if builder_fn is None:
        return None
    return builder_fn(raw_block, node_id)


# ===================================================================
# 3. INSERT node
# ===================================================================

def build_insert_node(raw_block: dict, node_id: str) -> dict:
    """Build graph node for an INSERT statement.

    Detects UNION arms when the SELECT half contains UNION / UNION ALL
    and builds per-arm column maps.
    """
    raw_lines: list[str] = raw_block.get("cleaned_lines") or raw_block.get("raw_lines", [])
    block_type = raw_block.get("block_type", "INSERT")
    body = "\n".join(raw_lines)

    tables = extract_table_names(raw_lines, block_type)
    target_table = tables.get("target_table", "")
    source_tables = tables.get("source_tables", [])

    union_arms = build_union_arms(raw_lines)

    if union_arms:
        # Build column maps / calculations per arm
        for arm in union_arms:
            arm_maps = extract_column_maps(arm.get("raw_lines", []), "INSERT")
            arm["column_maps"] = arm_maps
            arm["calculations"] = _build_calculations_from_maps(
                arm_maps, arm.get("raw_lines", []), arm.get("line_start", 0),
            )
    # Overall column maps from the full block
    column_maps = extract_column_maps(raw_lines, block_type)
    conditions = extract_conditions(raw_lines)
    calculations = _build_calculations_from_maps(
        column_maps, raw_lines, raw_block.get("line_start", 0),
    )

    summary = _summarise_insert(target_table, source_tables, conditions)

    node: dict = {
        "id": node_id,
        "type": "INSERT",
        "line_start": raw_block.get("line_start"),
        "line_end": raw_block.get("line_end"),
        "committed_after": raw_block.get("followed_by_commit", False),
        "target_table": target_table,
        "source_tables": source_tables,
        "column_maps": column_maps,
        "calculation": calculations,
        "conditions": conditions,
        "overrides": _collect_overrides(calculations),
        "summary": summary,
    }
    if union_arms:
        node["union_arms"] = union_arms
    return node


# ===================================================================
# 4. UPDATE node
# ===================================================================

def build_update_node(raw_block: dict, node_id: str) -> dict:
    """Build graph node for an UPDATE / SET statement."""
    raw_lines = raw_block.get("cleaned_lines") or raw_block.get("raw_lines", [])
    block_type = raw_block.get("block_type", "UPDATE")
    tables = extract_table_names(raw_lines, block_type)
    target_table = tables.get("target_table", "")
    source_tables = tables.get("source_tables", [])

    column_maps = extract_column_maps(raw_lines, block_type)
    conditions = extract_conditions(raw_lines)
    calculations = _build_calculations_from_maps(
        column_maps, raw_lines, raw_block.get("line_start", 0),
    )

    summary = _summarise_update(target_table, column_maps, conditions)

    return {
        "id": node_id,
        "type": "UPDATE",
        "line_start": raw_block.get("line_start"),
        "line_end": raw_block.get("line_end"),
        "committed_after": raw_block.get("followed_by_commit", False),
        "target_table": target_table,
        "source_tables": source_tables,
        "column_maps": column_maps,
        "calculation": calculations,
        "conditions": conditions,
        "overrides": _collect_overrides(calculations),
        "summary": summary,
    }


# ===================================================================
# 4b. DELETE node
# ===================================================================

def build_delete_node(raw_block: dict, node_id: str) -> dict:
    """Build graph node for a DELETE statement.

    DELETE removes rows from a target table; it has no SET clause and
    no column mappings, so the node shape is intentionally narrower
    than UPDATE/INSERT/MERGE: ``target_table``, parsed WHERE
    ``conditions``, the line range, and the ``committed_after`` flag.
    Pre-Phase-8 DELETE blocks were dispatched to ``build_update_node``
    and emerged misclassified as ``type="UPDATE"`` with empty
    ``column_maps``; downstream consumers (Phase 2 ``proof_builder``,
    ``query_templates``) saw them as no-op updates instead of as a
    distinct operation.
    """
    raw_lines = raw_block.get("cleaned_lines") or raw_block.get("raw_lines", [])
    block_type = raw_block.get("block_type", "DELETE")
    tables = extract_table_names(raw_lines, block_type)
    target_table = tables.get("target_table", "")
    source_tables = tables.get("source_tables", [])
    conditions = extract_conditions(raw_lines)

    summary = _summarise_delete(target_table, conditions)

    return {
        "id": node_id,
        "type": "DELETE",
        "line_start": raw_block.get("line_start"),
        "line_end": raw_block.get("line_end"),
        "committed_after": raw_block.get("followed_by_commit", False),
        "target_table": target_table,
        "source_tables": source_tables,
        "conditions": conditions,
        "summary": summary,
    }


# ===================================================================
# 5. MERGE node
# ===================================================================

def build_merge_node(raw_block: dict, node_id: str) -> dict:
    """Build graph node for a MERGE statement.

    Extracts WHEN MATCHED and WHEN NOT MATCHED clauses.
    """
    raw_lines = raw_block.get("cleaned_lines") or raw_block.get("raw_lines", [])
    block_type = raw_block.get("block_type", "MERGE")
    tables = extract_table_names(raw_lines, block_type)
    target_table = tables.get("target_table", "")
    source_tables = tables.get("source_tables", [])

    column_maps = extract_column_maps(raw_lines, block_type)
    conditions = extract_conditions(raw_lines)

    matched_block, not_matched_block = _split_merge_clauses(raw_lines)

    matched_maps = extract_column_maps(matched_block, "MERGE") if matched_block else {}
    not_matched_maps = extract_column_maps(not_matched_block, "MERGE") if not_matched_block else {}

    line_start = raw_block.get("line_start", 0)
    matched_calcs = _build_calculations_from_maps(matched_maps, matched_block or [], line_start)
    not_matched_calcs = _build_calculations_from_maps(not_matched_maps, not_matched_block or [], line_start)

    all_calculations = matched_calcs + not_matched_calcs

    summary = _summarise_merge(target_table, source_tables, matched_maps, not_matched_maps)

    return {
        "id": node_id,
        "type": "MERGE",
        "line_start": raw_block.get("line_start"),
        "line_end": raw_block.get("line_end"),
        "committed_after": raw_block.get("followed_by_commit", False),
        "target_table": target_table,
        "source_tables": source_tables,
        "column_maps": column_maps,
        "calculation": all_calculations,
        "conditions": conditions,
        "overrides": _collect_overrides(all_calculations),
        "summary": summary,
        "when_matched": {
            "column_maps": matched_maps,
            "calculations": matched_calcs,
        },
        "when_not_matched": {
            "column_maps": not_matched_maps,
            "calculations": not_matched_calcs,
        },
    }


# ===================================================================
# 6. SCALAR_COMPUTE node (SELECT INTO)
# ===================================================================

def build_scalar_compute_node(raw_block: dict, node_id: str) -> dict:
    """Build graph node for a SELECT ... INTO or VAR := expr assignment."""
    raw_lines = raw_block.get("cleaned_lines") or raw_block.get("raw_lines", [])
    block_type = raw_block.get("block_type", "SCALAR_COMPUTE")
    body = "\n".join(raw_lines)

    # Check if this is a direct assignment block (from parser ASSIGNMENT detection)
    direct_output_var = raw_block.get("output_variable")
    direct_expression = raw_block.get("expression")

    if direct_output_var and direct_expression:
        # Assignment-style SCALAR_COMPUTE (VAR := expr;)
        output_variable = direct_output_var
        expression = direct_expression
        source_tables: list[str] = []
        conditions: list[str] = []
    else:
        # SELECT ... INTO style
        tables = extract_table_names(raw_lines, block_type)
        source_tables = tables.get("source_tables", [])
        conditions = extract_conditions(raw_lines)
        output_variable = _extract_into_variable(body)
        expression = _extract_select_expression(body)

    line_start = raw_block.get("line_start", 0)
    calculation = build_calculation_block(
        output_variable or "result", expression, raw_lines, line_start,
    )

    summary = _summarise_scalar(output_variable, source_tables, expression)

    return {
        "id": node_id,
        "type": "SCALAR_COMPUTE",
        "line_start": raw_block.get("line_start"),
        "line_end": raw_block.get("line_end"),
        "committed_after": raw_block.get("followed_by_commit", False),
        "target_table": None,
        "source_tables": source_tables,
        "column_maps": {},
        "calculation": [calculation] if calculation else [],
        "conditions": conditions,
        "overrides": _collect_overrides([calculation] if calculation else []),
        "output_variable": output_variable,
        "summary": summary,
    }


# ===================================================================
# 7. WHILE_LOOP node
# ===================================================================

def build_while_loop_node(raw_block: dict, node_id: str) -> dict:
    """Build graph node for a WHILE loop construct."""
    raw_lines = raw_block.get("cleaned_lines") or raw_block.get("raw_lines", [])
    block_type = raw_block.get("block_type", "WHILE")
    body = "\n".join(raw_lines)

    loop_definition = _extract_while_definition(body, raw_lines)
    iterations = infer_loop_iterations(loop_definition, raw_lines)

    inner_blocks = raw_block.get("inner_blocks", [])
    inner_node = None
    if inner_blocks:
        inner_id = f"{node_id}_INNER"
        inner_node = build_node(inner_blocks[0], inner_id)

    tables = extract_table_names(raw_lines, block_type)
    conditions = extract_conditions(raw_lines)

    summary = _summarise_while(loop_definition, inner_node)

    return {
        "id": node_id,
        "type": "WHILE_LOOP",
        "line_start": raw_block.get("line_start"),
        "line_end": raw_block.get("line_end"),
        "committed_after": raw_block.get("followed_by_commit", False),
        "target_table": tables.get("target_table"),
        "source_tables": tables.get("source_tables", []),
        "column_maps": {},
        "calculation": [],
        "conditions": conditions,
        "overrides": [],
        "summary": summary,
        "loop_definition": loop_definition,
        "iterations": iterations,
        "inner_node": inner_node,
    }


# ===================================================================
# 8. FOR_LOOP node
# ===================================================================

def build_for_loop_node(raw_block: dict, node_id: str) -> dict:
    """Build graph node for a FOR loop (cursor-based)."""
    raw_lines = raw_block.get("cleaned_lines") or raw_block.get("raw_lines", [])
    block_type = raw_block.get("block_type", "FOR_LOOP")
    body = "\n".join(raw_lines)

    cursor_query = _extract_cursor_query(body)
    inner_blocks = raw_block.get("inner_blocks", [])
    inner_operations: list[dict] = []
    for idx, ib in enumerate(inner_blocks):
        inner_id = f"{node_id}_OP{idx + 1}"
        inner_op = build_node(ib, inner_id)
        if inner_op is not None:
            inner_operations.append(inner_op)

    tables = extract_table_names(raw_lines, block_type)
    conditions = extract_conditions(raw_lines)

    summary = _summarise_for(cursor_query, inner_operations)

    return {
        "id": node_id,
        "type": "FOR_LOOP",
        "line_start": raw_block.get("line_start"),
        "line_end": raw_block.get("line_end"),
        "committed_after": raw_block.get("followed_by_commit", False),
        "target_table": tables.get("target_table"),
        "source_tables": tables.get("source_tables", []),
        "column_maps": {},
        "calculation": [],
        "conditions": conditions,
        "overrides": [],
        "summary": summary,
        "cursor_query": cursor_query,
        "inner_operations": inner_operations,
    }


# ===================================================================
# 9. Calculation block builder
# ===================================================================

def build_calculation_block(
    column: str,
    expression: str,
    raw_lines: list[str],
    line_num: int,
) -> dict:
    """Classify *expression* and return a typed calculation dict.

    Priority order:
      1. NVL / COALESCE  -> FALLBACK
      2. DECODE / CASE   -> CONDITIONAL (with optional overrides)
      3. Arithmetic ops  -> ARITHMETIC
      4. Else            -> DIRECT
    """
    expr_upper = expression.upper().strip() if expression else ""

    # --- 1. FALLBACK: NVL / COALESCE ---
    if _NVL_RE.search(expression) or _COALESCE_RE.search(expression):
        primary, fallback = _parse_fallback_args(expression)
        return {
            "column": column,
            "type": "FALLBACK",
            "expression": expression.strip(),
            "primary": primary,
            "fallback": fallback,
            "line": line_num,
        }

    # --- 2. CONDITIONAL: DECODE / CASE WHEN ---
    if _DECODE_RE.search(expression):
        branches, overrides = _parse_decode(expression)
        return {
            "column": column,
            "type": "CONDITIONAL",
            "expression": expression.strip(),
            "branches": branches,
            "overrides": overrides,
            "line": line_num,
        }

    if _CASE_RE.search(expression):
        branches = _parse_case_when(expression, line_num)
        return {
            "column": column,
            "type": "CONDITIONAL",
            "expression": expression.strip(),
            "branches": branches,
            "overrides": [],
            "line": line_num,
        }

    # --- 3. ARITHMETIC ---
    # Strip string literals to avoid false positives on operators inside literals
    sanitised = re.sub(r"'[^']*'", "", expression)
    if _ARITHMETIC_RE.search(sanitised):
        components = _parse_arithmetic_components(expression)
        return {
            "column": column,
            "type": "ARITHMETIC",
            "expression": expression.strip(),
            "components": components,
            "line": line_num,
        }

    # --- 4. VARIABLE_REFERENCE: local variable (no dot, not a known literal) ---
    expr_stripped = expression.strip()
    if (re.match(r'^[A-Za-z_]\w*$', expr_stripped)
            and '.' not in expr_stripped
            and not _is_literal(expr_stripped)
            and expr_stripped.upper() not in _PLSQL_KEYWORDS):
        return {
            "column": column,
            "type": "VARIABLE_REFERENCE",
            "expression": expr_stripped,
            "variable": expr_stripped,
            "line": line_num,
        }

    # --- 5. DIRECT ---
    source_table, source_column = _parse_direct_ref(expression)
    return {
        "column": column,
        "type": "DIRECT",
        "expression": expression.strip(),
        "source_table": source_table,
        "source_column": source_column,
        "line": line_num,
    }


# ===================================================================
# 10. UNION arm splitter
# ===================================================================

def build_union_arms(raw_lines: list[str]) -> list[dict]:
    """Split an INSERT body on UNION / UNION ALL boundaries.

    Returns a list of arm dicts, each with ``arm_index``, ``union_type``,
    ``line_start``, ``line_end``, and ``raw_lines``.  Returns an empty
    list when the statement does not contain a UNION.
    """
    body = "\n".join(raw_lines)
    if not _UNION_RE.search(body):
        return []

    arms: list[dict] = []
    current_arm_lines: list[str] = []
    arm_start = 0
    arm_index = 0

    for idx, line in enumerate(raw_lines):
        match = _UNION_RE.search(line)
        if match:
            # Close current arm
            if current_arm_lines:
                arms.append({
                    "arm_index": arm_index,
                    "union_type": None if arm_index == 0 else "UNION ALL" if "ALL" in match.group().upper() else "UNION",
                    "line_start": arm_start,
                    "line_end": idx - 1,
                    "raw_lines": current_arm_lines,
                })
                arm_index += 1
            current_arm_lines = []
            arm_start = idx + 1
        else:
            current_arm_lines.append(line)

    # Final arm
    if current_arm_lines:
        arms.append({
            "arm_index": arm_index,
            "union_type": "UNION ALL" if arm_index > 0 else None,
            "line_start": arm_start,
            "line_end": len(raw_lines) - 1,
            "raw_lines": current_arm_lines,
        })

    return arms if len(arms) > 1 else []


# ===================================================================
# 11. Intra-function edges
# ===================================================================

def build_intra_function_edges(nodes: list[dict], function_name: str) -> list[dict]:
    """Derive edges between nodes within the same function.

    An edge is created when:
      - A SCALAR_COMPUTE ``output_variable`` is referenced in a later node.
      - A node writes to a table that a later node reads from.
    """
    edges: list[dict] = []
    edge_idx = 0

    for i, src_node in enumerate(nodes):
        for j in range(i + 1, len(nodes)):
            tgt_node = nodes[j]

            # --- variable-flow edge (SCALAR_COMPUTE -> consumer) ---
            if src_node.get("type") == "SCALAR_COMPUTE":
                out_var = src_node.get("output_variable", "")
                if out_var and _node_references_variable(tgt_node, out_var):
                    sc_node_id = src_node["id"]
                    consuming_node_id = tgt_node["id"]
                    edge_idx += 1
                    edges.append({
                        "id": f"{function_name}_E{edge_idx}",
                        "from_node": f"{function_name}:{sc_node_id}",
                        "to_node": f"{function_name}:{consuming_node_id}",
                        "via_table": None,
                        "source_col": out_var,
                        "target_col": "computed",
                        "transform": "variable_reference",
                        "description": f"Variable {out_var} computed in {sc_node_id} consumed by {consuming_node_id}",
                        # Legacy keys for backward compatibility
                        "from": sc_node_id,
                        "to": consuming_node_id,
                        "type": "VARIABLE_FLOW",
                        "variable": out_var,
                    })

            # --- table-flow edge ---
            src_target = (src_node.get("target_table") or "").upper()
            if src_target:
                tgt_sources = [s.upper() for s in (tgt_node.get("source_tables") or [])]
                if src_target in tgt_sources:
                    edge_idx += 1
                    edges.append({
                        "id": f"{function_name}_E{edge_idx}",
                        "from": src_node["id"],
                        "to": tgt_node["id"],
                        "type": "TABLE_FLOW",
                        "table": src_target,
                    })

    return edges


# ===================================================================
# 12. Column index
# ===================================================================

def build_function_column_index(nodes: list[dict], function_name: str) -> dict:
    """Build ``{column_name: [node_ids]}`` from nodes' column maps,
    conditions, and calculations."""
    index: dict[str, list[str]] = {}

    def _register(col: str, nid: str) -> None:
        col_upper = col.strip().upper()
        if not col_upper:
            return
        index.setdefault(col_upper, [])
        if nid not in index[col_upper]:
            index[col_upper].append(nid)

    for node in nodes:
        nid = node["id"]

        # column_maps — handle both INSERT format (has "mapping" sub-dict)
        # and UPDATE format (flat dict of col: expr)
        col_maps_raw = node.get("column_maps") or {}
        if isinstance(col_maps_raw, dict):
            # INSERT format: {"columns": [...], "values": [...], "mapping": {...}}
            if "mapping" in col_maps_raw:
                actual_map = col_maps_raw.get("mapping", {})
                # Also register column names from the "columns" list
                for col_name in col_maps_raw.get("columns", []):
                    if isinstance(col_name, str):
                        _register(col_name, nid)
                # Also register values
                for val in col_maps_raw.get("values", []):
                    if isinstance(val, str):
                        for ref in _extract_column_names_from_expr(val):
                            _register(ref, nid)
            # UPDATE format: {"assignments": [(col, expr), ...]}
            elif "assignments" in col_maps_raw:
                actual_map = {}
                for col, expr in col_maps_raw.get("assignments", []):
                    actual_map[col] = expr
            else:
                actual_map = col_maps_raw
            for target_col, src_expr in actual_map.items():
                _register(target_col, nid)
                if isinstance(src_expr, str):
                    for ref in _extract_column_names_from_expr(src_expr):
                        _register(ref, nid)

        # Also register output_variable for SCALAR_COMPUTE nodes
        ov = node.get("output_variable")
        if ov:
            _register(ov, nid)

        # conditions
        for cond in (node.get("conditions") or []):
            cond_text = cond if isinstance(cond, str) else cond.get("expression", "")
            for ref in _extract_column_names_from_expr(cond_text):
                _register(ref, nid)

        # calculations
        for calc in (node.get("calculation") or []):
            if isinstance(calc, dict):
                _register(calc.get("column", ""), nid)
                expr = calc.get("expression", "")
                for ref in _extract_column_names_from_expr(expr):
                    _register(ref, nid)

        # For WHILE_LOOP: recurse into inner_node
        inner = node.get("inner_node")
        if inner and isinstance(inner, dict):
            inner_maps = inner.get("column_maps") or {}
            if isinstance(inner_maps, dict):
                if "mapping" in inner_maps:
                    for col in inner_maps.get("columns", []):
                        if isinstance(col, str):
                            _register(col, nid)
                    for val in inner_maps.get("values", []):
                        if isinstance(val, str):
                            for ref in _extract_column_names_from_expr(val):
                                _register(ref, nid)
                    for col in inner_maps.get("mapping", {}).keys():
                        _register(col, nid)
                elif "assignments" in inner_maps:
                    for col, expr in inner_maps.get("assignments", []):
                        _register(col, nid)
                        if isinstance(expr, str):
                            for ref in _extract_column_names_from_expr(expr):
                                _register(ref, nid)
                else:
                    for col, expr in inner_maps.items():
                        _register(col, nid)
                        if isinstance(expr, str):
                            for ref in _extract_column_names_from_expr(expr):
                                _register(ref, nid)
            for src in (inner.get("source_tables") or []):
                _register(src, nid)
            if inner.get("target_table"):
                _register(inner["target_table"], nid)

        # For FOR_LOOP: recurse into inner_operations
        for inner_op in (node.get("inner_operations") or []):
            if isinstance(inner_op, dict):
                for col in (inner_op.get("column_maps") or {}).keys():
                    _register(col, nid)
                for src in (inner_op.get("source_tables") or []):
                    _register(src, nid)
                if inner_op.get("target_table"):
                    _register(inner_op["target_table"], nid)

    return index


# ===================================================================
# 13. Infer loop iterations
# ===================================================================

def infer_loop_iterations(loop_definition: dict, raw_lines: list[str]) -> list[dict]:
    """For a WHILE loop with a known counter range, infer iteration details.

    Returns a list of ``iteration_detail`` dicts, one per predicted
    iteration, or an empty list when the range cannot be determined.
    """
    counter = loop_definition.get("counter")
    start = loop_definition.get("start")
    end = loop_definition.get("termination_value")
    increment = loop_definition.get("increment", 1)

    if counter is None or start is None or end is None:
        return []

    try:
        start_val = int(start)
        end_val = int(end)
        inc_val = int(increment)
    except (ValueError, TypeError):
        return []

    if inc_val == 0:
        return []

    iterations: list[dict] = []
    current = start_val
    safety = 0
    max_iters = 10000  # guard against infinite expansion

    while safety < max_iters:
        if inc_val > 0 and current > end_val:
            break
        if inc_val < 0 and current < end_val:
            break
        iterations.append({
            "iteration": safety + 1,
            "counter_value": current,
            "counter_variable": counter,
        })
        current += inc_val
        safety += 1

    return iterations


# ===================================================================
# Dispatch table (populated after all builder fns are defined)
# ===================================================================

_BUILDER_DISPATCH.update({
    "INSERT": build_insert_node,
    "UPDATE": build_update_node,
    "MERGE": build_merge_node,
    "DELETE": build_delete_node,
    "SCALAR_COMPUTE": build_scalar_compute_node,
    "SELECT_INTO": build_scalar_compute_node,  # SELECT INTO → SCALAR_COMPUTE
    "WHILE": build_while_loop_node,
    "WHILE_LOOP": build_while_loop_node,
    "FOR_LOOP": build_for_loop_node,
})


# ===================================================================
# Internal helpers
# ===================================================================

def _build_calculations_from_maps(
    column_maps: dict,
    raw_lines: list[str],
    line_start: int,
) -> list[dict]:
    """Create a calculation block for every column map entry whose
    value is a non-trivial expression."""
    calcs: list[dict] = []
    for idx, (col, expr) in enumerate(column_maps.items()):
        if not isinstance(expr, str):
            continue
        calc = build_calculation_block(col, expr, raw_lines, line_start + idx)
        calcs.append(calc)
    return calcs


def _collect_overrides(calculations: list[dict]) -> list[dict]:
    """Gather all override entries from a list of calculation blocks."""
    overrides: list[dict] = []
    for calc in calculations:
        if isinstance(calc, dict):
            overrides.extend(calc.get("overrides", []))
    return overrides


# --------------- DECODE / CASE / NVL parsing helpers ----------------

def _parse_fallback_args(expression: str) -> tuple[str, str]:
    """Extract primary and fallback from NVL(a, b) or COALESCE(a, b, ...)."""
    inner = _extract_paren_content(expression, r"(?:NVL|COALESCE)\s*\(")
    if not inner:
        return (expression.strip(), "")
    parts = _split_top_level(inner, ",")
    primary = parts[0].strip() if parts else ""
    fallback = parts[1].strip() if len(parts) > 1 else ""
    return (primary, fallback)


def _parse_decode(expression: str) -> tuple[list[dict], list[dict]]:
    """Parse DECODE(expr, val1, result1, ..., default).

    Returns (branches, overrides).  An override is detected when a
    search value is a string/numeric literal that maps to a hardcoded
    result value (not a column reference).
    """
    inner = _extract_paren_content(expression, r"DECODE\s*\(")
    if not inner:
        return ([], [])

    args = _split_top_level(inner, ",")
    if len(args) < 3:
        return ([], [])

    decode_expr = args[0].strip()
    remaining = args[1:]
    branches: list[dict] = []
    overrides: list[dict] = []

    # Detect COMPOSITE_KEY: col1 || '-' || col2
    is_composite = "||" in decode_expr

    idx = 0
    while idx + 1 < len(remaining):
        search_val = remaining[idx].strip()
        result_val = remaining[idx + 1].strip()
        branch = {
            "when": f"{decode_expr} = {search_val}",
            "then": result_val,
        }
        branches.append(branch)

        # Check if this is a hardcoded override
        if _is_literal(search_val) and _is_literal(result_val):
            override_type = "COMPOSITE_KEY" if is_composite else "SINGLE_COLUMN"
            overrides.append({
                "type": override_type,
                "decode_expression": decode_expr,
                "search_value": search_val,
                "result_value": result_val,
            })

        idx += 2

    # Default (odd trailing argument)
    if idx < len(remaining):
        default_val = remaining[idx].strip()
        branches.append({
            "when": "DEFAULT",
            "then": default_val,
        })

    return (branches, overrides)


def _parse_case_when(expression: str, base_line: int) -> list[dict]:
    """Parse CASE WHEN ... THEN ... ELSE ... END into branch dicts."""
    branches: list[dict] = []
    pattern = re.compile(
        r"WHEN\s+(.+?)\s+THEN\s+(.+?)(?=\s+WHEN\b|\s+ELSE\b|\s+END\b)",
        re.IGNORECASE | re.DOTALL,
    )
    for idx, m in enumerate(pattern.finditer(expression)):
        branches.append({
            "when": m.group(1).strip(),
            "then": m.group(2).strip(),
            "formula": m.group(2).strip(),
            "line": base_line + idx,
        })

    else_match = re.search(r"ELSE\s+(.+?)\s+END", expression, re.IGNORECASE | re.DOTALL)
    if else_match:
        branches.append({
            "when": "ELSE",
            "then": else_match.group(1).strip(),
            "formula": else_match.group(1).strip(),
            "line": base_line + len(branches),
        })

    return branches


def _parse_arithmetic_components(expression: str) -> list[dict]:
    """Break an arithmetic expression into operand/operator components.

    Operands that look like local variables (no dot qualifier, not a
    literal, not a keyword) are tagged as ``VARIABLE_REFERENCE``.
    """
    components: list[dict] = []
    # Tokenise on operators while keeping them
    tokens = re.split(r"(\s*[+\-*/]\s*)", expression)
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        if token in ("+", "-", "*", "/"):
            components.append({"type": "operator", "value": token})
        else:
            # Detect local variable references (plain identifier, no dot)
            if (re.match(r'^[A-Za-z_]\w*$', token)
                    and '.' not in token
                    and not _is_literal(token)
                    and token.upper() not in _PLSQL_KEYWORDS):
                components.append({"type": "VARIABLE_REFERENCE", "value": token})
            else:
                components.append({"type": "operand", "value": token})
    return components


def _parse_direct_ref(expression: str) -> tuple[str | None, str]:
    """Parse a direct column reference like ``T.COLUMN`` or plain ``COLUMN``."""
    expr = expression.strip()
    m = re.match(r"^([A-Za-z_]\w*)\.([A-Za-z_]\w*)$", expr)
    if m:
        return (m.group(1), m.group(2))
    m2 = re.match(r"^([A-Za-z_]\w*)$", expr)
    if m2:
        return (None, m2.group(1))
    return (None, expr)


# --------------- MERGE helpers ----------------

def _split_merge_clauses(
    raw_lines: list[str],
) -> tuple[list[str] | None, list[str] | None]:
    """Split MERGE body into WHEN MATCHED and WHEN NOT MATCHED line lists."""
    body = "\n".join(raw_lines)
    upper = body.upper()

    matched_start = None
    not_matched_start = None

    # Find positions of WHEN MATCHED and WHEN NOT MATCHED
    wm = re.search(r"\bWHEN\s+MATCHED\b", upper)
    wnm = re.search(r"\bWHEN\s+NOT\s+MATCHED\b", upper)

    if wm:
        matched_start = wm.start()
    if wnm:
        not_matched_start = wnm.start()

    matched_lines = None
    not_matched_lines = None

    if matched_start is not None and not_matched_start is not None:
        if matched_start < not_matched_start:
            matched_text = body[matched_start:not_matched_start]
            not_matched_text = body[not_matched_start:]
        else:
            not_matched_text = body[not_matched_start:matched_start]
            matched_text = body[matched_start:]
        matched_lines = matched_text.splitlines()
        not_matched_lines = not_matched_text.splitlines()
    elif matched_start is not None:
        matched_lines = body[matched_start:].splitlines()
    elif not_matched_start is not None:
        not_matched_lines = body[not_matched_start:].splitlines()

    return (matched_lines, not_matched_lines)


# --------------- SELECT INTO helpers ----------------

def _extract_into_variable(body: str) -> str | None:
    """Extract the variable name from ``SELECT ... INTO <var> FROM``."""
    m = re.search(r"\bINTO\s+(\S+)", body, re.IGNORECASE)
    if m:
        return m.group(1).rstrip(";").strip()
    return None


def _extract_select_expression(body: str) -> str:
    """Extract the expression between SELECT and INTO."""
    m = re.search(r"\bSELECT\s+(.+?)\s+INTO\b", body, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return body.strip()


# --------------- WHILE loop helpers ----------------

def _extract_while_definition(body: str, raw_lines: list[str]) -> dict:
    """Extract counter, termination, increment from a WHILE block."""
    definition: dict = {
        "counter": None,
        "start": None,
        "termination": None,
        "termination_value": None,
        "increment": None,
        "iterations": None,
    }

    # WHILE <var> <op> <value> LOOP
    m = re.search(
        r"\bWHILE\s+(\w+)\s*([<>=!]+)\s*(\w+)\s+LOOP",
        body,
        re.IGNORECASE,
    )
    if m:
        definition["counter"] = m.group(1)
        definition["termination"] = f"{m.group(1)} {m.group(2)} {m.group(3)}"
        definition["termination_value"] = m.group(3)

    # Look for counter initialisation: <var> := <number>
    if definition["counter"]:
        init_pat = re.compile(
            rf"\b{re.escape(definition['counter'])}\s*:=\s*(\d+)",
            re.IGNORECASE,
        )
        im = init_pat.search(body)
        if im:
            definition["start"] = im.group(1)

    # Look for increment: <var> := <var> + <number>
    if definition["counter"]:
        inc_pat = re.compile(
            rf"\b{re.escape(definition['counter'])}\s*:=\s*{re.escape(definition['counter'])}\s*\+\s*(\d+)",
            re.IGNORECASE,
        )
        inc_m = inc_pat.search(body)
        if inc_m:
            definition["increment"] = inc_m.group(1)

    return definition


# --------------- FOR loop helpers ----------------

def _extract_cursor_query(body: str) -> str | None:
    """Extract the cursor SQL from ``FOR rec IN (SELECT ...) LOOP``."""
    m = re.search(
        r"\bFOR\s+\w+\s+IN\s*\((.+?)\)\s*LOOP",
        body,
        re.IGNORECASE | re.DOTALL,
    )
    if m:
        return m.group(1).strip()
    # Named cursor reference: FOR rec IN cursor_name LOOP
    m2 = re.search(
        r"\bFOR\s+\w+\s+IN\s+(\w+)\s+LOOP",
        body,
        re.IGNORECASE,
    )
    if m2:
        return m2.group(1).strip()
    return None


# --------------- Utility helpers ----------------

def _is_literal(value: str) -> bool:
    """Return True if *value* looks like a string or numeric literal."""
    v = value.strip()
    if (v.startswith("'") and v.endswith("'")) or (v.startswith('"') and v.endswith('"')):
        return True
    try:
        float(v)
        return True
    except ValueError:
        return False


def _extract_paren_content(expression: str, opening_pattern: str) -> str | None:
    """Extract the content between the first matched opening pattern and
    its balanced closing parenthesis."""
    m = re.search(opening_pattern, expression, re.IGNORECASE)
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(expression) and depth > 0:
        if expression[i] == "(":
            depth += 1
        elif expression[i] == ")":
            depth -= 1
        i += 1
    if depth != 0:
        return expression[start:]
    return expression[start: i - 1]


def _split_top_level(text: str, delimiter: str) -> list[str]:
    """Split *text* on *delimiter* only when not inside parentheses or quotes."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = False
    quote_char = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quote:
            current.append(ch)
            if ch == quote_char:
                in_quote = False
        elif ch in ("'", '"'):
            in_quote = True
            quote_char = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif depth == 0 and text[i: i + len(delimiter)] == delimiter:
            parts.append("".join(current))
            current = []
            i += len(delimiter)
            continue
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _node_references_variable(node: dict, variable: str) -> bool:
    """Return True when *variable* appears anywhere in the node's
    column_maps, conditions, or calculations."""
    var_upper = variable.upper()

    # column_maps values
    for expr in (node.get("column_maps") or {}).values():
        if isinstance(expr, str) and var_upper in expr.upper():
            return True

    # conditions
    for cond in (node.get("conditions") or []):
        cond_text = cond if isinstance(cond, str) else cond.get("expression", "")
        if var_upper in cond_text.upper():
            return True

    # calculations
    for calc in (node.get("calculation") or []):
        if isinstance(calc, dict) and var_upper in calc.get("expression", "").upper():
            return True

    return False


def _extract_column_names_from_expr(expression: str) -> list[str]:
    """Return a list of plausible column names found in *expression*."""
    if not expression:
        return []
    # Match word.word (table.column) or standalone words, skip keywords
    keywords = {
        "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "ON",
        "INSERT", "INTO", "UPDATE", "SET", "DELETE", "MERGE", "WHEN",
        "MATCHED", "THEN", "ELSE", "END", "CASE", "NULL", "IS",
        "BETWEEN", "LIKE", "EXISTS", "AS", "JOIN", "LEFT", "RIGHT",
        "INNER", "OUTER", "FULL", "CROSS", "GROUP", "BY", "ORDER",
        "HAVING", "UNION", "ALL", "NVL", "COALESCE", "DECODE",
        "BEGIN", "LOOP", "FOR", "WHILE", "IF", "ELSIF", "COMMIT",
    }
    refs: list[str] = []
    for m in re.finditer(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\b", expression):
        token = m.group(1)
        # Take the column part (after dot) if qualified
        parts = token.split(".")
        col = parts[-1]
        if col.upper() not in keywords and not col.isdigit():
            refs.append(col)
    return refs


# --------------- Summary generators ----------------

def _summarise_insert(
    target: str, sources: list[str], conditions: list,
) -> str:
    source_text = f" from {', '.join(sources)}" if sources else ""
    cond_hint = ""
    if conditions:
        first = conditions[0] if isinstance(conditions[0], str) else conditions[0].get("expression", "")
        if first:
            cond_hint = f" where {_truncate(first, 60)}"
    return f"Inserts data into {target or 'target table'}{source_text}{cond_hint}"


def _summarise_update(
    target: str, column_maps: dict, conditions: list,
) -> str:
    cols = list(column_maps.keys())[:3]
    col_text = ", ".join(cols) if cols else "columns"
    extra = f" (+{len(column_maps) - 3} more)" if len(column_maps) > 3 else ""
    cond_hint = ""
    if conditions:
        first = conditions[0] if isinstance(conditions[0], str) else conditions[0].get("expression", "")
        if first:
            cond_hint = f" where {_truncate(first, 50)}"
    return f"Updates {col_text}{extra} in {target or 'target table'}{cond_hint}"


def _summarise_delete(target: str, conditions: list) -> str:
    cond_hint = ""
    if conditions:
        first = (
            conditions[0]
            if isinstance(conditions[0], str)
            else conditions[0].get("expression", "")
        )
        if first:
            cond_hint = f" where {_truncate(first, 50)}"
    return f"Deletes rows from {target or 'target table'}{cond_hint}"


def _summarise_merge(
    target: str,
    sources: list[str],
    matched_maps: dict,
    not_matched_maps: dict,
) -> str:
    parts = [f"Merges into {target or 'target table'}"]
    if sources:
        parts.append(f"from {', '.join(sources)}")
    if matched_maps:
        parts.append(f"updating {len(matched_maps)} columns when matched")
    if not_matched_maps:
        parts.append(f"inserting {len(not_matched_maps)} columns when not matched")
    return "; ".join(parts)


def _summarise_scalar(
    variable: str | None, sources: list[str], expression: str,
) -> str:
    target = variable or "a variable"
    source_text = f" from {', '.join(sources)}" if sources else ""
    expr_hint = f" using {_truncate(expression, 50)}" if expression else ""
    return f"Computes {target}{source_text}{expr_hint}"


def _summarise_while(loop_def: dict, inner_node: dict | None) -> str:
    counter = loop_def.get("counter", "counter")
    term = loop_def.get("termination", "condition")
    inner_type = inner_node.get("type", "operation") if inner_node else "operations"
    return f"WHILE loop on {counter} until {term}, executing {inner_type}"


def _summarise_for(cursor_query: str | None, inner_ops: list[dict]) -> str:
    cursor_text = f"cursor ({_truncate(cursor_query, 50)})" if cursor_query else "cursor"
    op_count = len(inner_ops)
    return f"FOR loop over {cursor_text} with {op_count} inner operation(s)"


def _truncate(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* characters, adding ellipsis if cut."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."
