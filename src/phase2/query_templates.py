"""
Parameterised SQL templates for Phase 2 value fetching.

All templates use bind variables (`:param_name`). Table and column names
are taken from already-parsed graph nodes so they are not user-supplied.
Filter values always go into the bind_params dict, never into the SQL
string.
"""

from __future__ import annotations

import re
from typing import Any


# Bind parameter keys that correspond to common filter fields on Phase 2
# state. Adding a field here lets generate_query() automatically emit a
# WHERE clause for it.
_STANDARD_FILTERS: tuple[tuple[str, str, str], ...] = (
    # Key, column, placeholder -- FIC_MIS_DATE is a DATE column so it
    # uses TO_DATE. All other columns are VARCHAR2 and bind as-is.
    ("mis_date",       "FIC_MIS_DATE",    "TO_DATE(:mis_date, 'YYYY-MM-DD')"),
    ("account_number", "V_ACCOUNT_NUMBER", ":account_number"),
    ("gl_code",        "V_GL_CODE",       ":gl_code"),
    ("lob_code",       "V_LOB_CODE",      ":lob_code"),
    ("lv_code",        "V_LV_CODE",       ":lv_code"),
    ("branch_code",    "V_BRANCH_CODE",   ":branch_code"),
)


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_ident(name: str) -> str:
    """Guard against a graph node carrying a malformed identifier.

    Raises ValueError if name does not match a plain SQL identifier.
    Used before interpolating table/column names into SQL.
    """
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def determine_template(node: dict, query_intent: str) -> str:
    """Pick the template name matching the node type and query intent.

    query_intent is one of: "trace_final", "trace_source",
    "trace_compute", "verify_agg".
    """
    intent = (query_intent or "").strip().lower()
    node_type = (node.get("type") or "").upper()

    if intent == "trace_source":
        return "UPSTREAM_SOURCE"
    if intent == "trace_compute" or node_type == "SCALAR_COMPUTE":
        return "SCALAR_COMPUTE"
    if intent == "verify_agg":
        return "AGGREGATE_VERIFY"
    if node_type == "INSERT":
        return "INSERT_TARGET"
    if node_type in ("UPDATE", "MERGE"):
        return "UPDATE_TARGET"
    return "UPSTREAM_SOURCE"


def _cols_for_node(node: dict, target_column: str | None) -> list[str]:
    """Return the ordered list of columns to SELECT for a node."""
    cols: list[str] = []
    if target_column:
        cols.append(_safe_ident(target_column.upper()))

    assignments = (node.get("column_maps") or {}).get("assignments") or []
    for col, _expr in assignments:
        c = (col or "").strip().upper()
        if c and c not in cols:
            try:
                cols.append(_safe_ident(c))
            except ValueError:
                continue

    mapping = (node.get("column_maps") or {}).get("mapping") or {}
    for col in mapping.keys():
        c = (col or "").strip().upper()
        if c and c not in cols:
            try:
                cols.append(_safe_ident(c))
            except ValueError:
                continue

    if not cols:
        cols = ["*"]
    return cols


def _resolve_table(node: dict, intent: str) -> str:
    """Pick the physical table for a given intent."""
    if intent == "trace_source":
        sources = node.get("source_tables") or []
        if sources:
            return _safe_ident(sources[0].upper())
        raise ValueError(f"Node {node.get('id')} has no source_tables")
    target = node.get("target_table")
    if target:
        return _safe_ident(target.upper())
    sources = node.get("source_tables") or []
    if sources:
        return _safe_ident(sources[0].upper())
    raise ValueError(f"Node {node.get('id')} has no usable table")


def generate_query(
    node: dict,
    filters: dict[str, Any],
    template_name: str | None = None,
    target_column: str | None = None,
    fetch_limit: int = 100,
) -> tuple[str, dict[str, Any]]:
    """Build a complete SELECT query and its bind-parameter dict.

    All filter values are placed in bind_params; only column/table
    identifiers (already validated) appear in the SQL string.

    Returns
    -------
    (sql, bind_params)
        The SQL string (with `:param` placeholders) and a dict of
        parameter values ready to pass to the Oracle driver.
    """
    intent = _intent_from_template(template_name) if template_name else "trace_final"
    if not template_name:
        template_name = determine_template(node, intent)

    if template_name == "SCALAR_COMPUTE":
        return _generate_scalar_compute_query(node, filters, fetch_limit)
    if template_name == "AGGREGATE_VERIFY":
        return _generate_aggregate_query(node, filters, fetch_limit)

    if template_name == "UPSTREAM_SOURCE":
        table = _resolve_table(node, "trace_source")
    else:
        table = _resolve_table(node, "trace_final")

    cols = _cols_for_node(node, target_column)
    select_list = ", ".join(cols)

    where_clauses: list[str] = []
    bind_params: dict[str, Any] = {}

    for key, column, placeholder in _STANDARD_FILTERS:
        val = filters.get(key)
        if val is None or val == "":
            continue
        where_clauses.append(f"{column} = {placeholder}")
        bind_params[key] = val

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    sql = (
        f"SELECT {select_list}, FIC_MIS_DATE\n"
        f"FROM {table}\n"
        f"WHERE {where_sql}\n"
        f"FETCH FIRST {int(fetch_limit)} ROWS ONLY"
    )
    return sql, bind_params


