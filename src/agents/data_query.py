"""
RTIE DataQueryAgent.

Sibling of ValueTracerAgent for aggregate and filter queries that cannot
be answered by the graph tracer. Generates a read-only SELECT via the
LLM, validates it through SQLGuardian, applies three safeguards (row
count pre-check, aggregation preference, FETCH injection) and returns a
structured response with the executed SQL, the rows, and a deterministic
one-sentence summary.

Safeguards:
  1. Row count pre-check against a hard / warn / auto threshold.
  2. LLM prompt steers toward aggregation when the question allows.
  3. FETCH FIRST is injected into every row-listing query that lacks one.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from langchain_core.messages import SystemMessage, HumanMessage

from src.llm_factory import create_llm
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id
from src.parsing.store import get_column_index
from src.tools.sql_guardian import GuardianRejectionError

logger = get_logger(__name__, concern="app")


DEFAULT_HARD_LIMIT = 10_000
DEFAULT_WARN_LIMIT = 100
DEFAULT_DISPLAY_LIMIT = 100


SYSTEM_PROMPT = """You generate a single Oracle read-only SELECT statement.

HARD CONSTRAINTS — violating any of these produces an invalid response:
- SELECT only. No INSERT, UPDATE, DELETE, MERGE, DDL.
- Use Oracle bind variables `:param_name` for every value that varies
  (dates, codes, ids, numbers). Never inline user values.
- Reference ONLY tables and columns listed in the provided schema.
  If the question needs a table that is not listed, respond with the
  special JSON shape `{"unsupported": true, "reason": "..."}`.
- Single statement. No semicolons, no PL/SQL blocks.
- Date columns (any column ending in _DATE or named *FIC_MIS_DATE*) must
  be bound via `TO_DATE(:param_name, 'YYYY-MM-DD')`, not as bare strings.
  Pass the date as a 'YYYY-MM-DD' string in params. Apply this rule to
  BOTH the main SQL and count_sql.

AGGREGATION PREFERENCE — if the question can be answered by an aggregate,
generate an aggregate, not a row list:
- "how many ..."  -> COUNT(*)
- "total ..."     -> SUM(column)
- "average ..."   -> AVG(column)
- "breakdown by X" -> GROUP BY X with COUNT / SUM
Only return a row list when the user explicitly asks for rows
("which accounts", "list all", "show me all").

TIME-SERIES / DATE-RANGE QUERIES — when the filters contain BOTH
`start_date` and `end_date`, the user wants to compare values at two
specific dates. Generate a TIME_SERIES query:
- Select FIC_MIS_DATE plus the requested column(s) (and any filter
  columns such as V_ACCOUNT_NUMBER).
- Use `FIC_MIS_DATE IN (TO_DATE(:start_date, 'YYYY-MM-DD'),
                        TO_DATE(:end_date, 'YYYY-MM-DD'))`.
- ORDER BY FIC_MIS_DATE.
- Include all other filters (account_number, lv_code, ...) in the WHERE.
- Set count_sql to null — time-series queries never need a count check,
  they return at most 2 rows per account.
- Do NOT use BETWEEN over a range; the user wants the two endpoint
  values, not every row in the interval.

Response format: a single JSON object, no markdown, no prose:
{
  "query_kind": "AGGREGATE" | "ROW_LIST" | "TIME_SERIES",
  "sql": "SELECT ...",
  "params": { "param_name": value, ... },
  "select_columns": ["COL1", "COL2", ...],
  "count_sql": "SELECT COUNT(*) FROM ... (same WHERE as the main query, or null if AGGREGATE/TIME_SERIES)"
}

For AGGREGATE and TIME_SERIES queries, count_sql MUST be null.
For ROW_LIST queries, count_sql MUST contain a SELECT COUNT(*) against the
same tables and the same WHERE clause as the main query, using the same
bind variables.

Never include FETCH FIRST / ROWNUM / OFFSET in the main SQL — the runtime
injects row limits after your response (TIME_SERIES and AGGREGATE queries
are not row-limit-injected).

