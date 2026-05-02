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
from typing import Any, AsyncIterator, Optional

from langchain_core.messages import SystemMessage, HumanMessage
from tenacity import RetryError

from src.agents.ambiguity import (
    build_identifier_ambiguous_response,
    detect_identifier_ambiguity,
)
from src.llm_factory import create_llm
from src.llm_errors import sanitize_llm_exception
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id
from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.schema_discovery import schemas_for_table
from src.parsing.store import get_column_index
from src.telemetry import stage_timer
from src.tools.sql_guardian import (
    CharPaddingError,
    ColumnResidencyError,
    GuardianRejectionError,
)

logger = get_logger(__name__, concern="app")


DEFAULT_HARD_LIMIT = 10_000
DEFAULT_WARN_LIMIT = 100
DEFAULT_DISPLAY_LIMIT = 100

# When the resolved target schema matches this, the catalog renders bare
# table names (no `SCHEMA.` prefix) — preserves the rendered prompt for
# OFSMDM-only canaries unchanged. Other schemas always render qualified.
_DEFAULT_LEGACY_SCHEMA = "OFSMDM"

# Phase 4 table-token regex: matches OFSAA staging / fact / dimension /
# setup / DIM / DWH / mapping table names that appear in user queries.
# Function-name prefixes (FN_, TLX_, ABL_, MAPPING_) are deliberately
# excluded — those are typically called as functions in user questions,
# not queried as tables.
_USER_QUERY_TABLE_RE = re.compile(
    r"\b((?:STG|FCT|FSI|DIM|SETUP|OFSDWH|INTERNAL|MAP)_[A-Z][A-Z0-9_]+)\b",
    re.IGNORECASE,
)

# Token list — tables that look table-shaped by their prefix but should
# be ignored when extracting from user queries (e.g. system tokens).
_USER_QUERY_TABLE_SKIP = frozenset({"DIM_DATES", "DIM_DATE"})

TABLE_AMBIGUOUS_TYPE = "table_ambiguous"


