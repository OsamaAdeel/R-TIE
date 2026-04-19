"""
Value Tracer Agent -- orchestrates the row-first Phase 2 pipeline.

The pipeline is:

    Stage 1 RowInspector      fetch the actual row first
    Stage 2 OriginClassifier  classify the row's origin
    Stage 3 TraceRouter       pick the right strategy
    Stage 4 EvidenceBuilder   build evidence from data, not assumptions
    Stage 5 Explainer         LLM explains using ONLY verified evidence

Row-first means we never ask the LLM to explain a value until we have
confirmed the row exists and know where it came from. If the row is
missing, we stop immediately and tell the user.
"""

from __future__ import annotations

from typing import Any, Optional

from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id
from src.parsing.query_engine import (
    determine_execution_order,
    fetch_nodes_by_ids,
    fetch_relevant_edges,
    resolve_query_to_nodes,
)
from src.phase2.evidence_builder import EvidenceBuilder
from src.phase2.explainer import Phase2Explainer
from src.phase2.origin_classifier import OriginClassifier
from src.phase2.origins_catalog import get_eop_override, is_gl_blocked
from src.phase2.row_inspector import RowInspector
from src.phase2.trace_router import TraceRouter
from src.phase2.value_fetcher import ValueFetcher

logger = get_logger(__name__, concern="app")


# Target-table selection for common variables. Additional mappings are
# resolved via the Redis graph at query time.
_TARGET_TABLE_BY_COLUMN: dict[str, str] = {
    "N_ANNUAL_GROSS_INCOME": "STG_OPS_RISK_DATA",
    "N_BETA_FACTOR": "STG_OPS_RISK_DATA",
    "N_EOP_BAL": "STG_PRODUCT_PROCESSOR",
    "V_PROD_CODE": "STG_PRODUCT_PROCESSOR",
    "V_ACCOUNT_NUMBER": "STG_PRODUCT_PROCESSOR",
    "N_AMOUNT_LCY": "STG_GL_DATA",
    "N_AMOUNT_ACY": "STG_GL_DATA",
    "V_GL_CODE": "STG_GL_DATA",
}


