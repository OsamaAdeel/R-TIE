"""
Value Tracer Agent -- orchestrates Phase 2 components.

Handles VALUE_TRACE, DIFFERENCE_EXPLANATION, and RECONCILIATION queries
by combining the Phase 1 graph pipeline with the Phase 2 data layer.
"""

from __future__ import annotations

from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from src.llm_factory import create_llm
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id
from src.parsing.query_engine import (
    determine_execution_order,
    fetch_nodes_by_ids,
    fetch_relevant_edges,
    resolve_query_to_nodes,
)
from src.phase2.difference_detector import DifferenceDetector
from src.phase2.proof_builder import ProofBuilder
from src.phase2.value_fetcher import ValueFetcher
from src.phase2.verification_sql import VerificationSQLGenerator

logger = get_logger(__name__, concern="app")


_VALUE_TRACE_SYSTEM_PROMPT = """You are an expert in Oracle OFSAA FSAPPS regulatory capital calculations.
You are given a proof chain showing how a specific value was computed across several PL/SQL functions,
together with actual values observed at each step.

Explain the computation in plain English for a risk analyst:
- Start with the origin value and where it came from.
- Walk through each step describing what changed and why (business meaning, not SQL syntax).
- If a delta was detected, explain which step caused it and why (override, condition, missing data).
- Include the final value and whether it matched expectations.
- Cite function names and line numbers for every claim.
- Use markdown. Do NOT invent values that are not in the proof chain.
"""


