"""
Fetches actual Oracle data for each graph node.

All queries are read-only SELECT statements, validated by SQLGuardian,
and parameterised with bind variables. The fetcher never interprets
values semantically -- it simply returns rows as dicts alongside the
query that produced them.
"""

from __future__ import annotations

from typing import Any

from src.phase2.query_templates import (
    determine_template,
    generate_query,
)
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="app")


class ValueFetcher:
    """Fetch actual values at each graph node via read-only SELECT."""

    def __init__(self, schema_tools, sql_guardian) -> None:
        self._schema_tools = schema_tools
        self._guardian = sql_guardian

    async def fetch_node_value(
        self,
        node: dict,
        filters: dict[str, Any],
        query_intent: str = "trace_final",
        target_column: str | None = None,
        fetch_limit: int = 100,
    ) -> dict:
        """Run a SELECT for the given node and return matching rows.

        Returns
        -------
        dict
            status: "found" | "empty" | "error"
            row_count: int
            rows: list[dict]
            query: str (the SQL that was executed)
            bind_params: dict
            error: str | None
        """
        correlation_id = get_correlation_id()
        template_name = determine_template(node, query_intent)

        try:
            sql, bind_params = generate_query(
                node=node,
                filters=filters,
                template_name=template_name,
                target_column=target_column,
                fetch_limit=fetch_limit,
            )
        except Exception as exc:
            logger.warning(
                "ValueFetcher: query generation failed for %s: %s | correlation_id=%s",
                node.get("id"), exc, correlation_id,
            )
            return {
                "status": "error",
                "row_count": 0,
                "rows": [],
                "query": "",
                "bind_params": {},
                "error": f"query generation failed: {exc}",
            }

        try:
            self._guardian.validate(sql)
            self._guardian.check_bind_variables(sql, bind_params)
        except Exception as exc:
            logger.warning(
                "ValueFetcher: guardian rejected SQL for %s: %s | correlation_id=%s",
                node.get("id"), exc, correlation_id,
            )
            return {
                "status": "error",
                "row_count": 0,
                "rows": [],
                "query": sql,
                "bind_params": bind_params,
                "error": f"guardian rejection: {exc}",
            }

        try:
            rows = await self._schema_tools.execute_raw(sql, bind_params)
        except Exception as exc:
            logger.warning(
                "ValueFetcher: Oracle execution failed for %s: %s | correlation_id=%s",
                node.get("id"), exc, correlation_id,
            )
            return {
                "status": "error",
                "row_count": 0,
                "rows": [],
                "query": sql,
                "bind_params": bind_params,
                "error": f"oracle error: {exc}",
            }

        column_names = self._extract_select_columns(sql)
        dict_rows = [self._row_to_dict(r, column_names) for r in rows]

        return {
            "status": "found" if dict_rows else "empty",
            "row_count": len(dict_rows),
            "rows": dict_rows,
            "query": sql,
            "bind_params": bind_params,
            "error": None,
        }

    async def fetch_value_chain(
        self,
        graph_path: list[dict],
        filters: dict[str, Any],
        target_column: str,
    ) -> list[dict]:
        """Run fetch_node_value() for every node in execution order.

        Returns a list of ``{"node": node_dict, "value_result": fetch_result}``
        preserving the order of *graph_path*.
        """
        chain: list[dict] = []
        for entry in graph_path:
            node = entry.get("node", entry) if isinstance(entry, dict) else entry
            intent = self._intent_for_node(node)
            result = await self.fetch_node_value(
                node=node,
                filters=filters,
                query_intent=intent,
                target_column=target_column,
            )
            chain.append({
                "node": node,
                "function": entry.get("function") if isinstance(entry, dict) else None,
                "value_result": result,
            })
        return chain

    def detect_upstream_missing(self, value_chain: list[dict]) -> dict | None:
        """Return the first node where upstream data is missing.

        A node is considered "upstream-missing" when its query returned
        zero rows while being the origin point of the chain (no prior
        node produced data either). Errors are also reported here.
        """
        for idx, entry in enumerate(value_chain):
            result = entry.get("value_result", {})
            node = entry.get("node", {})
            status = result.get("status")
            if status == "error":
                return {
                    "node": node,
                    "position": idx,
                    "reason": "error",
                    "message": result.get("error", "unknown error"),
                    "query": result.get("query", ""),
                }
            if status == "empty" and idx == 0:
                return {
                    "node": node,
                    "position": idx,
                    "reason": "empty_source",
                    "message": (
                        f"Source table {node.get('target_table') or (node.get('source_tables') or [None])[0]} "
                        "returned no rows for the requested filters."
                    ),
                    "query": result.get("query", ""),
                }
        return None

    def _intent_for_node(self, node: dict) -> str:
        """Choose the right query intent for a given node type."""
        node_type = (node.get("type") or "").upper()
        if node_type == "SCALAR_COMPUTE":
            return "trace_compute"
        return "trace_final"

    def _extract_select_columns(self, sql: str) -> list[str]:
        """Extract the column aliases from a SELECT clause.

        Only used to give row dicts sensible keys. Silently returns an
        empty list if parsing fails -- callers fall back to positional.
        """
        try:
            lowered = sql.lower()
            select_idx = lowered.index("select")
            from_idx = lowered.index("\nfrom ", select_idx)
            select_body = sql[select_idx + len("select"):from_idx].strip()
            parts = [p.strip() for p in select_body.split(",")]
            names: list[str] = []
            for p in parts:
                upper = p.upper()
                if " AS " in upper:
                    name = p.split()[-1]
                else:
                    name = p.split()[-1]
                names.append(name.strip().upper())
            return names
        except Exception:
            return []

    def _row_to_dict(self, row: Any, column_names: list[str]) -> dict:
        """Convert a DB row (tuple or dict) to a plain dict keyed by column name."""
        if isinstance(row, dict):
            return {str(k).upper(): v for k, v in row.items()}
        if not isinstance(row, (list, tuple)):
            return {"VALUE": row}
        result: dict = {}
        for i, val in enumerate(row):
            key = column_names[i] if i < len(column_names) and column_names[i] else f"COL_{i}"
            result[key] = val
        return result