class ValueTracerAgent:
    """Row-first entry point for Phase 2 queries."""

    def __init__(
        self,
        schema_tools,
        redis_client,
        sql_guardian,
        temperature: float = 0.0,
        max_tokens: int = 2000,
    ) -> None:
        self._schema_tools = schema_tools
        self._redis = redis_client
        self._guardian = sql_guardian
        self._temperature = temperature
        self._max_tokens = max_tokens

        self._row_inspector = RowInspector(schema_tools, sql_guardian)
        self._origin_classifier = OriginClassifier()
        self._trace_router = TraceRouter()
        self._evidence_builder = EvidenceBuilder()
        self._value_fetcher = ValueFetcher(schema_tools, sql_guardian)
        self._explainer = Phase2Explainer(
            temperature=temperature,
            max_tokens=max_tokens,
        )

    # ---------------------------------------------------------------
    # Main entry point -- VALUE_TRACE
    # ---------------------------------------------------------------

    async def trace_value(
        self,
        target_variable: str,
        filters: dict[str, Any],
        schema: str,
        expected_value: float | None = None,
        user_query: str = "",
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Run the row-first pipeline end-to-end.

        Returns a dict with:

        ``status``         "traced" | "row_not_found" | "untraceable_etl" | "oracle_error"
        ``row``            dict or None
        ``origin``         origin classification
        ``route``          strategy name picked by the router
        ``evidence``       evidence fed to the explainer
        ``explanation``    natural-language answer (already sanity-checked)
        ``sanity_warnings`` list[str] -- warnings the explainer raised
        ``used_fallback``  True if the deterministic fallback was used
        ``verification_sql`` ready-to-run SELECT for engineer verification
        """
        correlation_id = get_correlation_id()
        logger.info(
            "value_tracer: target=%s schema=%s filters=%s | correlation_id=%s",
            target_variable, schema, filters, correlation_id,
        )

        # ---- Stage 1: determine target table and fetch the row ----
        target_table = self._determine_target_table(target_variable)
        fetch_result = await self._row_inspector.fetch_target_row(
            target_table=target_table,
            filters=filters,
        )

        if fetch_result["status"] == "oracle_error":
            error = fetch_result.get("error") or "oracle error"
            return {
                "status": "oracle_error",
                "row": None,
                "origin": None,
                "route": "error",
                "evidence": {"oracle_error": error},
                "explanation": f"Database error: {error}",
                "sanity_warnings": [],
                "used_fallback": False,
                "verification_sql": fetch_result.get("query", ""),
            }

        if fetch_result["status"] == "not_found":
            gl_code = filters.get("gl_code") or filters.get("V_GL_CODE")
            eop_override = get_eop_override(gl_code) if gl_code else None
            gl_blocked = is_gl_blocked(gl_code) if gl_code else False
            evidence = self._evidence_builder.build_for_missing_row(
                filters,
                eop_override=eop_override,
                gl_blocked=gl_blocked,
            )
            explainer_result = await self._explainer.explain(
                route="missing_row",
                evidence=evidence,
                row=None,
                filters=filters,
                known_functions=self._known_functions(schema),
                provider=provider,
                model=model,
            )
            return {
                "status": "row_not_found",
                "row": None,
                "origin": None,
                "route": "missing_row",
                "evidence": evidence,
                "explanation": explainer_result["text"],
                "sanity_warnings": explainer_result["sanity_warnings"],
                "used_fallback": explainer_result["used_fallback"],
                "verification_sql": "",
            }

        row = fetch_result["row"]

        # ---- Stage 2: classify origin ----
        classification = self._origin_classifier.classify_row(row)

        # ---- Stage 3: route ----
        route_decision = self._trace_router.route(classification, row, filters)
        strategy = route_decision["strategy"]

        # ---- Stage 4: build evidence ----
        if strategy in ("graph_trace", "partial_graph_trace"):
            graph_path, value_chain = await self._resolve_graph_and_values(
                target_variable=target_variable,
                filters=filters,
                schema=schema,
            )
            evidence = self._evidence_builder.build_for_plsql_trace(
                row=row,
                classification=classification,
                graph_path=graph_path,
                value_chain=value_chain,
            )
        elif strategy == "etl_explain":
            evidence = self._evidence_builder.build_for_etl_origin(
                row=row,
                classification=classification,
            )
        else:
            evidence = self._evidence_builder.build_for_unknown_origin(
                row=row,
                classification=classification,
            )

        # ---- Stage 5: LLM explains ----
        explainer_result = await self._explainer.explain(
            route=strategy,
            evidence=evidence,
            row=row,
            filters=filters,
            known_functions=self._known_functions(schema),
            provider=provider,
            model=model,
        )

        status = "traced" if strategy != "etl_explain" else "untraceable_etl"

        return {
            "status": status,
            "row": row,
            "origin": classification,
            "route": strategy,
            "evidence": evidence,
            "explanation": explainer_result["text"],
            "sanity_warnings": explainer_result["sanity_warnings"],
            "used_fallback": explainer_result["used_fallback"],
            "verification_sql": evidence.get("verification_sql", ""),
        }

    # ---------------------------------------------------------------
    # DIFFERENCE_EXPLANATION / RECONCILIATION -- thin wrappers
    # ---------------------------------------------------------------

    async def explain_difference(
        self,
        target_variable: str,
        filters: dict[str, Any],
        schema: str,
        bank_value: float,
        system_value: float,
        user_query: str = "",
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Handle a DIFFERENCE_EXPLANATION query.

        The row-first pipeline is the right approach here too: before we
        compare claimed values, we must verify the row exists and know
        where it came from.
        """
        result = await self.trace_value(
            target_variable=target_variable,
            filters=filters,
            schema=schema,
            expected_value=bank_value,
            user_query=user_query,
            provider=provider,
            model=model,
        )
        result["query_type"] = "DIFFERENCE_EXPLANATION"
        result["bank_value"] = bank_value
        result["system_value"] = system_value
        return result

    async def reconcile(
        self,
        target_variable: str,
        filters: dict[str, Any],
        schema: str,
        user_query: str = "",
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Handle a RECONCILIATION query via the same row-first path."""
        result = await self.trace_value(
            target_variable=target_variable,
            filters=filters,
            schema=schema,
            expected_value=None,
            user_query=user_query,
            provider=provider,
            model=model,
        )
        result["query_type"] = "RECONCILIATION"
        return result

    # ---------------------------------------------------------------
    # Streaming -- used by /v1/stream in main.py
    # ---------------------------------------------------------------

    async def stream_explanation(
        self,
        user_query: str,
        proof_chain: dict,
        delta_analysis: dict | None,
        missing: dict | None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """Back-compat: token-by-token streaming of an explanation.

        In the row-first design we produce the explanation in
        ``trace_value`` and yield it as a single chunk here. The stream
        endpoint still fires ``event: token`` so the frontend renders
        incrementally.
        """
        text = (proof_chain or {}).get("__explanation__") or ""
        if text:
            yield text

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _determine_target_table(self, target_variable: str) -> str:
        """Pick the table that holds *target_variable*.

        Uses a small lookup for common columns and falls back to
        ``STG_PRODUCT_PROCESSOR`` (the most common staging table) so
        the pipeline never crashes on unknown variables.
        """
        key = (target_variable or "").strip().upper()
        return _TARGET_TABLE_BY_COLUMN.get(key, "STG_PRODUCT_PROCESSOR")

    async def _resolve_graph_and_values(
        self,
        target_variable: str,
        filters: dict[str, Any],
        schema: str,
    ) -> tuple[list[dict], list[dict]]:
        """Resolve the graph path and fetch actual values at each node."""
        node_ids = resolve_query_to_nodes(
            query_type="variable",
            target_variable=target_variable,
            function_name="",
            table_name="",
            schema=schema,
            redis_client=self._redis,
        )
        if not node_ids:
            return [], []

        nodes = fetch_nodes_by_ids(
            node_ids, schema, self._redis, include_upstream=True
        )
        edges = fetch_relevant_edges(node_ids, schema, self._redis)
        ordered = determine_execution_order(nodes, edges)
        main_path = [e for e in ordered if not e.get("is_upstream")]
        if not main_path:
            main_path = ordered

        value_chain = await self._value_fetcher.fetch_value_chain(
            graph_path=main_path,
            filters=filters,
            target_column=target_variable,
        )
        return main_path, value_chain

    def _known_functions(self, schema: str) -> set[str]:
        """Return the set of function names that exist in the graph.

        Used by the explainer's sanity check to flag hallucinated
        function names before they reach the user.
        """
        try:
            keys = self._redis.keys(f"graph:{schema}:*")
        except Exception as exc:
            logger.warning("Could not enumerate graph keys for sanity check: %s", exc)
            return set()
        names: set[str] = set()
        prefix = f"graph:{schema}:".encode()
        for k in keys:
            if isinstance(k, bytes):
                if k.startswith(prefix):
                    names.add(k[len(prefix):].decode("utf-8", errors="ignore"))
            else:
                s = str(k)
                marker = f"graph:{schema}:"
                if marker in s:
                    names.add(s.split(marker, 1)[1])
        return {n for n in names if n and "meta" not in n and "source" not in n}