def _generate_scalar_compute_query(
    node: dict,
    filters: dict[str, Any],
    fetch_limit: int,
) -> tuple[str, dict[str, Any]]:
    """Build a verification query for a SCALAR_COMPUTE node.

    The node's calculation[0].expression is used as the SELECT expression
    (wrapped in a single-column projection). Conditions from the node
    are translated into WHERE clauses only when they reference known
    filter columns -- non-matching conditions are left out to avoid
    unresolved variable references.
    """
    sources = node.get("source_tables") or []
    if not sources:
        raise ValueError(f"SCALAR_COMPUTE node {node.get('id')} has no source_tables")
    table = _safe_ident(sources[0].upper())

    calcs = node.get("calculation") or []
    if not calcs or not isinstance(calcs[0], dict):
        raise ValueError(f"SCALAR_COMPUTE node {node.get('id')} has no calculation expression")
    expr = (calcs[0].get("expression") or "").strip()
    if not expr:
        raise ValueError(f"SCALAR_COMPUTE node {node.get('id')} has empty expression")
    output_var = (node.get("output_variable") or calcs[0].get("column") or "VALUE").upper()
    try:
        alias = _safe_ident(output_var)
    except ValueError:
        alias = "VALUE"

    where_clauses: list[str] = []
    bind_params: dict[str, Any] = {}
    for key, column, placeholder in _STANDARD_FILTERS:
        val = filters.get(key)
        if val is None or val == "":
            continue
        where_clauses.append(f"{column} = {placeholder}")
        bind_params[key] = val

    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    sql = (
        f"SELECT {expr} AS {alias}\n"
        f"FROM {table}\n"
        f"WHERE {where_sql}\n"
        f"FETCH FIRST {int(fetch_limit)} ROWS ONLY"
    )
    return sql, bind_params


def _generate_aggregate_query(
    node: dict,
    filters: dict[str, Any],
    fetch_limit: int,
) -> tuple[str, dict[str, Any]]:
    """Build a verification query that aggregates a node's target column.

    Uses SUM if the calculation type is ARITHMETIC with SUM, else COUNT.
    Falls back to SELECT * with a limit when no calculation is available.
    """
    table = _resolve_table(node, "trace_final")
    agg_fn = "SUM"
    calcs = node.get("calculation") or []
    if calcs and isinstance(calcs[0], dict):
        expr = (calcs[0].get("expression") or "").upper()
        if expr.startswith("MAX"):
            agg_fn = "MAX"
        elif expr.startswith("MIN"):
            agg_fn = "MIN"
        elif expr.startswith("AVG"):
            agg_fn = "AVG"

    column = None
    if calcs and isinstance(calcs[0], dict):
        column = (calcs[0].get("column") or "").upper() or None
    if not column:
        assignments = (node.get("column_maps") or {}).get("assignments") or []
        if assignments:
            column = (assignments[0][0] or "").upper()
    if not column:
        column = "N_EOP_BAL"
    column = _safe_ident(column)

    where_clauses: list[str] = []
    bind_params: dict[str, Any] = {}
    for key, db_col, placeholder in _STANDARD_FILTERS:
        val = filters.get(key)
        if val is None or val == "":
            continue
        where_clauses.append(f"{db_col} = {placeholder}")
        bind_params[key] = val
    where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

    sql = (
        f"SELECT {agg_fn}({column}) AS AGG_VALUE, COUNT(*) AS ROW_COUNT\n"
        f"FROM {table}\n"
        f"WHERE {where_sql}\n"
        f"FETCH FIRST {int(fetch_limit)} ROWS ONLY"
    )
    return sql, bind_params


def _intent_from_template(template_name: str) -> str:
    mapping = {
        "INSERT_TARGET": "trace_final",
        "UPDATE_TARGET": "trace_final",
        "UPSTREAM_SOURCE": "trace_source",
        "SCALAR_COMPUTE": "trace_compute",
        "AGGREGATE_VERIFY": "verify_agg",
    }
    return mapping.get(template_name, "trace_final")
