"""
Fetch the target row before any tracing.

If the row does not exist for the user-supplied filters, the entire
Phase 2 pipeline stops. We never let the LLM invent an explanation
for a row that isn't there.
"""

from __future__ import annotations

import re
from typing import Any

from src.logger import get_logger

logger = get_logger(__name__, concern="app")


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Mapping of filter keys to SQL columns. Values go into bind_params
# keyed by the filter name; the column name comes from this table.
_FILTER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("mis_date",       "FIC_MIS_DATE"),
    ("account_number", "V_ACCOUNT_NUMBER"),
    ("gl_code",        "V_GL_CODE"),
    ("lv_code",        "V_LV_CODE"),
    ("lob_code",       "V_LOB_CODE"),
    ("branch_code",    "V_BRANCH_CODE"),
)


def _safe_ident(name: str) -> str:
    """Guard against malformed identifiers before interpolation."""
    if not name or not _IDENT_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


class RowInspector:
    """Fetch the actual row being traced via a read-only SELECT."""

    def __init__(self, schema_tools, sql_guardian) -> None:
        self._schema_tools = schema_tools
        self._guardian = sql_guardian

    async def fetch_target_row(
        self,
        target_table: str,
        filters: dict[str, Any],
        fetch_limit: int = 10,
    ) -> dict:
        """Fetch the exact row(s) the user is asking about.

        Returns a dict:

        .. code-block:: python

            {
                "status":      "found" | "not_found" | "oracle_error",
                "row_count":   int,
                "row":         dict | None,   # first matching row
                "rows":        list[dict],    # all matching rows
                "columns":     list[str],     # column names in order
                "query":       str,
                "bind_params": dict,
                "error":       str | None,
            }
        """
        # Fetch column names first so we can return rows as dicts.
        try:
            columns = await self._get_column_names(target_table)
        except Exception as exc:
            logger.warning("RowInspector: could not read columns for %s: %s", target_table, exc)
            columns = []

        sql, bind_params = self._build_select(target_table, filters, fetch_limit)

        try:
            self._guardian.validate(sql)
            self._guardian.check_bind_variables(sql, bind_params)
        except Exception as exc:
            logger.warning("RowInspector: guardian rejected SQL: %s", exc)
            return {
                "status": "oracle_error",
                "row_count": 0,
                "row": None,
                "rows": [],
                "columns": columns,
                "query": sql,
                "bind_params": bind_params,
                "error": f"guardian rejection: {exc}",
            }

        try:
            raw_rows = await self._schema_tools.execute_raw(sql, bind_params)
        except Exception as exc:
            logger.warning("RowInspector: Oracle execution failed: %s", exc)
            return {
                "status": "oracle_error",
                "row_count": 0,
                "row": None,
                "rows": [],
                "columns": columns,
                "query": sql,
                "bind_params": bind_params,
                "error": f"oracle error: {exc}",
            }

        dict_rows = [self._row_to_dict(r, columns) for r in raw_rows]

        if not dict_rows:
            return {
                "status": "not_found",
                "row_count": 0,
                "row": None,
                "rows": [],
                "columns": columns,
                "query": sql,
                "bind_params": bind_params,
                "error": None,
            }

        return {
            "status": "found",
            "row_count": len(dict_rows),
            "row": dict_rows[0],
            "rows": dict_rows,
            "columns": columns,
            "query": sql,
            "bind_params": bind_params,
            "error": None,
        }

    async def _get_column_names(self, target_table: str) -> list[str]:
        """Return the ordered column list for *target_table*.

        Uses ``user_tab_columns`` via the same read-only schema_tools
        execute path. Column names come back uppercase.
        """
        table = _safe_ident(target_table.upper())
        sql = (
            "SELECT column_name FROM user_tab_columns "
            "WHERE table_name = :t ORDER BY column_id"
        )
        params = {"t": table}
        self._guardian.validate(sql)
        rows = await self._schema_tools.execute_raw(sql, params)
        return [r[0] for r in rows if r and r[0]]

    async def row_exists(self, target_table: str, filters: dict[str, Any]) -> bool:
        """Lightweight existence check: SELECT 1 ... FETCH FIRST 1 ROW."""
        table = _safe_ident(target_table.upper())
        where_clauses, bind_params = self._build_where_clauses(filters)
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        sql = (
            f"SELECT 1 FROM {table}\n"
            f"WHERE {where_sql}\n"
            f"FETCH FIRST 1 ROW ONLY"
        )
        self._guardian.validate(sql)
        self._guardian.check_bind_variables(sql, bind_params)
        try:
            rows = await self._schema_tools.execute_raw(sql, bind_params)
            return bool(rows)
        except Exception as exc:
            logger.warning("RowInspector.row_exists failed: %s", exc)
            return False

    def _build_select(
        self,
        target_table: str,
        filters: dict[str, Any],
        fetch_limit: int,
    ) -> tuple[str, dict[str, Any]]:
        """Build a SELECT * query for the row, with bind variables."""
        table = _safe_ident(target_table.upper())
        where_clauses, bind_params = self._build_where_clauses(filters)
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        sql = (
            f"SELECT * FROM {table}\n"
            f"WHERE {where_sql}\n"
            f"FETCH FIRST {int(fetch_limit)} ROWS ONLY"
        )
        return sql, bind_params

    def _build_where_clauses(
        self,
        filters: dict[str, Any],
    ) -> tuple[list[str], dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        for key, column in _FILTER_COLUMNS:
            val = filters.get(key)
            if val is None or val == "":
                continue
            # FIC_MIS_DATE is an Oracle DATE column; bind as a string
            # wrapped in TO_DATE so NLS settings don't affect matching.
            if key == "mis_date":
                clauses.append(f"{column} = TO_DATE(:{key}, 'YYYY-MM-DD')")
            else:
                clauses.append(f"{column} = :{key}")
            params[key] = val
        return clauses, params

    def _row_to_dict(self, row: Any, columns: list[str]) -> dict:
        """Convert a DB row into a dict keyed by column name.

        Oracle async driver returns tuples. We zip against the fetched
        column list. Any trailing/extra values are keyed as ``COL_<n>``
        so nothing silently gets dropped.
        """
        if isinstance(row, dict):
            return {str(k).upper(): v for k, v in row.items()}
        if not isinstance(row, (list, tuple)):
            return {"VALUE": row}
        result: dict = {}
        for i, value in enumerate(row):
            key = columns[i].upper() if i < len(columns) and columns[i] else f"COL_{i}"
            result[key] = value
        return result