Example of a TIME_SERIES response shape:
{
  "query_kind": "TIME_SERIES",
  "sql": "SELECT FIC_MIS_DATE, V_ACCOUNT_NUMBER, N_EOP_BAL FROM STG_PRODUCT_PROCESSOR WHERE V_ACCOUNT_NUMBER = :account_number AND FIC_MIS_DATE IN (TO_DATE(:start_date, 'YYYY-MM-DD'), TO_DATE(:end_date, 'YYYY-MM-DD')) ORDER BY FIC_MIS_DATE",
  "params": { "account_number": "TF1528012748-T24-COLLBLG", "start_date": "2025-09-30", "end_date": "2025-12-31" },
  "select_columns": ["FIC_MIS_DATE", "V_ACCOUNT_NUMBER", "N_EOP_BAL"],
  "count_sql": null
}
"""


class DataQueryAgent:
    """Answers aggregate / filter questions by generating + executing SQL."""

    def __init__(
        self,
        schema_tools,
        redis_client,
        sql_guardian,
        temperature: float = 0,
        max_tokens: int = 2000,
        hard_row_limit: int = DEFAULT_HARD_LIMIT,
        warn_row_limit: int = DEFAULT_WARN_LIMIT,
        display_row_limit: int = DEFAULT_DISPLAY_LIMIT,
    ) -> None:
        self._schema_tools = schema_tools
        self._redis = redis_client
        self._guardian = sql_guardian
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._hard_limit = hard_row_limit
        self._warn_limit = warn_row_limit
        self._display_limit = display_row_limit

    # -----------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------

    async def answer(
        self,
        user_query: str,
        schema: str,
        filters: Optional[dict] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        force: bool = False,
    ) -> dict:
        """Generate + execute SQL for a data query. Returns a dict describing
        the outcome.

        Parameters
        ----------
        user_query:
            The raw natural-language question.
        schema:
            Oracle schema name (used to scope the graph catalog).
        filters:
            Orchestrator-extracted filters (mis_date, account_number, etc.).
            Merged into the LLM prompt to give explicit binding hints.
        force:
            When True, bypass the warn threshold confirmation gate and
            proceed with execution anyway. Used for user-confirmed retries.
        """
        correlation_id = get_correlation_id()
        filters = dict(filters or {})

        try:
            catalog_text = self._build_schema_catalog(schema)
        except Exception as exc:
            logger.warning("DataQuery catalog build failed: %s", exc)
            catalog_text = "(schema catalog unavailable — rely on commonly-known OFSAA STG tables)"

        try:
            plan = await self._generate_sql(
                user_query=user_query,
                filters=filters,
                catalog_text=catalog_text,
                provider=provider,
                model=model,
            )
        except Exception as exc:
            logger.error("DataQuery SQL generation failed: %s", exc)
            return self._error_result(
                status="generation_error",
                user_query=user_query,
                explanation=(
                    "I couldn't turn your question into a SQL query. "
                    f"Reason: {exc}. Try rephrasing with explicit column / "
                    "filter names."
                ),
            )

        if plan.get("unsupported"):
            return {
                "status": "unsupported",
                "query_kind": None,
                "sql": None,
                "count_sql": None,
                "params": {},
                "rows": [],
                "columns": [],
                "row_count": 0,
                "summary": plan.get("reason") or "Question references data outside scope.",
                "explanation": (
                    "This question cannot be answered by the current system: "
                    f"{plan.get('reason') or 'capability not available.'} "
                    "No partial answer returned."
                ),
                "sanity_warnings": [],
                "verification_sql": None,
                "correlation_id": correlation_id,
            }

        sql = plan["sql"]
        params = plan.get("params") or {}
        query_kind = plan.get("query_kind") or "ROW_LIST"
        count_sql = plan.get("count_sql")
        select_columns = plan.get("select_columns") or []

        # Guardian validation (hard stop on DML/DDL or interpolation)
        try:
            self._guardian.validate(sql)
            if params:
                self._guardian.check_bind_variables(sql, params)
        except GuardianRejectionError as exc:
            logger.error("DataQuery guardian rejected generated SQL: %s", exc)
            return self._error_result(
                status="validation_error",
                user_query=user_query,
                sql=sql,
                params=params,
                explanation=(
                    "The generated SQL was rejected by the SQL Guardian. "
                    f"Reason: {exc.message}. No execution performed."
                ),
            )

        # Safeguard 1: row count pre-check (skipped for aggregate queries).
        warnings: list[str] = []
        if query_kind == "ROW_LIST" and count_sql:
            try:
                self._guardian.validate(count_sql)
                if params:
                    self._guardian.check_bind_variables(count_sql, params)
                count_rows = await self._schema_tools.execute_raw(count_sql, params)
                total_rows = int(count_rows[0][0]) if count_rows else 0
            except Exception as exc:
                logger.warning("DataQuery count pre-check failed: %s", exc)
                total_rows = None
                warnings.append(f"count pre-check failed: {exc}")

            if total_rows is not None:
                if total_rows > self._hard_limit:
                    return {
                        "status": "too_many_rows",
                        "query_kind": query_kind,
                        "sql": sql,
                        "count_sql": count_sql,
                        "params": params,
                        "rows": [],
                        "columns": select_columns,
                        "row_count": total_rows,
                        "summary": (
                            f"Query would return {total_rows:,} rows, exceeding "
                            f"the hard limit of {self._hard_limit:,}."
                        ),
                        "explanation": (
                            f"Your question would return **{total_rows:,} rows**, "
                            f"which exceeds the hard limit of {self._hard_limit:,}. "
                            "Narrow the query with a more specific filter "
                            "(e.g. a single MIS date, a specific LOB, or an "
                            "aggregation) and retry."
                        ),
                        "sanity_warnings": warnings,
                        "verification_sql": count_sql,
                        "correlation_id": correlation_id,
                    }
                if total_rows > self._warn_limit and not force:
                    return {
                        "status": "confirmation_required",
                        "query_kind": query_kind,
                        "sql": sql,
                        "count_sql": count_sql,
                        "params": params,
                        "rows": [],
                        "columns": select_columns,
                        "row_count": total_rows,
                        "summary": (
                            f"Query would return {total_rows:,} rows. "
                            "Confirm to proceed or reformulate as an aggregate."
                        ),
                        "explanation": (
                            f"Your question would return **{total_rows:,} rows** "
                            f"(above the {self._warn_limit:,}-row warn threshold). "
                            "Re-send with `force=true` to list them (first "
                            f"{self._display_limit} will be shown), or rephrase "
                            "as an aggregate (e.g. COUNT, SUM, GROUP BY)."
                        ),
                        "sanity_warnings": warnings,
                        "verification_sql": count_sql,
                        "correlation_id": correlation_id,
                    }

        # Safeguard 3: inject display limit for row-listing queries.
        exec_sql = sql
        if query_kind == "ROW_LIST":
            exec_sql = self._guardian.inject_fetch_limit(
                sql, limit=self._display_limit
            )

        # Execute
        try:
            rows = await self._schema_tools.execute_raw(exec_sql, params)
        except Exception as exc:
            logger.error("DataQuery execution failed: %s", exc)
            return self._error_result(
                status="oracle_error",
                user_query=user_query,
                sql=exec_sql,
                params=params,
                explanation=(
                    "Oracle rejected the generated SQL. "
                    f"Reason: {exc}. No rows returned."
                ),
                warnings=warnings,
            )

        columns = select_columns or _column_names_from_sql(exec_sql)
        materialised = _materialise_rows(rows)
        row_count = len(materialised)
        summary = _summarise(
            user_query=user_query,
            query_kind=query_kind,
            columns=columns,
            rows=materialised,
            row_count=row_count,
        )

        return {
            "status": "answered",
            "query_kind": query_kind,
            "sql": exec_sql,
            "count_sql": count_sql,
            "params": params,
            "rows": materialised[: self._display_limit],
            "columns": columns,
            "row_count": row_count,
            "summary": summary,
            "explanation": _build_explanation(
                summary=summary,
                sql=exec_sql,
                params=params,
                rows=materialised[: self._display_limit],
                columns=columns,
                truncated=row_count > self._display_limit,
                display_limit=self._display_limit,
            ),
            "sanity_warnings": warnings,
            "verification_sql": count_sql or exec_sql,
            "correlation_id": correlation_id,
        }

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------

    async def _generate_sql(
        self,
        user_query: str,
        filters: dict,
        catalog_text: str,
        provider: Optional[str],
        model: Optional[str],
    ) -> dict:
        llm = create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            json_mode=(provider or "openai") != "anthropic",
        )

        non_null_filters = {k: v for k, v in filters.items() if v not in (None, "")}
        filters_hint = (
            "Orchestrator-extracted filters (use these as bind variables "
            "where relevant): " + json.dumps(non_null_filters, default=str)
            if non_null_filters
            else "No filters pre-extracted; parse the question directly."
        )

        prompt = (
            f"Question: {user_query}\n\n"
            f"{filters_hint}\n\n"
            "Available schema (tables → columns):\n"
            f"{catalog_text}\n"
        )

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]

        response = await llm.ainvoke(messages)
        raw = (response.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        parsed = json.loads(raw)
        return parsed

    def _build_schema_catalog(self, schema: str) -> str:
        """Build a compact `table → [columns]` description from the graph.

        Two data sources are combined:
        1. Per-function graphs — INSERT/UPDATE column_maps give precise
           `table → columns` mapping for the columns the PL/SQL touches.
        2. The raw source of each function — INSERT `(COL, COL, ...)`
           column lists are parsed and attributed to the target table.
        3. A flat "Known columns" section sourced from the graph column
           index so the LLM can reference columns that exist but weren't
           attributed to a single table.
        """
        tables_to_columns: dict[str, set[str]] = {}
        all_columns: set[str] = set()

        if self._redis is not None:
            try:
                keys = self._redis.keys(f"graph:{schema}:*") or []
            except Exception as exc:
                logger.warning("Redis keys() failed during catalog build: %s", exc)
                keys = []

            from src.parsing.store import (
                get_function_graph,
                get_raw_source,
                get_column_index,
            )

            reserved = {"full", "index", "aliases", "source", "meta"}
            for raw_key in keys:
                key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
                parts = key.split(":")
                if len(parts) < 3 or parts[1] in reserved:
                    continue
                function_name = parts[2]
                graph = get_function_graph(self._redis, schema, function_name)
                if not graph:
                    continue
                for node in graph.get("nodes", []) or []:
                    _collect_table_columns(node, tables_to_columns)
                    for arm in node.get("union_arms", []) or []:
                        _collect_table_columns(
                            arm,
                            tables_to_columns,
                            fallback_target=node.get("target_table"),
                        )

                raw_lines = get_raw_source(self._redis, schema, function_name)
                if raw_lines:
                    for table, cols in _parse_insert_column_lists(raw_lines).items():
                        tables_to_columns.setdefault(table, set()).update(cols)

            try:
                idx = get_column_index(self._redis, schema) or {}
                for col in idx.keys():
                    if isinstance(col, str) and _looks_like_column_name(col):
                        all_columns.add(col)
            except Exception as exc:
                logger.warning("column index load failed: %s", exc)

        if not tables_to_columns and not all_columns:
            return "(no tables discovered — schema catalog empty)"

        lines: list[str] = ["Tables:"]
        for table in sorted(tables_to_columns.keys()):
            cols = sorted(tables_to_columns[table])
            if not cols:
                lines.append(f"- {table}: (columns unknown; check Known columns below)")
                continue
            lines.append(f"- {table}: {', '.join(cols)}")

        if all_columns:
            lines.append("")
            lines.append(
                "Known columns in schema (exist somewhere in the graph; "
                "pick the one that fits the question — common OFSAA "
                "conventions: N_* = numeric, V_* = varchar, F_* = flag, "
                "D_* = date):"
            )
            lines.append(", ".join(sorted(all_columns)))

        return "\n".join(lines)

    def _error_result(
        self,
        status: str,
        user_query: str,
        explanation: str,
        sql: Optional[str] = None,
        params: Optional[dict] = None,
        warnings: Optional[list[str]] = None,
    ) -> dict:
        return {
            "status": status,
            "query_kind": None,
            "sql": sql,
            "count_sql": None,
            "params": params or {},
            "rows": [],
            "columns": [],
            "row_count": 0,
            "summary": f"Could not answer: {user_query}",
            "explanation": explanation,
            "sanity_warnings": warnings or [],
            "verification_sql": None,
            "correlation_id": get_correlation_id(),
        }


# ---------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------

_IDENT_RE = re.compile(r"\b[A-Z_][A-Z0-9_]*\b")
_SKIP_TABLE_TOKENS = frozenset({
    "DUAL", "OFSMDM", "SYSDATE", "NULL", "SYSTIMESTAMP", "CURRENT_DATE",
    "CURRENT_TIMESTAMP", "USER", "ADD_MONTHS", "TRUNC", "WITH", "YEAR",
    "MONTH", "FIC_MIS_DATE",
})

# Prefixes that identify real OFSAA tables (and some generic ones)
_TABLE_PREFIX_RE = re.compile(
    r"^(STG|FCT|FSI|DIM|SETUP|OFSDWH|INTERNAL|ABL|MAP_)_|"
    r"^(MAPPING_|TLX_|FN_)",
    re.IGNORECASE,
)

# Column naming conventions — N_ numeric, V_ varchar, F_ flag, D_ date,
# plus FIC_* (OFSAA system columns) and LD_ / SETUP_
_COLUMN_PREFIX_RE = re.compile(r"^(N|V|F|D|FIC|LD|SETUP|B)_", re.IGNORECASE)


def _collect_table_columns(
    node: dict,
    tables_to_columns: dict[str, set[str]],
    fallback_target: Optional[str] = None,
) -> None:
    target = (node.get("target_table") or fallback_target or "").strip().upper()
    if target and target not in _SKIP_TABLE_TOKENS and _looks_like_table_name(target):
        tables_to_columns.setdefault(target, set())

    col_maps = node.get("column_maps") or {}
    if isinstance(col_maps, dict):
        cols: list[str] = []
        if "columns" in col_maps and isinstance(col_maps["columns"], list):
            cols.extend(c for c in col_maps["columns"] if isinstance(c, str))
        mapping = col_maps.get("mapping") or {}
        if isinstance(mapping, dict):
            cols.extend(k for k in mapping.keys() if isinstance(k, str))
        for col, _expr in col_maps.get("assignments") or []:
            if isinstance(col, str):
                cols.append(col)
        if target in tables_to_columns:
            for col in cols:
                name = col.strip().upper()
                if name and _IDENT_RE.fullmatch(name):
                    tables_to_columns[target].add(name)

    for src in node.get("source_tables") or []:
        if isinstance(src, str):
            nm = src.strip().upper()
            if nm and nm not in _SKIP_TABLE_TOKENS and _looks_like_table_name(nm):
                tables_to_columns.setdefault(nm, set())


_INSERT_HEAD_RE = re.compile(
    r"INSERT\s+INTO\s+(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*)(?:\s+[A-Za-z_]\w*)?\s*\(",
    re.IGNORECASE,
)


def _parse_insert_column_lists(raw_lines: list[str]) -> dict[str, set[str]]:
    """Scan raw PL/SQL source for `INSERT INTO TABLE (COL, COL, ...)` headers
    and return `{table: {columns}}`. Handles multi-line column lists."""
    result: dict[str, set[str]] = {}
    text = "\n".join(raw_lines) if isinstance(raw_lines, list) else str(raw_lines)
    for m in _INSERT_HEAD_RE.finditer(text):
        table = m.group(1).upper()
        if table in _SKIP_TABLE_TOKENS or not _looks_like_table_name(table):
            continue
        # Walk from the `(` after the table name, balancing parens, to collect
        # the column list content.
        start = m.end()  # points just after the opening '('
        depth = 1
        i = start
        while i < len(text) and depth > 0:
            ch = text[i]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth != 0:
            continue
        inner = text[start:i]
        cols = set()
        for part in inner.split(","):
            token = part.strip()
            # Allow "alias.col" form (e.g. B.V_ACCOUNT_NUMBER)
            if "." in token:
                token = token.split(".", 1)[1].strip()
            token = token.strip().rstrip(";").strip()
            if not token:
                continue
            name = token.upper()
            if _IDENT_RE.fullmatch(name):
                cols.add(name)
        if cols:
            result.setdefault(table, set()).update(cols)
    return result


def _looks_like_table_name(name: str) -> bool:
    if not name or not _IDENT_RE.fullmatch(name):
        return False
    if name in _SKIP_TABLE_TOKENS:
        return False
    # Column-style prefixes aren't tables (N_, V_, F_, D_, FIC_).
    if _COLUMN_PREFIX_RE.match(name):
        return False
    return bool(_TABLE_PREFIX_RE.match(name))


def _looks_like_column_name(name: str) -> bool:
    if not name or not _IDENT_RE.fullmatch(name):
        return False
    # Reject anything that ends with a digit — typically a SELECT alias
    # (e.g. V_LV_CODE1, V_PROD_CODE1).
    if name[-1].isdigit():
        return False
    return bool(_COLUMN_PREFIX_RE.match(name))


def _materialise_rows(rows: list) -> list[list]:
    """Normalize cursor tuples into JSON-serialisable lists of primitives."""
    normalised: list[list] = []
    for r in rows or []:
        if isinstance(r, (list, tuple)):
            normalised.append([_jsonable(v) for v in r])
        else:
            normalised.append([_jsonable(r)])
    return normalised


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Oracle dates / decimals / LOBs: fall back to str.
    try:
        return str(value)
    except Exception:
        return repr(value)


def _column_names_from_sql(sql: str) -> list[str]:
    """Best-effort alias extraction from the SELECT list — only used when the
    LLM didn't provide select_columns."""
    m = re.search(r"\bSELECT\b(.+?)\bFROM\b", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    body = m.group(1)
    parts = [p.strip() for p in body.split(",")]
    names: list[str] = []
    for p in parts:
        alias = re.search(r"\bAS\s+([A-Za-z_]\w*)\s*$", p, re.IGNORECASE)
        if alias:
            names.append(alias.group(1).upper())
            continue
        trailing = re.search(r"([A-Za-z_]\w*)\s*$", p)
        if trailing:
            names.append(trailing.group(1).upper())
        else:
            names.append(p[:40])
    return names


def _summarise(
    user_query: str,
    query_kind: str,
    columns: list[str],
    rows: list[list],
    row_count: int,
) -> str:
    if row_count == 0:
        return "No rows matched the query."
    if query_kind == "AGGREGATE" and rows and len(rows[0]) == 1:
        value = rows[0][0]
        col = columns[0] if columns else "result"
        return f"{col} = {value}."
    if query_kind == "AGGREGATE":
        return f"Aggregate returned {row_count} group(s)."
    if query_kind == "TIME_SERIES":
        return _summarise_time_series(columns, rows)
    return f"Query returned {row_count} row(s)."


def _summarise_time_series(columns: list[str], rows: list[list]) -> str:
    """For a TIME_SERIES result, compute the delta between first and last row.

    Looks for a FIC_MIS_DATE column and a numeric value column; reports
    `col: value_start -> value_end (delta = +/-X)`. Falls back to a
    generic row count if the shape is unexpected.
    """
    if not rows:
        return "No rows returned for the date range."
    if len(rows) == 1:
        return f"Time-series query returned only 1 row (expected 2). Value: {rows[0]}."

    upper_cols = [str(c).upper() for c in columns]
    date_idx = upper_cols.index("FIC_MIS_DATE") if "FIC_MIS_DATE" in upper_cols else None
    # Prefer an N_*, then V_*, then anything else that isn't the date column
    value_idx: Optional[int] = None
    for i, c in enumerate(upper_cols):
        if i == date_idx:
            continue
        if c.startswith("N_"):
            value_idx = i
            break
    if value_idx is None:
        for i, c in enumerate(upper_cols):
            if i != date_idx:
                value_idx = i
                break

    first, last = rows[0], rows[-1]
    if value_idx is None:
        return f"Time-series query returned {len(rows)} rows: {first} -> {last}."

    v_start = first[value_idx]
    v_end = last[value_idx]
    d_start = first[date_idx] if date_idx is not None else "start"
    d_end = last[date_idx] if date_idx is not None else "end"
    col_name = columns[value_idx] if value_idx < len(columns) else "value"
    delta_str = ""
    try:
        delta = float(v_end) - float(v_start)
        sign = "+" if delta > 0 else ""
        delta_str = f" (delta = {sign}{delta:g})"
    except (TypeError, ValueError):
        pass
    return f"{col_name}: {v_start} on {d_start} -> {v_end} on {d_end}{delta_str}."


def _build_explanation(
    summary: str,
    sql: str,
    params: dict,
    rows: list[list],
    columns: list[str],
    truncated: bool,
    display_limit: int,
) -> str:
    """Deterministic markdown explanation — no LLM."""
    lines: list[str] = [f"**Summary:** {summary}", ""]

    if rows:
        header = columns if columns else [f"col{i}" for i in range(len(rows[0]))]
        if len(rows) == 1 and len(rows[0]) == 1:
            lines.append(f"**Result:** `{rows[0][0]}`")
        else:
            lines.append("**Rows:**")
            lines.append("")
            lines.append("| " + " | ".join(header) + " |")
            lines.append("|" + "|".join("---" for _ in header) + "|")
            preview = rows[: min(20, len(rows))]
            for r in preview:
                cells = [str(v) if v is not None else "" for v in r]
                if len(cells) < len(header):
                    cells.extend([""] * (len(header) - len(cells)))
                lines.append("| " + " | ".join(cells) + " |")
            if len(rows) > len(preview):
                lines.append("")
                lines.append(f"_…showing first {len(preview)} of {len(rows)}._")
        lines.append("")

    if truncated:
        lines.append(
            f"_Truncated at {display_limit} rows — total result set was larger._"
        )
        lines.append("")

    lines.append("**SQL executed:**")
    lines.append("")
    lines.append("```sql")
    lines.append(sql.strip())
    lines.append("```")
    if params:
        lines.append("")
        lines.append(f"**Bind params:** `{json.dumps(params, default=str)}`")
    return "\n".join(lines).strip()