SYSTEM_PROMPT = """You generate a single Oracle read-only SELECT statement.

HARD CONSTRAINTS — violating any of these produces an invalid response:
- SELECT only. No INSERT, UPDATE, DELETE, MERGE, DDL.
- Use Oracle bind variables `:param_name` for every value that varies
  (dates, codes, ids, numbers). Never inline user values.
- Reference ONLY tables and columns listed in the provided schema.
  If the question needs a table that is not listed, respond with the
  special JSON shape `{"unsupported": true, "reason": "..."}`.
- SCHEMA QUALIFICATION: each table block in the schema below is rendered
  as either `Table: SCHEMA.TABLE` (multi-schema deployments) or
  `Table: TABLE` (single-schema). When a `SCHEMA.TABLE` form is shown,
  you MUST write the same `SCHEMA.TABLE` qualifier in your `FROM` and
  `JOIN` clauses — Oracle resolves an unqualified name in the connected
  user's default schema, which is the wrong schema for tables shown
  with an explicit qualifier. Bare-table catalogs do NOT need
  qualification.
- COLUMN RESIDENCY: you MUST only use columns that are listed under the
  table you are querying FROM. A column listed under STG_PRODUCT_PROCESSOR
  does NOT exist on STG_GL_DATA (and vice-versa) unless it also appears
  under that other table in the schema block. If a question asks about
  accounts but the relevant value column lives on a different table from
  the filter column, you must JOIN the two tables on their shared keys
  (V_GL_CODE, V_LV_CODE, FIC_MIS_DATE) — never invent a column on the
  wrong table.
- Single statement. No semicolons, no PL/SQL blocks.
- Date columns (any column ending in _DATE or named *FIC_MIS_DATE*) must
  be bound via `TO_DATE(:param_name, 'YYYY-MM-DD')`, not as bare strings.
  Pass the date as a 'YYYY-MM-DD' string in params. Apply this rule to
  BOTH the main SQL and count_sql.

CHAR COLUMN HANDLING — Oracle CHAR(n) columns store blank-padded values.
A direct equality between a CHAR(n) column and a VARCHAR2 bind variable
is evaluated with non-padded semantics and returns ZERO matches when
n > len(value). The schema block below spells out each column's data
type; every column shown as `CHAR(n)` (not VARCHAR2, not NUMBER) needs
special handling:
- When comparing a CHAR column to a bind variable, ALWAYS wrap the
  column reference in RTRIM so the trailing spaces are stripped:
    WRONG:   WHERE F_EXPOSURE_ENABLED_IND = :exposure_ind
    CORRECT: WHERE RTRIM(F_EXPOSURE_ENABLED_IND) = :exposure_ind
- The rule applies to `=`, `!=`, `<>`, and `IN (:bind, ...)` predicates
  on CHAR columns. Apply it in the main SQL AND in count_sql.
- VARCHAR2 / NUMBER / DATE columns NEVER need RTRIM. Adding it to a
  VARCHAR2 column is harmless but pointless — don't do it by default.
- CHAR-to-CHAR column comparisons (no bind involved) do not need RTRIM;
  Oracle's blank-padded comparison semantics handle those correctly.

Positive example (F_EXPOSURE_ENABLED_IND is CHAR(3)):
  SELECT COUNT(DISTINCT V_ACCOUNT_NUMBER) AS ACCOUNT_COUNT
  FROM STG_PRODUCT_PROCESSOR
  WHERE RTRIM(F_EXPOSURE_ENABLED_IND) = :exposure_ind
    AND FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')

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
        target_variable: Optional[str] = None,
    ) -> dict:
        """Backward-compatible wrapper around :meth:`answer_stream`.

        Drives the streaming generator to completion and returns the
        terminal ``("result", payload)`` payload as a plain dict. Stage
        events are silently discarded here. New SSE callers should
        consume :meth:`answer_stream` directly.
        """
        result: Optional[dict] = None
        async for event in self.answer_stream(
            user_query=user_query,
            schema=schema,
            filters=filters,
            provider=provider,
            model=model,
            force=force,
            target_variable=target_variable,
        ):
            if event[0] == "result":
                result = event[1]  # type: ignore[assignment]
        if result is None:
            return self._error_result(
                status="generation_error",
                user_query=user_query,
                explanation=(
                    "Internal error: data_query stream finished without "
                    "producing a result."
                ),
            )
        return result

    async def answer_stream(
        self,
        user_query: str,
        schema: str,
        filters: Optional[dict] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        force: bool = False,
        target_variable: Optional[str] = None,
    ) -> AsyncIterator[tuple]:
        """Generate + execute SQL for a data query, yielding progress.

        Yields one of:

        * ``("stage", stage_name, message)`` immediately before each
          significant sub-stage begins. Stage names mirror existing
          ``event: stage`` names where the work is analogous (``"search"``,
          ``"fetch"``, ``"explain"``) so the frontend's stage rendering
          stays consistent. Other stage names: ``"generating_sql"``
          (LLM SQL hop), ``"validating"`` (Guardian + residency + CHAR
          checks), ``"checking_size"`` (ROW_LIST count pre-check only).
        * ``("result", payload_dict)`` exactly once, as the terminal
          yield. ``payload_dict`` matches the pre-W34a ``answer()`` return
          shape — same keys, same semantics. Badge / warnings / sanity
          flags live in ``payload_dict`` and are the source of truth.

        Behaviour of every sub-stage is identical to the pre-W34a
        ``answer()``: the only changes are (a) progressive stage events
        emitted at TRUE sub-stage boundaries, and (b) the terminal
        return became a yield. SQL generation, Guardian validation,
        Oracle execution, and result-shape construction are unchanged.
        """
        correlation_id = get_correlation_id()
        filters = dict(filters or {})

        # Phase 4 routing: when the user query names a table, resolve it
        # across all discovered schemas before building the catalog. If
        # the named table lives in exactly one schema, pivot to that
        # schema (so OFSERM-table queries no longer get a default-OFSMDM
        # catalog). When the named table is ambiguous across schemas,
        # short-circuit with a CLARIFICATION response — never guess.
        target_schema = schema
        ambiguity_info = None
        try:
            target_schema, ambiguity_info = _resolve_target_schema(
                user_query=user_query,
                default_schema=schema,
                redis_client=self._redis,
            )
        except Exception as exc:
            logger.warning("DataQuery schema resolution failed: %s", exc)

        if ambiguity_info is not None:
            logger.info(
                "DataQuery table ambiguous across schemas | table=%s schemas=%s",
                ambiguity_info["table"], ambiguity_info["schemas"],
            )
            yield ("result", _build_table_ambiguous_response(
                table=ambiguity_info["table"],
                schemas=ambiguity_info["schemas"],
                user_query=user_query,
            ))
            return

        if target_schema != schema:
            logger.info(
                "DataQuery routing pivoted: %s -> %s based on user-named table",
                schema, target_schema,
            )

        yield ("stage", "search", "Building schema catalog...")
        try:
            with stage_timer("data_query_schema_catalog_build", correlation_id):
                catalog_text, tables_to_columns, column_types = self._build_schema_catalog(
                    target_schema, qualify_in_prompt=(target_schema != _DEFAULT_LEGACY_SCHEMA)
                )
        except Exception as exc:
            logger.warning("DataQuery catalog build failed: %s", exc)
            catalog_text = "(schema catalog unavailable — rely on commonly-known OFSAA STG tables)"
            tables_to_columns = {}
            column_types = {}

        # Identifier-ambiguity check — short-circuits before SQL generation
        # when the target column lives on multiple tables and the user gave
        # only a bare identifier. Detection is a pure catalog lookup.
        ambiguity_candidates = detect_identifier_ambiguity(
            target_column=target_variable,
            filters=filters,
            tables_to_columns=tables_to_columns,
            user_query=user_query,
        )
        if ambiguity_candidates:
            logger.info(
                "DataQuery identifier ambiguous | target=%s candidates=%s",
                target_variable,
                [c["table"] for c in ambiguity_candidates],
            )
            yield ("result", build_identifier_ambiguous_response(
                target_column=(target_variable or "").strip().upper(),
                filters=filters,
                user_query=user_query,
                candidates=ambiguity_candidates,
            ))
            return

        yield ("stage", "generating_sql", "Generating SQL for your question...")
        try:
            with stage_timer("llm_api_sql_generate", correlation_id, provider=(provider or "default")):
                plan = await self._generate_sql(
                    user_query=user_query,
                    filters=filters,
                    catalog_text=catalog_text,
                    provider=provider,
                    model=model,
                )
        except Exception as exc:
            logger.error("DataQuery SQL generation failed: %s", exc)
            yield ("result", self._error_result(
                status="generation_error",
                user_query=user_query,
                explanation=(
                    "I couldn't turn your question into a SQL query. "
                    f"Reason: {exc}. Try rephrasing with explicit column / "
                    "filter names."
                ),
            ))
            return

        if plan.get("unsupported"):
            yield ("result", {
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
            })
            return

        sql = plan["sql"]
        params = plan.get("params") or {}
        query_kind = plan.get("query_kind") or "ROW_LIST"
        count_sql = plan.get("count_sql")
        select_columns = plan.get("select_columns") or []

        yield ("stage", "validating", "Validating the generated SQL...")
        # Guardian validation (hard stop on DML/DDL or interpolation)
        try:
            self._guardian.validate(sql)
            if params:
                self._guardian.check_bind_variables(sql, params)
        except GuardianRejectionError as exc:
            logger.error("DataQuery guardian rejected generated SQL: %s", exc)
            yield ("result", self._error_result(
                status="validation_error",
                user_query=user_query,
                sql=sql,
                params=params,
                explanation=(
                    "The generated SQL was rejected by the SQL Guardian. "
                    f"Reason: {exc.message}. No execution performed."
                ),
            ))
            return

        # Pre-execution column residency check — catches LLM hallucinations
        # where a column is referenced against a table it doesn't live on.
        if tables_to_columns:
            try:
                self._guardian.validate_column_residency(sql, tables_to_columns)
            except ColumnResidencyError as exc:
                logger.error(
                    "DataQuery column residency rejected | column=%s table=%s sql=%s",
                    exc.column, exc.table, sql,
                )
                yield ("result", self._query_generation_error(
                    reason="column_not_found",
                    user_query=user_query,
                    sql=sql,
                    params=params,
                    user_message=(
                        f"The generated SQL references column {exc.column} "
                        f"against table {exc.table}, but that column doesn't "
                        "exist on that table. This is a query-generation bug. "
                        "Please rephrase your question and try again."
                    ),
                    suggestion=(
                        "Try naming the target table explicitly, or rephrase "
                        "with the column you actually want to see."
                    ),
                ))
                return

        # Pre-execution CHAR-padding check — catches Oracle CHAR(n) columns
        # compared against a VARCHAR2 bind without RTRIM, which silently
        # returns zero matches due to trailing-space padding semantics.
        if column_types:
            try:
                self._guardian.validate_char_column_comparisons(sql, column_types)
                if count_sql:
                    self._guardian.validate_char_column_comparisons(
                        count_sql, column_types
                    )
            except CharPaddingError as exc:
                logger.error(
                    "DataQuery CHAR padding rejected | column=%s table=%s sql=%s",
                    exc.column, exc.table, sql,
                )
                yield ("result", self._query_generation_error(
                    reason="char_padding_mismatch",
                    user_query=user_query,
                    sql=sql,
                    params=params,
                    user_message=(
                        f"The generated SQL compares CHAR column {exc.column} "
                        f"(on {exc.table}) to a bind variable without using "
                        "RTRIM. Oracle CHAR(n) columns are blank-padded, so "
                        "this comparison would silently return zero rows. "
                        "Please retry — the prompt now enforces RTRIM on "
                        "CHAR comparisons."
                    ),
                    suggestion=(
                        "If you re-ask the same question, the generator will "
                        "wrap the CHAR column in RTRIM automatically."
                    ),
                ))
                return

        # Safeguard 1: row count pre-check (skipped for aggregate queries).
        warnings: list[str] = []
        if query_kind == "ROW_LIST" and count_sql:
            yield ("stage", "checking_size", "Checking how many rows match...")
            try:
                self._guardian.validate(count_sql)
                if params:
                    self._guardian.check_bind_variables(count_sql, params)
                with stage_timer("oracle_count_precheck", correlation_id):
                    count_rows = await self._schema_tools.execute_raw(count_sql, params)
                total_rows = int(count_rows[0][0]) if count_rows else 0
            except Exception as exc:
                logger.warning("DataQuery count pre-check failed: %s", exc)
                total_rows = None
                warnings.append(f"count pre-check failed: {exc}")

            if total_rows is not None:
                if total_rows > self._hard_limit:
                    yield ("result", {
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
                    })
                    return
                if total_rows > self._warn_limit and not force:
                    yield ("result", {
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
                    })
                    return

        # Safeguard 3: inject display limit for row-listing queries.
        exec_sql = sql
        if query_kind == "ROW_LIST":
            exec_sql = self._guardian.inject_fetch_limit(
                sql, limit=self._display_limit
            )

        yield ("stage", "fetch", "Querying Oracle...")
        # Execute
        try:
            with stage_timer("oracle_query_execute", correlation_id, query_kind=query_kind):
                rows = await self._schema_tools.execute_raw(exec_sql, params)
        except Exception as exc:
            inner = _unwrap_retry_error(exc)
            ora_code, ora_message = _extract_oracle_error(inner)
            logger.error(
                "DataQuery Oracle error | code=%s msg=%s sql=%s params=%s",
                ora_code or "unknown",
                ora_message or str(inner),
                exec_sql,
                params,
            )
            reason, user_message, suggestion = _sanitize_oracle_error(ora_code)
            yield ("result", self._query_generation_error(
                reason=reason,
                user_query=user_query,
                sql=exec_sql,
                params=params,
                user_message=user_message,
                suggestion=suggestion,
                warnings=warnings,
            ))
            return

        columns = select_columns or _column_names_from_sql(exec_sql)
        materialised = _materialise_rows(rows)
        row_count = len(materialised)

        # TIME_SERIES: pad the display rows so every requested date is
        # present (missing ones show "no data"). Raw Oracle row_count is
        # preserved — only the display rows are padded.
        if query_kind == "TIME_SERIES":
            display_rows, requested_dates = _pad_time_series_rows(
                materialised, columns, params
            )
            summary = _summarise_time_series(columns, materialised, params)
        else:
            display_rows = materialised
            requested_dates = []
            summary = _summarise(
                user_query=user_query,
                query_kind=query_kind,
                columns=columns,
                rows=materialised,
                row_count=row_count,
            )

        # Post-execution suspicious-result check: a zero-result aggregate
        # against a populated target table is a classic symptom of the
        # CHAR/VARCHAR2 padding trap or similar silent filter failures.
        # Downgrades the response from VERIFIED to UNVERIFIED.
        with stage_timer("suspicious_result_check", correlation_id):
            suspicious, suspicion_reason = await self._check_suspicious_result(
                sql=exec_sql,
                query_kind=query_kind,
                columns=columns,
                rows=materialised,
                params=params,
            )
        if suspicious:
            warnings = list(warnings) + [
                f"suspicious_zero_result: {suspicion_reason}"
            ]
            logger.warning(
                "DataQuery suspicious result flagged | reason=%s sql=%s params=%s",
                suspicion_reason, exec_sql, params,
            )

        yield ("stage", "explain", "Formatting the results...")
        explanation = _build_explanation(
            summary=summary,
            sql=exec_sql,
            params=params,
            rows=display_rows[: self._display_limit],
            columns=columns,
            truncated=row_count > self._display_limit,
            display_limit=self._display_limit,
        )
        if suspicious:
            explanation = (
                "> \u26a0\ufe0f **UNVERIFIED — suspicious result.** "
                f"{suspicion_reason}\n\n" + explanation
            )

        yield ("result", {
            "status": "answered",
            "query_kind": query_kind,
            "schema": target_schema,
            "sql": exec_sql,
            "count_sql": count_sql,
            "params": params,
            "rows": display_rows[: self._display_limit],
            "columns": columns,
            "row_count": row_count,
            "requested_dates": requested_dates,
            "summary": summary,
            "explanation": explanation,
            "sanity_warnings": warnings,
            "suspicious": suspicious,
            "suspicion_reason": suspicion_reason if suspicious else None,
            "verification_sql": count_sql or exec_sql,
            "correlation_id": correlation_id,
        })

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

        try:
            response = await llm.ainvoke(messages)
        except Exception as exc:
            raise sanitize_llm_exception(
                exc, context="data_query_generate_sql"
            ) from exc
        raw = (response.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        parsed = json.loads(raw)
        return parsed

    def _build_schema_catalog(
        self, schema: str, qualify_in_prompt: bool = False,
    ) -> tuple[str, dict[str, set[str]], dict[str, dict[str, dict]]]:
        """Build a per-table catalog from the graph + Oracle type snapshot.

        Three data sources are combined:
        1. Per-function graphs — INSERT/UPDATE column_maps give the precise
           `table → columns` mapping for the columns the PL/SQL touches.
        2. The raw source of each function — INSERT `(COL, COL, ...)`
           column lists are parsed and attributed to the target table.
        3. The Oracle schema snapshot cached in Redis under
           `rtie:schema:snapshot:<schema>` — provides each column's data
           type, length, precision, and scale so the LLM can avoid the
           CHAR(n) blank-padding trap. Phase 4: this snapshot is also
           consulted as a column-set fall-through for tables that appear
           in the graph as a read-only source (`source_tables`) but
           never as an INSERT target — DIM_DATES is the prime example.
           Without the fall-through the catalog rendered an empty
           `Columns:` block for those tables and the LLM either aborted
           with `unsupported` or generated SQL the residency check then
           rejected.

        Returns
        -------
        (catalog_text, tables_to_columns, column_types):
            * `catalog_text` is the per-table block rendered for the LLM
              prompt, with each column annotated by its Oracle data type
              when available.
            * `tables_to_columns` is the authoritative `{TABLE: {COL, ...}}`
              mapping used by SQLGuardian.validate_column_residency.
            * `column_types` is `{TABLE: {COL: {data_type, length,
              precision, scale}}}` — empty when the snapshot has not been
              refreshed. Used by SQLGuardian.validate_char_column_comparisons
              to reject CHAR(n) bind comparisons that skip RTRIM.
        """
        tables_to_columns = build_tables_to_columns(self._redis, schema)
        column_types = load_column_types(self._redis, schema)

        # Phase 4: when a table is in the catalog because some function
        # READ from it (source_tables) but never INSERTed to it, the
        # graph-derived column set is empty. Fall through to the Oracle
        # snapshot — the columns are real, RTIE just hadn't seen any
        # INSERT statement to attribute them to. Without this enrichment
        # the LLM sees an empty `Columns:` block for tables like
        # OFSERM.DIM_DATES (read by ~141 OFSERM functions, INSERTed to by
        # zero) and either aborts with `unsupported` or generates SQL
        # that SQLGuardian then rejects on column residency.
        for table, types_for_table in column_types.items():
            if table in tables_to_columns and not tables_to_columns[table]:
                tables_to_columns[table] = {
                    str(c).upper() for c in types_for_table.keys()
                }

        if not tables_to_columns:
            return "(no tables discovered — schema catalog empty)", {}, column_types

        lines: list[str] = []
        table_prefix = f"{schema}." if qualify_in_prompt and schema else ""
        for table in sorted(tables_to_columns.keys()):
            cols = sorted(tables_to_columns[table])
            lines.append(f"Table: {table_prefix}{table}")
            if not cols:
                lines.append("Columns: (none discovered in graph)")
                lines.append("")
                continue
            table_types = column_types.get(table, {})
            if table_types:
                lines.append("Columns:")
                for col in cols:
                    type_str = format_column_type(table_types.get(col))
                    if type_str:
                        lines.append(f"  {col} {type_str}")
                    else:
                        lines.append(f"  {col}")
            else:
                lines.append(f"Columns: {', '.join(cols)}")
            lines.append("")

        return "\n".join(lines).rstrip(), tables_to_columns, column_types

    async def _check_suspicious_result(
        self,
        sql: str,
        query_kind: str,
        columns: list[str],
        rows: list[list],
        params: dict,
    ) -> tuple[bool, Optional[str]]:
        """Flag a zero-result aggregate against a populated target table.

        A COUNT/SUM/aggregate that returns 0 or NULL, while the target
        table has rows at the same date filter, is a classic symptom of
        silent filter failures (CHAR padding, case mismatches, stale
        binds). We run one cheap Oracle query — the baseline row count
        for the target table at the filter date — and flag when the
        baseline is positive but the answer is zero.

        Returns ``(suspicious, reason_text)``. ``suspicious`` is False
        whenever we can't confidently make a call (unknown table, no
        date filter, no non-date predicates, baseline query failed).
        """
        if query_kind != "AGGREGATE" or not rows:
            return False, None

        first_row = rows[0]
        if not first_row:
            return False, None
        first_value = first_row[0]
        if first_value not in (0, 0.0, None, "0"):
            try:
                if float(first_value) != 0.0:
                    return False, None
            except (TypeError, ValueError):
                return False, None

        # Require the SQL to have a non-date WHERE predicate — if the only
        # filter is the date, the zero count is just "no data that day".
        stripped = _strip_sql_literals(sql)
        where_text = _extract_where_clause(stripped)
        if not where_text:
            return False, None
        predicates = _extract_predicate_columns(where_text)
        non_date_predicates = [
            col for col in predicates
            if col != "FIC_MIS_DATE" and not col.endswith("_DATE")
        ]
        if not non_date_predicates:
            return False, None

        mis_date = (
            params.get("mis_date")
            or params.get("fic_mis_date")
            or params.get("date")
        )
        if not mis_date:
            return False, None

        target_table = _extract_primary_from_table(stripped)
        if not target_table:
            return False, None

        baseline_sql = (
            f"SELECT COUNT(*) FROM {target_table} "
            "WHERE FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')"
        )
        try:
            baseline_rows = await self._schema_tools.execute_raw(
                baseline_sql, {"mis_date": mis_date}
            )
            baseline = int(baseline_rows[0][0]) if baseline_rows else 0
        except Exception as exc:
            logger.info(
                "Suspicious-result baseline query failed (non-fatal): %s", exc
            )
            return False, None

        if baseline <= 0:
            return False, None

        reason = (
            f"The query returned 0, but table {target_table} has "
            f"{baseline:,} row(s) at {mis_date}. The filter on "
            f"{', '.join(non_date_predicates)} may have a data-type "
            "mismatch (CHAR padding, case sensitivity) or a bad value. "
            "Please verify the SQL before trusting this result."
        )
        return True, reason

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

    def _query_generation_error(
        self,
        reason: str,
        user_query: str,
        sql: str,
        params: dict,
        user_message: str,
        suggestion: str,
        warnings: Optional[list[str]] = None,
    ) -> dict:
        """Structured response for LLM-generated SQL that Oracle rejected
        or that failed column residency. Distinct from infrastructure
        errors so the frontend can frame it as "rephrase your question"
        rather than "the system is broken"."""
        explanation = (
            f"**{user_message}**\n\n"
            f"**Suggestion:** {suggestion}\n\n"
            "**SQL that was rejected:**\n\n"
            "```sql\n"
            f"{(sql or '').strip()}\n"
            "```"
        )
        if params:
            explanation += (
                f"\n\n**Bind params:** `{json.dumps(params, default=str)}`"
            )
        return {
            "status": "query_generation_error",
            "type": "query_generation_error",
            "reason": reason,
            "query_kind": None,
            "sql": sql,
            "count_sql": None,
            "params": params or {},
            "bind_params": params or {},
            "rows": [],
            "columns": [],
            "row_count": 0,
            "summary": f"Could not answer: {user_query}",
            "user_message": user_message,
            "suggestion": suggestion,
            "explanation": explanation,
            "sanity_warnings": warnings or [],
            "verification_sql": None,
            "correlation_id": get_correlation_id(),
        }


# ---------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------

def load_column_types(
    redis_client,
    schema: Optional[str] = None,
    key_prefix: str = "rtie",
) -> dict[str, dict[str, dict]]:
    """Load per-column Oracle data types from the cached schema snapshot.

    The async CacheClient writes the snapshot under
    ``<key_prefix>:schema:snapshot:<schema>`` during ``/refresh-schema``.
    This helper reads that same key via the sync graph-pipeline Redis
    client so DataQueryAgent can render types in the LLM catalog and the
    SQLGuardian can validate CHAR comparisons.

    *schema* scopes the lookup to a single Oracle owner. ``None`` (the
    Phase 2 default) iterates every schema discovered in Redis and
    merges the per-schema snapshots into a single map, so a caller that
    doesn't know the schema (or wants visibility across schemas) gets
    the union. When two schemas share a table name, the snapshot read
    later wins — a documented but rare collision in the OFSAA corpora.

    Returns ``{TABLE_UPPER: {COL_UPPER: {data_type, data_length,
    data_precision, data_scale}}}`` or ``{}`` when no snapshot is
    available, Redis is unreachable, or every payload is malformed.
    Never raises — a missing snapshot degrades gracefully to an
    untyped catalog.
    """
    if redis_client is None:
        return {}

    if schema is None:
        from src.parsing.schema_discovery import discovered_schemas
        schemas = discovered_schemas(redis_client)
    else:
        schemas = [schema]

    out: dict[str, dict[str, dict]] = {}
    for sch in schemas:
        per_schema = _load_column_types_one_schema(
            redis_client, sch, key_prefix
        )
        for table_upper, table_out in per_schema.items():
            # Same table in two schemas: later schema wins. OFSAA's
            # canonical naming makes this a non-issue in practice.
            existing = out.get(table_upper)
            if existing:
                merged = dict(existing)
                merged.update(table_out)
                out[table_upper] = merged
            else:
                out[table_upper] = table_out
    return out


def _load_column_types_one_schema(
    redis_client,
    schema: str,
    key_prefix: str,
) -> dict[str, dict[str, dict]]:
    """Single-schema implementation of :func:`load_column_types`."""
    key = f"{key_prefix}:schema:snapshot:{schema}"
    try:
        raw = redis_client.get(key)
    except Exception as exc:
        logger.info("Redis GET failed for schema snapshot %s: %s", key, exc)
        return {}
    if raw is None:
        return {}
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        payload = json.loads(raw)
    except Exception as exc:
        logger.info("Schema snapshot parse failed for %s: %s", key, exc)
        return {}

    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not isinstance(tables, dict):
        return {}

    out: dict[str, dict[str, dict]] = {}
    for table_name, table_info in tables.items():
        if not isinstance(table_info, dict):
            continue
        cols = table_info.get("columns")
        if not isinstance(cols, dict):
            continue
        table_upper = str(table_name).upper()
        table_out: dict[str, dict] = {}
        for col_name, col_info in cols.items():
            if not isinstance(col_info, dict):
                continue
            table_out[str(col_name).upper()] = {
                "data_type": str(col_info.get("data_type") or "").upper(),
                "data_length": col_info.get("data_length"),
                "data_precision": col_info.get("data_precision"),
                "data_scale": col_info.get("data_scale"),
                "nullable": col_info.get("nullable"),
            }
        if table_out:
            out[table_upper] = table_out
    return out


def format_column_type(type_info: Optional[dict]) -> str:
    """Format a single column's type metadata as `VARCHAR2(50)`, `CHAR(3)`,
    `NUMBER(10,2)`, `DATE`, etc.

    Returns an empty string when ``type_info`` is missing so callers can
    render the bare column name unchanged.
    """
    if not type_info:
        return ""
    dtype = str(type_info.get("data_type") or "").upper().strip()
    if not dtype:
        return ""
    length = type_info.get("data_length")
    precision = type_info.get("data_precision")
    scale = type_info.get("data_scale")

    if dtype in ("CHAR", "NCHAR", "VARCHAR2", "VARCHAR", "NVARCHAR2", "RAW"):
        try:
            n = int(length) if length is not None else None
        except (TypeError, ValueError):
            n = None
        return f"{dtype}({n})" if n else dtype
    if dtype == "NUMBER":
        try:
            p = int(precision) if precision is not None else None
        except (TypeError, ValueError):
            p = None
        try:
            s = int(scale) if scale is not None else None
        except (TypeError, ValueError):
            s = None
        if p is not None and s is not None:
            return f"NUMBER({p},{s})"
        if p is not None:
            return f"NUMBER({p})"
        return "NUMBER"
    return dtype


def build_tables_to_columns(
    redis_client,
    schema: Optional[str] = None,
) -> dict[str, set[str]]:
    """Build per-table `{table: {columns}}` mapping from the graph in Redis.

    Shared by `DataQueryAgent` (for SQL generation + residency checks) and
    `ValueTracerAgent` (for identifier-ambiguity detection).

    *schema* scopes the scan to a single Oracle owner. ``None`` (the
    Phase 2 default) iterates every schema discovered in Redis and
    aggregates the table → column mapping across all of them. Tables
    shared between schemas (none today, but defensively handled) merge
    column sets via union. Returns an empty dict when Redis is
    unavailable or no graphs are stored.
    """
    tables_to_columns: dict[str, set[str]] = {}
    if redis_client is None:
        return tables_to_columns

    if schema is None:
        from src.parsing.schema_discovery import discovered_schemas
        schemas = discovered_schemas(redis_client)
    else:
        schemas = [schema]

    for sch in schemas:
        for table, cols in _build_tables_to_columns_one_schema(
            redis_client, sch
        ).items():
            tables_to_columns.setdefault(table, set()).update(cols)

    return tables_to_columns


def _build_tables_to_columns_one_schema(
    redis_client,
    schema: str,
) -> dict[str, set[str]]:
    """Single-schema implementation of :func:`build_tables_to_columns`."""
    tables_to_columns: dict[str, set[str]] = {}

    try:
        keys = redis_client.keys(SchemaAwareKeyspace.graph_scan_pattern(schema)) or []
    except Exception as exc:
        logger.warning(
            "Redis keys() failed during catalog build for %s: %s", schema, exc
        )
        return tables_to_columns

    from src.parsing.store import (
        get_function_graph,
        get_raw_source,
    )

    # SchemaAwareKeyspace.parse_graph_key rejects family keys
    # (graph:meta:*, graph:full:*, graph:source:*, ...) so we don't need
    # a local reserved-subkey set; mismatched-schema keys are also
    # filtered out by the parsed_schema != schema check.
    for raw_key in keys:
        key = (
            raw_key.decode("utf-8", errors="ignore")
            if isinstance(raw_key, (bytes, bytearray))
            else str(raw_key)
        )
        parsed = SchemaAwareKeyspace.parse_graph_key(key)
        if parsed is None or parsed[0] != schema:
            continue
        function_name = parsed[1]
        graph = get_function_graph(redis_client, schema, function_name)
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

        raw_lines = get_raw_source(redis_client, schema, function_name)
        if raw_lines:
            for table, cols in _parse_insert_column_lists(raw_lines).items():
                tables_to_columns.setdefault(table, set()).update(cols)

    return tables_to_columns


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


_ORA_CODE_RE = re.compile(r"ORA-(\d{5})", re.IGNORECASE)


_WHERE_CLAUSE_RE = re.compile(
    r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bORDER\s+BY\b|\bHAVING\b|"
    r"\bFETCH\b|\bUNION\b|$)",
    re.IGNORECASE | re.DOTALL,
)
_FROM_PRIMARY_RE = re.compile(
    r"\bFROM\s+([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)",
    re.IGNORECASE,
)


def _extract_user_query_tables(user_query: str) -> list[str]:
    """Return OFSAA-shaped table tokens found in *user_query*.

    Deduplicated, upper-cased. Used by Phase 4 schema routing to decide
    which schema to pivot to when the user names a specific table.
    """
    if not user_query:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in _USER_QUERY_TABLE_RE.finditer(user_query):
        token = match.group(1).upper()
        if token in seen or token in _USER_QUERY_TABLE_SKIP:
            continue
        seen.add(token)
        out.append(token)
    return out


def _resolve_target_schema(
    user_query: str,
    default_schema: str,
    redis_client,
) -> tuple[str, Optional[dict]]:
    """Phase 4 routing: pivot to the schema that owns the user-named table.

    Returns ``(schema_to_use, ambiguity_info)``:

    * ``(single_schema, None)`` — every table named in *user_query* lives
      in the same schema. ``single_schema`` may differ from
      *default_schema* (Phase 4: pivot to the table's owner).
    * ``(default_schema, {"table": ..., "schemas": [...]})`` — at least
      one named table lives in two or more schemas. The caller should
      surface this as a CLARIFICATION rather than guessing which schema
      the user meant.
    * ``(default_schema, None)`` — no recognised table token in the
      query, no resolution found in any schema, or multiple named
      tables span multiple schemas (cross-schema query — Phase 4 leaves
      these to the catalog visibility path).

    Lookup is case-insensitive on the table name. When *redis_client* is
    None, the function is a no-op and returns ``(default_schema, None)``.
    """
    if not user_query or redis_client is None:
        return default_schema, None

    candidates = _extract_user_query_tables(user_query)
    if not candidates:
        return default_schema, None

    schemas_seen: set[str] = set()
    for table in candidates:
        owners = schemas_for_table(table, redis_client)
        if not owners:
            continue
        if len(owners) > 1:
            return default_schema, {
                "table": table,
                "schemas": sorted(owners),
            }
        schemas_seen.add(owners[0])

    if len(schemas_seen) == 1:
        return next(iter(schemas_seen)), None
    return default_schema, None


def _build_table_ambiguous_response(
    table: str,
    schemas: list[str],
    user_query: str,
) -> dict:
    """Construct a CLARIFICATION response when a named table exists in
    multiple schemas.

    Mirrors the ``identifier_ambiguous`` response shape so the existing
    main.py dispatch (``result.get("type") == "table_ambiguous"``) can
    surface the message and suggestions verbatim.
    """
    suggestions = [
        # Replace the bare table name with a schema-qualified suggestion.
        # Each rephrase substitutes only the first occurrence so a query
        # mentioning the same table twice doesn't produce malformed text.
        user_query.replace(table, f"{schema}.{table}", 1)
        for schema in schemas
    ]
    candidate_payload = [
        {"table": table, "schema": schema} for schema in schemas
    ]
    schema_labels = ", ".join(schemas)
    lines = [
        f"`{table}` exists in more than one schema: {schema_labels}.",
        "",
        "Try rephrasing with the schema-qualified table name:",
    ]
    for suggestion in suggestions:
        lines.append(f'  - "{suggestion}"')
    message = "\n".join(lines)
    return {
        "status": TABLE_AMBIGUOUS_TYPE,
        "type":   TABLE_AMBIGUOUS_TYPE,
        "table": table,
        "candidate_schemas": candidate_payload,
        "message": message,
        "suggestions": suggestions,
        "correlation_id": get_correlation_id(),
    }


def _strip_sql_literals(sql: str) -> str:
    """Replace string literals with empty strings so regex predicate scans
    don't pick up tokens inside literals."""
    return re.sub(r"'(?:[^']|'')*'", "''", sql or "")


def _extract_where_clause(sql: str) -> str:
    """Return the WHERE clause body (excluding the WHERE keyword) or ''."""
    match = _WHERE_CLAUSE_RE.search(sql)
    return match.group(1).strip() if match else ""


def _extract_predicate_columns(where_text: str) -> list[str]:
    """Extract OFSAA-shaped column names appearing in a WHERE clause.

    Uses the N_/V_/F_/D_/FIC_/LD_/SETUP_/B_ naming conventions to identify
    column tokens. Works equally well for bare (``V_LV_CODE = :x``),
    qualified (``PP.V_LV_CODE = :x``), and function-wrapped
    (``RTRIM(F_EXPOSURE_ENABLED_IND) = :x``) predicates. Duplicates are
    returned — callers can dedupe if they care.
    """
    return [
        match.group(1).upper()
        for match in _COLUMN_PREFIX_IDENT_RE.finditer(where_text)
    ]


_COLUMN_PREFIX_IDENT_RE = re.compile(
    r"\b((?:N|V|F|D|FIC|LD|SETUP|B)_[A-Za-z0-9_]+)\b"
)


def _extract_primary_from_table(sql: str) -> Optional[str]:
    """Return the first table name after FROM (unqualified, upper-case).
    Returns ``None`` when no FROM is found."""
    match = _FROM_PRIMARY_RE.search(sql)
    if not match:
        return None
    raw = match.group(1)
    return raw.split(".")[-1].upper()


def _unwrap_retry_error(exc: BaseException) -> BaseException:
    """Peel tenacity's RetryError to get the real DatabaseError underneath.

    tenacity wraps every retried call in `RetryError[<Future ...>]`; `str()`
    of that wrapper is the Python Future repr, which is useless for
    debugging. The actual Oracle exception lives at
    `retry_err.last_attempt.exception()`.
    """
    current = exc
    while isinstance(current, RetryError):
        try:
            inner = current.last_attempt.exception()
        except Exception:
            inner = None
        if inner is None or inner is current:
            break
        current = inner
    return current


def _extract_oracle_error(exc: BaseException) -> tuple[Optional[str], Optional[str]]:
    """Return `(full_code, message)` for an oracledb.DatabaseError, else
    `(None, None)`.

    oracledb puts its structured error into `exc.args[0]`, an `_Error` with
    `full_code` (e.g. "ORA-00904"), `code` (int), and `message` attrs.
    Falls back to regex-scraping the stringified exception if those aren't
    present (handles oracledb versions / wrapped variants).
    """
    if exc is None:
        return None, None
    inner = None
    args = getattr(exc, "args", None) or ()
    if args:
        inner = args[0]
    full_code = getattr(inner, "full_code", None) if inner is not None else None
    message = getattr(inner, "message", None) if inner is not None else None
    if not full_code:
        match = _ORA_CODE_RE.search(str(exc))
        if match:
            full_code = f"ORA-{match.group(1)}"
    if not message:
        message = str(exc)
    return full_code, message


_ORA_SANITIZATION = {
    "ORA-00904": (
        "column_not_found",
        "The generated SQL referenced a column that doesn't exist on the "
        "target table. This is a query-generation bug. Please rephrase "
        "your question and try again.",
        "Try naming the table or column you want explicitly — e.g. "
        "\"the N_EOP_BAL from STG_PRODUCT_PROCESSOR\".",
    ),
    "ORA-00942": (
        "table_not_found",
        "The generated SQL referenced a table not in the current schema. "
        "Please rephrase your question or check the table name.",
        "Check that the table you're asking about exists in the parsed "
        "schema catalog.",
    ),
    "ORA-01722": (
        "type_mismatch",
        "The generated SQL had a data type or format error. Please "
        "rephrase your question and try again.",
        "If you're filtering on a date or number, make the format "
        "explicit in the question.",
    ),
    "ORA-01861": (
        "type_mismatch",
        "The generated SQL had a data type or format error. Please "
        "rephrase your question and try again.",
        "Use an ISO date (YYYY-MM-DD) and state the column explicitly.",
    ),
}


def _sanitize_oracle_error(full_code: Optional[str]) -> tuple[str, str, str]:
    """Map an Oracle error code to `(reason, user_message, suggestion)`.

    Never returns the raw Oracle message (which may leak schema info) and
    never returns the Python `RetryError` / Future repr. Callers still log
    the raw error verbatim at ERROR level — this is only for user-facing
    text.
    """
    if full_code and full_code.upper() in _ORA_SANITIZATION:
        return _ORA_SANITIZATION[full_code.upper()]
    return (
        "other_oracle_error",
        "The generated SQL was rejected by the database. Please try "
        "rephrasing your question.",
        "A different wording — or a more specific filter (date, account, "
        "code) — often helps the generator pick the right table.",
    )


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


def _summarise_time_series(
    columns: list[str],
    raw_rows: list[list],
    params: Optional[dict],
) -> str:
    """Return a neutral, factual one-line framing for a TIME_SERIES result.

    Does NOT speculate about why data is missing. States only:
      * how many requested dates returned data, or
      * the change between the two values when all dates returned data
        and the target column is numeric.
    """
    params = params or {}
    start = params.get("start_date")
    end = params.get("end_date")
    requested = _requested_dates(start, end)
    n_requested = len(requested) if requested else len(raw_rows)
    n_found = len(raw_rows)

    if n_found == 0:
        if n_requested:
            return "No data found for any of the requested dates."
        return "No rows returned for the date range."

    if n_requested and n_found < n_requested:
        return (
            f"{n_found} of {n_requested} requested dates has data "
            "for this account."
        )

    # All requested dates returned data. If we have exactly two rows and a
    # numeric target column, compute a deterministic delta.
    upper_cols = [str(c).upper() for c in columns]
    date_idx = upper_cols.index("FIC_MIS_DATE") if "FIC_MIS_DATE" in upper_cols else None
    value_idx = _pick_value_column_idx(upper_cols, date_idx)

    if n_found >= 2 and value_idx is not None and date_idx is not None:
        sorted_rows = sorted(raw_rows, key=lambda r: _row_date_iso(r, date_idx) or "")
        v_start = sorted_rows[0][value_idx]
        v_end = sorted_rows[-1][value_idx]
        try:
            fs = float(v_start)
            fe = float(v_end)
            delta = fe - fs
            sign = "+" if delta > 0 else ""
            col_name = columns[value_idx] if value_idx < len(columns) else "value"
            return (
                f"Change in {col_name}: {v_start} \u2192 {v_end} "
                f"(delta: {sign}{delta:g})."
            )
        except (TypeError, ValueError):
            pass

    return (
        "Both dates returned data." if n_requested == 2
        else f"All {n_requested} requested dates returned data."
    )


def _pad_time_series_rows(
    rows: list[list],
    columns: list[str],
    params: Optional[dict],
) -> tuple[list[list], list[str]]:
    """Produce a display list with one row per requested date.

    For each requested date (from params.start_date / end_date), use the
    Oracle row for that date if present; otherwise emit a placeholder row
    with "no data" in the value column(s) and filter values carried from
    params where they are known.

    Returns (padded_rows, requested_dates_iso). When no date range is
    provided in params, returns (rows unchanged, []).
    """
    params = params or {}
    requested = _requested_dates(params.get("start_date"), params.get("end_date"))
    if not requested:
        return list(rows), []

    upper_cols = [str(c).upper() for c in columns]
    date_idx = upper_cols.index("FIC_MIS_DATE") if "FIC_MIS_DATE" in upper_cols else None
    if date_idx is None:
        # Cannot pad without a date column to pivot on
        return list(rows), requested

    by_date: dict[str, list] = {}
    for r in rows:
        d = _row_date_iso(r, date_idx)
        if d:
            by_date[d] = r

    padded: list[list] = []
    for iso in requested:
        if iso in by_date:
            padded.append(list(by_date[iso]))
        else:
            padded.append(_make_placeholder_row(iso, columns, date_idx, params))
    return padded, requested


def _make_placeholder_row(
    iso_date: str,
    columns: list[str],
    date_idx: int,
    params: dict,
) -> list:
    """Build a row for a missing date: the date itself in the date column,
    known filter values (e.g. V_ACCOUNT_NUMBER from params) in filter
    columns, and "no data" in value columns."""
    row: list = []
    for i, col in enumerate(columns):
        cu = str(col).upper()
        if i == date_idx:
            row.append(iso_date)
        elif cu == "V_ACCOUNT_NUMBER" and params.get("account_number"):
            row.append(params["account_number"])
        elif cu == "V_LV_CODE" and params.get("lv_code"):
            row.append(params["lv_code"])
        elif cu == "V_GL_CODE" and params.get("gl_code"):
            row.append(params["gl_code"])
        elif cu == "V_BRANCH_CODE" and params.get("branch_code"):
            row.append(params["branch_code"])
        elif cu == "V_LOB_CODE" and params.get("lob_code"):
            row.append(params["lob_code"])
        else:
            row.append("no data")
    return row


def _requested_dates(start: Optional[str], end: Optional[str]) -> list[str]:
    """Return the list of requested ISO dates (deduplicated, ordered)."""
    if not start or not end:
        return []
    if start == end:
        return [start]
    # Order: earlier first. Lexical compare works for YYYY-MM-DD.
    return [start, end] if start <= end else [end, start]


def _pick_value_column_idx(upper_cols: list[str], date_idx: Optional[int]) -> Optional[int]:
    """Prefer N_* numeric columns, then anything non-date and non-filter."""
    for i, c in enumerate(upper_cols):
        if i == date_idx:
            continue
        if c.startswith("N_"):
            return i
    # Fall back to any non-date column (useful for non-numeric targets
    # where we just want to report presence of values)
    for i, c in enumerate(upper_cols):
        if i == date_idx:
            continue
        if c in ("V_ACCOUNT_NUMBER", "V_LV_CODE", "V_GL_CODE",
                 "V_BRANCH_CODE", "V_LOB_CODE"):
            continue
        return i
    return None


def _row_date_iso(row: list, date_idx: Optional[int]) -> Optional[str]:
    """Normalize row[date_idx] to YYYY-MM-DD, or None if not parseable."""
    if date_idx is None or date_idx >= len(row):
        return None
    val = row[date_idx]
    if val is None:
        return None
    s = str(val).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return None


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
            lines.append("| " + " | ".join(str(h) for h in header) + " |")
            lines.append("| " + " | ".join("---" for _ in header) + " |")
            preview = rows[: min(20, len(rows))]
            for r in preview:
                cells = [str(v) if v is not None else "" for v in r]
                if len(cells) < len(header):
                    cells.extend([""] * (len(header) - len(cells)))
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
            if len(rows) > len(preview):
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
