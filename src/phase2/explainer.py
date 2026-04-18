"""
LLM explainer with strict anti-hallucination prompts.

Every prompt template includes HARD RULES that forbid the model from
inventing function names, describing hypothetical flows, or explaining
values that are not in the evidence. After the LLM responds, we run a
sanity check that rejects outputs which contain forbidden patterns and
falls back to a deterministic template.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from src.llm_factory import create_llm
from src.logger import get_logger

logger = get_logger(__name__, concern="app")


# ---------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------

ETL_ROW_PROMPT = """You are explaining why a specific row in an OFSAA database has the value it has.
The row was loaded by an EXTERNAL ETL -- not by any PL/SQL function in this codebase.

HARD RULES -- DO NOT VIOLATE:
1. You must NOT invent function names. There is no "get_initial_balance",
   "calculate_transactions", "compute_end_of_period_balance", or any similar
   fictional function.
2. You must NOT describe hypothetical flows. Forbidden words: "typically",
   "might", "could", "would normally", "in general", "usually".
3. You must ONLY state facts from the EVIDENCE block below.
4. You must state clearly that OFSAA PL/SQL did not compute this value.
5. You must give an actionable fix path pointing to the upstream system.

EVIDENCE (JSON):
{evidence}

Produce a concise explanation (maximum 6 sentences) that:
- States the actual value of the row (use numbers from row_facts).
- States which external system loaded it (use origin_source).
- States explicitly that no PL/SQL function computed the traced variable.
- Lists what PL/SQL did modify (plsql_modifications) if anything.
- Gives the fix path verbatim from the evidence.
- Ends with the verification SQL verbatim inside a ```sql code block.
"""


PLSQL_TRACE_PROMPT = """You are explaining how a value was computed by OFSAA PL/SQL functions.

HARD RULES:
1. You must ONLY reference functions that appear in the EVIDENCE below.
   Do NOT invent function names.
2. You must NOT describe hypothetical flows. Forbidden words: "typically",
   "might", "could", "would normally", "in general".
3. Cite exact line numbers from the evidence.
4. If any step has status "empty" or "error", state that the data is
   unavailable -- do not guess what the value should be.

EVIDENCE (JSON):
{evidence}

Produce a step-by-step explanation showing:
- The actual fact of each step (function, lines, status).
- The transformation the node applied.
- Any override conditions that triggered.
- Source citations with function name and line numbers.
- Ends with the verification SQL verbatim inside a ```sql code block.
"""


MISSING_ROW_PROMPT = """The user asked about a row that does not exist.

HARD RULES:
1. You must NOT explain a value that does not exist.
2. You must NOT invent the row, the value, or the computation.
3. Produce a one-sentence response: state that the row does not exist for
   the given filters, and suggest the user verify the account number and date.

FILTERS PROVIDED (JSON):
{filters}

STATUS: row not found.
"""


UNKNOWN_ORIGIN_PROMPT = """The user asked about a row whose V_DATA_ORIGIN is not in the RTIE catalog.

HARD RULES:
1. You must NOT invent function names.
2. You must NOT describe hypothetical flows. Forbidden words: "typically",
   "might", "could", "in general".
3. Produce at most 4 sentences. State the raw origin value, the row facts,
   and suggest that the engineer extend the catalog or inspect the upstream ETL.