class ValueTracerAgent:
    """Entry point for Phase 2 data-trace queries."""

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
        self._fetcher = ValueFetcher(schema_tools, sql_guardian)
        self._proof_builder = ProofBuilder()
        self._diff_detector = DifferenceDetector()
        self._sql_generator = VerificationSQLGenerator()

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
        """Handle a VALUE_TRACE query end-to-end.

        Parameters
        ----------
        target_variable
            Column or variable name to trace.
        filters
            Dict with keys such as ``mis_date``, ``account_number``,
            ``lob_code``, ``lv_code``, ``gl_code``, ``branch_code``.
        schema
            Oracle schema used as the namespace for Redis graph lookups.
        expected_value
            If provided, DifferenceDetector runs against the actual
            final value and populates ``delta_analysis``.
        user_query
            Raw user question (for the LLM prompt).
        """
        correlation_id = get_correlation_id()
        logger.info(
            "value_tracer: target=%s schema=%s filters=%s | correlation_id=%s",
            target_variable, schema, filters, correlation_id,
        )

        node_ids = resolve_query_to_nodes(
            query_type="variable",
            target_variable=target_variable,
            function_name="",
            table_name="",
            schema=schema,
            redis_client=self._redis,
        )

        if not node_ids:
            return self._empty_result(
                target_variable=target_variable,
                filters=filters,
                reason=(
                    f"No graph nodes found for variable {target_variable!r}. "
                    "Either the column name does not exist in the indexed graph, "
                    "or the function has not been indexed yet."
                ),
            )

        nodes = fetch_nodes_by_ids(node_ids, schema, self._redis, include_upstream=True)
        edges = fetch_relevant_edges(node_ids, schema, self._redis)
        ordered = determine_execution_order(nodes, edges)
        main_path = [e for e in ordered if not e.get("is_upstream")]
        if not main_path:
            main_path = ordered

        value_chain = await self._fetcher.fetch_value_chain(
            graph_path=main_path,
            filters=filters,
            target_column=target_variable,
        )

        proof_chain = self._proof_builder.build_proof_chain(
            graph_path=main_path,
            value_chain=value_chain,
            target_variable=target_variable,
        )

        missing = self._fetcher.detect_upstream_missing(value_chain)

        delta_analysis: dict | None = None
        if expected_value is not None:
            actual = proof_chain.get("final_value")
            if actual is not None:
                delta_analysis = self._diff_detector.detect_delta_source(
                    proof_chain=proof_chain,
                    expected_value=expected_value,
                    actual_value=actual,
                )
            else:
                delta_analysis = {
                    "delta": None,
                    "delta_percent": None,
                    "root_cause_step": None,
                    "cause_type": "MISSING_DATA",
                    "explanation": "No final value could be fetched -- cannot compute delta.",
                }

        verification_script = self._sql_generator.generate_full_verification_script(
            proof_chain=proof_chain,
            filters=filters,
        )

        explanation = await self._llm_explain(
            user_query=user_query or f"How is {target_variable} calculated?",
            proof_chain=proof_chain,
            delta_analysis=delta_analysis,
            missing=missing,
            provider=provider,
            model=model,
        )

        data_completeness = {
            str(entry.get("node", {}).get("id")): (entry.get("value_result") or {}).get("status", "unknown")
            for entry in value_chain
        }

        return {
            "query_type": "VALUE_TRACE",
            "target_variable": target_variable,
            "filters": filters,
            "proof_chain": proof_chain,
            "delta_analysis": delta_analysis,
            "verification_sql": verification_script,
            "explanation": explanation,
            "confidence": proof_chain.get("confidence", 0.0),
            "data_completeness": data_completeness,
            "missing_upstream": missing,
        }

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

        The system's computed value is treated as the expected, and the
        bank-reported value is treated as the actual. The DifferenceDetector
        localises where the computed chain would produce the bank value.
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

        if result.get("delta_analysis") is None and system_value is not None:
            result["delta_analysis"] = self._diff_detector.detect_delta_source(
                proof_chain=result["proof_chain"],
                expected_value=bank_value,
                actual_value=system_value,
            )
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
        """Handle a RECONCILIATION query.

        Implemented as a VALUE_TRACE with no expected value -- the proof
        chain itself reveals where values diverge across tables.
        """
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

    async def _llm_explain(
        self,
        user_query: str,
        proof_chain: dict,
        delta_analysis: dict | None,
        missing: dict | None,
        provider: Optional[str],
        model: Optional[str],
    ) -> str:
        """Ask the LLM to turn the structured proof chain into plain English.

        The LLM never generates SQL. It only narrates the already-computed
        proof structure.
        """
        payload = self._render_payload(proof_chain, delta_analysis, missing)
        llm = create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            json_mode=False,
        )
        messages = [
            SystemMessage(content=_VALUE_TRACE_SYSTEM_PROMPT),
            HumanMessage(content=f"User Question: {user_query}\n\n{payload}"),
        ]
        try:
            response = await llm.ainvoke(messages)
            return (response.content or "").strip()
        except Exception as exc:
            logger.warning("value_tracer LLM explanation failed: %s", exc)
            return _fallback_text(proof_chain, delta_analysis, missing)

    async def stream_explanation(
        self,
        user_query: str,
        proof_chain: dict,
        delta_analysis: dict | None,
        missing: dict | None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """Stream the LLM explanation as an async generator of tokens."""
        payload = self._render_payload(proof_chain, delta_analysis, missing)
        llm = create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            json_mode=False,
        )
        messages = [
            SystemMessage(content=_VALUE_TRACE_SYSTEM_PROMPT),
            HumanMessage(content=f"User Question: {user_query}\n\n{payload}"),
        ]
        async for chunk in llm.astream(messages):
            if chunk.content:
                yield chunk.content

    def _render_payload(
        self,
        proof_chain: dict,
        delta_analysis: dict | None,
        missing: dict | None,
    ) -> str:
        lines: list[str] = []
        lines.append(f"Target variable: {proof_chain.get('target_variable')}")
        lines.append(f"Origin value:    {proof_chain.get('origin_value')}")
        lines.append(f"Final value:     {proof_chain.get('final_value')}")
        td = proof_chain.get("total_delta")
        if td is not None:
            lines.append(f"Total delta:     {td:+.4f}")
        lines.append("")

        if missing:
            lines.append("MISSING UPSTREAM:")
            lines.append(f"  node:     {missing.get('node', {}).get('id')}")
            lines.append(f"  reason:   {missing.get('reason')}")
            lines.append(f"  message:  {missing.get('message')}")
            lines.append("")

        for step in proof_chain.get("steps") or []:
            lines.append(f"--- STEP {step['step_number']}: {step.get('function')} ---")
            lines.append(f"Operation: {step.get('operation')}")
            lines.append(f"Source:    {step.get('source_ref')}")
            iv = step.get("input_value")
            ov = step.get("output_value")
            lines.append(f"Input  -> Output:  {iv} -> {ov}")
            formula = step.get("formula")
            if formula:
                lines.append(f"Formula: {formula[:200]}")
            conds = step.get("conditions_met") or []
            if conds:
                lines.append(f"Conditions: {'; '.join(c[:100] for c in conds[:3])}")
            overrides = step.get("overrides_triggered") or []
            if overrides:
                lines.append(f"Overrides triggered: {overrides}")
            note = step.get("notes")
            if note:
                lines.append(f"Note: {note}")
            lines.append("")

        if delta_analysis:
            lines.append("DELTA ANALYSIS:")
            lines.append(f"  delta:            {delta_analysis.get('delta')}")
            lines.append(f"  cause_type:       {delta_analysis.get('cause_type')}")
            lines.append(f"  root_cause_step:  {delta_analysis.get('root_cause_step')}")
            lines.append(f"  explanation:      {delta_analysis.get('explanation')}")

        return "\n".join(lines)

    def _empty_result(
        self,
        target_variable: str,
        filters: dict[str, Any],
        reason: str,
    ) -> dict:
        return {
            "query_type": "VALUE_TRACE",
            "target_variable": target_variable,
            "filters": filters,
            "proof_chain": {
                "target_variable": target_variable,
                "steps": [],
                "final_value": None,
                "origin_value": None,
                "total_delta": None,
                "summary": reason,
                "confidence": 0.0,
            },
            "delta_analysis": None,
            "verification_sql": "",
            "explanation": reason,
            "confidence": 0.0,
            "data_completeness": {},
            "missing_upstream": None,
        }


def _fallback_text(
    proof_chain: dict,
    delta_analysis: dict | None,
    missing: dict | None,
) -> str:
    """Plain-text explanation when the LLM call fails."""
    lines: list[str] = []
    lines.append(f"## Value trace for {proof_chain.get('target_variable')}")
    lines.append("")
    lines.append(proof_chain.get("summary", ""))
    if missing:
        lines.append("")
        lines.append(f"**Missing upstream data:** {missing.get('message')}")
    for step in proof_chain.get("steps") or []:
        lines.append("")
        lines.append(f"**Step {step['step_number']} ({step.get('function')}):** {step.get('transformation')}")
        lines.append(f"- input={step.get('input_value')}  output={step.get('output_value')}")
        lines.append(f"- source: {step.get('source_ref')}")
    if delta_analysis and delta_analysis.get("cause_type") != "NONE":
        lines.append("")
        lines.append(f"**Delta detected:** {delta_analysis.get('explanation')}")
    return "\n".join(lines)