EVIDENCE (JSON):
{evidence}
"""


# ---------------------------------------------------------------------
# Forbidden phrases -- used by sanity_check()
# ---------------------------------------------------------------------

_FORBIDDEN_PHRASES = (
    "typically", "might", "could", "would normally",
    "in general", "usually", "would likely", "probably",
)

_FORBIDDEN_FUNCTION_PATTERNS = (
    "get_initial_balance",
    "calculate_transactions",
    "compute_end_of_period_balance",
)


class Phase2Explainer:
    """Route to a prompt template and run the LLM with sanity checks."""

    def __init__(
        self,
        temperature: float = 0.0,
        max_tokens: int = 1500,
    ) -> None:
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def explain(
        self,
        route: str,
        evidence: dict,
        row: dict | None,
        filters: dict,
        known_functions: set[str] | None = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Produce a natural-language explanation plus sanity metadata.

        Returns
        -------
        dict
            ``text``             the explanation (may be a fallback on failure)
            ``route``            the strategy used
            ``sanity_warnings``  list of warning strings (empty = clean)
            ``used_fallback``    True if the LLM output was rejected
        """
        prompt, max_sentences = self._prompt_for_route(route, evidence, filters)

        try:
            text = await self._invoke_llm(prompt, provider, model)
        except Exception as exc:
            logger.warning("Phase2Explainer LLM invocation failed: %s", exc)
            fallback = self._fallback(route, evidence, filters)
            return {
                "text": fallback,
                "route": route,
                "sanity_warnings": [f"llm_error: {exc}"],
                "used_fallback": True,
            }

        warnings = self.sanity_check(
            text=text,
            route=route,
            evidence=evidence,
            row=row,
            known_functions=known_functions or set(),
        )

        if warnings:
            logger.warning(
                "Phase2Explainer sanity check failed (%d issues); using fallback",
                len(warnings),
            )
            fallback = self._fallback(route, evidence, filters)
            return {
                "text": fallback,
                "route": route,
                "sanity_warnings": warnings,
                "used_fallback": True,
            }

        return {
            "text": text,
            "route": route,
            "sanity_warnings": [],
            "used_fallback": False,
        }

    # -----------------------------------------------------------------
    # Sanity checking
    # -----------------------------------------------------------------

    def sanity_check(
        self,
        text: str,
        route: str,
        evidence: dict,
        row: dict | None,
        known_functions: set[str],
    ) -> list[str]:
        """Validate the LLM output. Returns a list of warnings."""
        warnings: list[str] = []
        lowered = (text or "").lower()

        for phrase in _FORBIDDEN_PHRASES:
            if phrase in lowered:
                warnings.append(f"forbidden_phrase:{phrase}")

        for bad_fn in _FORBIDDEN_FUNCTION_PATTERNS:
            if bad_fn.lower() in lowered:
                warnings.append(f"hallucinated_function:{bad_fn}")

        # Any FN_-prefixed identifier referenced that is NOT in the graph.
        if known_functions:
            referenced = set(re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b", text or ""))
            hallucinated = [
                name for name in referenced
                if name.startswith(("FN_", "POPULATE_", "TLX_", "MAP_",
                                    "MAPPING_"))
                and name not in known_functions
            ]
            for name in hallucinated:
                warnings.append(f"unknown_function:{name}")

        # For missing_row route, the text must not mention a value other
        # than what the user provided.
        if route == "missing_row" and row is None:
            if re.search(r"\b\d{2,}\b", text or ""):
                # Allow years like 2025 -- this is a soft warning we keep
                # but only if a currency/amount-like pattern is present.
                if re.search(r"[-]?\$?\s?\d[\d,]+(\.\d+)?", text or ""):
                    warnings.append("invented_numeric_value")

        # For etl_explain, the text must name the ETL source.
        if route == "etl_explain":
            origin_source = evidence.get("origin_source") or ""
            origin_value = evidence.get("origin") or ""
            if origin_source and origin_source.lower() not in lowered:
                if origin_value and origin_value.lower() not in lowered:
                    warnings.append("etl_source_not_mentioned")

        return warnings

    # -----------------------------------------------------------------
    # Prompt selection & deterministic fallback
    # -----------------------------------------------------------------

    def _prompt_for_route(
        self,
        route: str,
        evidence: dict,
        filters: dict,
    ) -> tuple[str, int]:
        if route == "missing_row":
            return (
                MISSING_ROW_PROMPT.format(filters=json.dumps(_clean(filters), default=str)),
                2,
            )
        if route == "etl_explain":
            return (
                ETL_ROW_PROMPT.format(evidence=json.dumps(evidence, default=str, indent=2)),
                6,
            )
        if route in ("graph_trace", "partial_graph_trace"):
            return (
                PLSQL_TRACE_PROMPT.format(evidence=json.dumps(evidence, default=str, indent=2)),
                12,
            )
        # unknown_origin_diagnose or anything else
        return (
            UNKNOWN_ORIGIN_PROMPT.format(evidence=json.dumps(evidence, default=str, indent=2)),
            4,
        )

    def _fallback(self, route: str, evidence: dict, filters: dict) -> str:
        if route == "missing_row":
            f = {k: v for k, v in filters.items() if v is not None and v != ""}
            parts = ", ".join(f"{k}={v}" for k, v in f.items())
            return (
                f"No row was found for the given filters ({parts}). "
                "Please verify the account number and MIS date."
            )

        if route == "etl_explain":
            origin = evidence.get("origin_source") or evidence.get("origin") or "external ETL"
            fix = evidence.get("fix_path") or "Investigate the upstream ETL pipeline."
            facts = evidence.get("row_facts") or {}
            value_snippet = ""
            for k in ("N_EOP_BAL", "N_ANNUAL_GROSS_INCOME", "N_AMOUNT_LCY"):
                if k in facts:
                    value_snippet = f"{k} = {facts[k]}. "
                    break
            mods = evidence.get("plsql_modifications") or []
            mod_sentence = ""
            if mods:
                names = ", ".join(m.get("column", "?") for m in mods)
                mod_sentence = (
                    f"PL/SQL did modify these columns on the row: {names}. "
                )
            vsql = evidence.get("verification_sql") or ""
            ver = f"\n\n```sql\n{vsql}\n```" if vsql else ""
            return (
                f"This row was loaded by {origin}. "
                f"{value_snippet}"
                f"No PL/SQL function in the indexed codebase computed this value. "
                f"{mod_sentence}"
                f"Fix path: {fix}.{ver}"
            )

        if route in ("graph_trace", "partial_graph_trace"):
            steps = evidence.get("graph_steps") or []
            lines = ["The traced value was computed by the following PL/SQL steps:"]
            for s in steps:
                lines.append(
                    f"- {s.get('function')} ({s.get('node_id')}), lines "
                    f"{s.get('lines')}; status={s.get('status')}"
                )
            vsql = evidence.get("verification_sql") or ""
            if vsql:
                lines.append(f"\n```sql\n{vsql}\n```")
            return "\n".join(lines)

        # unknown
        return (
            f"The row's V_DATA_ORIGIN is {evidence.get('origin')!r}, which is not "
            "in the RTIE catalog. Extend the catalog or inspect the upstream ETL."
        )

    async def _invoke_llm(
        self,
        prompt: str,
        provider: Optional[str],
        model: Optional[str],
    ) -> str:
        llm = create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            json_mode=False,
        )
        system = (
            "You are a careful regulatory-compliance analyst. You only state "
            "facts that appear in the evidence you are given."
        )
        messages = [
            SystemMessage(content=system),
            HumanMessage(content=prompt),
        ]
        response = await llm.ainvoke(messages)
        return (response.content or "").strip()


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != ""}
