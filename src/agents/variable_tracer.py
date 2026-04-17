"""
RTIE Variable Tracer Module.

Traces how a specific variable (e.g. EAD_AMOUNT) is calculated, transformed,
and propagated across multiple PL/SQL functions.

Pipeline (3 stages):
    1. LLM Variable Resolver — tiny prompt (~500 chars) maps the user's
       business concept (e.g. "EAD_AMOUNT") to actual code variable names
       (e.g. "LN_EXP_AMOUNT", "N_AMOUNT_LCY1") by scanning declarations.
    2. Pure Python Extraction — builds alias map, extracts only relevant
       lines, tags each with function name + line number + operation type.
    3. LLM Explanation — sends only the compact chain (~60-80 lines) to
       gpt-4o for a structured explanation. NOT the full 5000+ line source.
"""

import json
import re
from typing import Any, Dict, List, Optional, Set

from langchain_core.messages import SystemMessage, HumanMessage

from src.pipeline.state import LogicState
from src.llm_factory import create_llm
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="app")


# ──────────────────────────────────────────────────────────────
# LLM PROMPTS
# ──────────────────────────────────────────────────────────────

VARIABLE_RESOLVER_PROMPT = """\
You are a PL/SQL variable name resolver for Oracle OFSAA regulatory systems.

The user is asking about a variable/column using a BUSINESS name that may not
match the actual code variable names. Your job is to identify which variables
in the source code correspond to the user's concept.

You will receive:
1. The user's target variable name (a business concept).
2. A list of all declared variables and column names found in the source code.

Return ONLY a JSON array of the actual code variable names that correspond to
or are related to the user's concept. Include:
- Exact matches (if any)
- Variables that clearly represent the same concept under a different naming convention
  (e.g. "EAD_AMOUNT" → "LN_EXP_AMOUNT" because EXP = Exposure = EAD)
- Column names in tables that store or compute this value
- Variables that feed into or derive from the target

Common OFSAA naming patterns:
- LN_ = local number variable
- LV_ = local varchar variable
- N_ = number column/parameter
- V_ = varchar column/parameter
- F_ = flag column
- FN_ = function name prefix

Do NOT include variables that are unrelated just because they share a common
word like "AMOUNT" with something completely different.

Respond with ONLY a valid JSON object — no markdown, no extra text:
{
  "resolved_variables": ["VAR1", "VAR2"],
  "reasoning": "brief explanation of why these variables match"
}
"""


VARIABLE_TRACE_PROMPT = """\
You are an expert in Oracle OFSAA FSAPPS regulatory capital calculations.

You will receive a **variable transformation chain** — a compact extract showing
every line across multiple PL/SQL functions where a specific target variable
(or any of its aliases) is read, written, or transformed.

Your task is to produce a **rich, detailed markdown explanation** of the complete
calculation lifecycle of the target variable, focused on BUSINESS MEANING and DATA FLOW — not syntax.

RULES:
1. Never explain what SQL syntax does (do not explain NVL, CASE, TO_NUMBER, DECODE).
   Instead explain what the VALUE represents and why it changes.

2. For every step, answer these questions:
   - What is the value at this point?
   - Where did it come from (which table, which column)?
   - Why is it being changed?
   - What does the result mean in business terms?

3. For intermediate variables (local PL/SQL variables like TOT1, CBA_DEDUCTION):
   - Explain the formula in plain English
   - Name the source tables and what data they contribute
   - Show the arithmetic clearly: e.g. "DBS GL balance × deduction ratio"

4. Always include execution conditions prominently:
   "This entire function ONLY runs when the reporting month is December."
   Never bury this at the end — state it first for the function.

5. For steps where a value is copied unchanged between tables:
   State clearly: "The value is passed through without modification."

6. Cite every claim with function name and line numbers.

7. End with a SHORT SUMMARY (4 sentences max) that states:
   - Where the value originates
   - What transforms it
   - What the final value represents
   - Any important conditions (e.g. December-only)

FORMAT:
- Start with: ## {VARIABLE_NAME} in `FUNCTION_NAME` (SCHEMA)
- Use ### for each function/step
- Include ```sql code blocks with the relevant PL/SQL
- Put line references in section headers: ### Step 1: Initial Insert (Lines 203-223)
- Do NOT repeat line references separately below code blocks
- Show the data flow: origin → transformations → destination
"""


# ──────────────────────────────────────────────────────────────
# PL/SQL keywords to ignore when scanning for variables
# ──────────────────────────────────────────────────────────────

_PLSQL_NOISE = {
    "IF", "ELSIF", "ELSE", "THEN", "END", "BEGIN", "DECLARE",
    "EXCEPTION", "WHEN", "LOOP", "FOR", "WHILE", "CASE",
    "RETURN", "IS", "AS", "NOT", "AND", "OR", "NULL",
    "TRUE", "FALSE", "IN", "OUT", "NUMBER", "VARCHAR2",
    "DATE", "BOOLEAN", "INTEGER", "PLS_INTEGER", "CLOB",
    "BLOB", "CURSOR", "OPEN", "CLOSE", "FETCH", "EXIT",
    "COMMIT", "ROLLBACK", "SAVEPOINT", "PRAGMA", "RAISE",
    "GOTO", "RESULT", "TYPE", "RECORD", "TABLE", "INDEX",
    "BULK", "COLLECT", "LIMIT", "FORALL", "SELECT", "FROM",
    "WHERE", "INSERT", "UPDATE", "DELETE", "MERGE", "INTO",
    "SET", "VALUES", "CREATE", "REPLACE", "FUNCTION",
    "PROCEDURE", "PACKAGE", "BODY", "USING", "MATCHED",
    "GROUP", "ORDER", "HAVING", "BETWEEN", "LIKE", "EXISTS",
    "DISTINCT", "INNER", "OUTER", "LEFT", "RIGHT", "JOIN",
    "SUBSTR", "INSTR", "LENGTH", "TRIM", "UPPER", "LOWER",
    "NVL", "NVL2", "DECODE", "COALESCE", "ROUND", "TRUNC",
    "TO_CHAR", "TO_DATE", "TO_NUMBER", "COUNT", "SUM",
    "AVG", "MIN", "MAX", "DBMS_OUTPUT", "PUT_LINE",
}


class VariableTracer:
    """Traces a target variable across multiple PL/SQL functions.

    Uses a two-LLM-call strategy:
    1. Lightweight variable resolver (~500 chars) maps user's business
       concept to actual code variable names.
    2. After pure Python extraction, sends compact chain (~60-80 lines)
       to gpt-4o for structured explanation.
    """

    def __init__(
        self,
        temperature: float = 0,
        max_tokens: int = 3000,
    ) -> None:
        self._temperature = temperature
        self._max_tokens = max_tokens

    # ──────────────────────────────────────────────────────────
    # 0. LLM VARIABLE RESOLVER (tiny payload, bridges naming gap)
    # ──────────────────────────────────────────────────────────

    async def resolve_variable_names(
        self,
        target_variable: str,
        functions_source: Dict[str, List[Dict[str, Any]]],
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[str]:
        """Ask the LLM to map a business concept to actual code variable names.

        Extracts all declared variable/column names from the source (pure Python),
        then sends a tiny prompt to the LLM asking which ones correspond to the
        user's target. This bridges the gap between "EAD_AMOUNT" (business name)
        and "LN_EXP_AMOUNT" (code name).

        Args:
            target_variable: The user's variable name (business concept).
            functions_source: Dict of function_name → source lines.
            provider: LLM provider.
            model: LLM model.

        Returns:
            List of actual code variable names that match the concept.
        """
        correlation_id = get_correlation_id()

        # Extract all unique identifiers from source (pure Python)
        all_vars = self._extract_all_identifiers(functions_source)

        if not all_vars:
            return [target_variable.upper()]

        llm = create_llm(
            provider=provider or "openai",
            model=model or "gpt-4o",
            temperature=0,
            max_tokens=500,
            json_mode=(provider or "openai") != "anthropic",
        )

        user_prompt = (
            f"Target variable (user's business concept): {target_variable}\n\n"
            f"All declared variables and column names found in the source code:\n"
            f"{', '.join(sorted(all_vars))}\n\n"
            f"Which of these variables correspond to or are related to "
            f"'{target_variable}'? Include variables that store, compute, "
            f"or derive this value."
        )

        messages = [
            SystemMessage(content=VARIABLE_RESOLVER_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        logger.info(
            f"Resolving variable names for '{target_variable}' "
            f"({len(all_vars)} identifiers in source) | "
            f"correlation_id={correlation_id}"
        )

        response = await llm.ainvoke(messages)
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        try:
            parsed = json.loads(raw)
            resolved = parsed.get("resolved_variables", [])
            reasoning = parsed.get("reasoning", "")
            logger.info(
                f"Variable resolver: '{target_variable}' → {resolved} "
                f"(reason: {reasoning}) | correlation_id={correlation_id}"
            )
            return [v.upper() for v in resolved] if resolved else [target_variable.upper()]
        except (json.JSONDecodeError, KeyError):
            logger.warning(f"Variable resolver returned non-JSON, using target as-is")
            return [target_variable.upper()]

    def _extract_all_identifiers(
        self,
        functions_source: Dict[str, List[Dict[str, Any]]],
    ) -> Set[str]:
        """Extract all unique variable/column identifiers from source code.

        Focuses on declarations (VAR_NAME  TYPE), assignments (VAR := ...),
        and column references in SQL statements.

        Args:
            functions_source: Dict of function_name → source lines.

        Returns:
            Set of unique identifier names.
        """
        ident_pattern = re.compile(r'\b([A-Z_][A-Z0-9_]{2,})\b')
        identifiers: Set[str] = set()

        for source_lines in functions_source.values():
            for line_info in source_lines:
                text_upper = line_info.get("text", "").upper()
                for match in ident_pattern.findall(text_upper):
                    if match not in _PLSQL_NOISE and len(match) <= 50:
                        identifiers.add(match)

        return identifiers

    # ──────────────────────────────────────────────────────────
    # 1. ALIAS MAP BUILDER
    # ──────────────────────────────────────────────────────────

    def build_alias_map(
        self,
        seed_variables: List[str],
        functions_source: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Set[str]]:
        """Build a map of all aliases starting from LLM-resolved seed variables.

        Takes the seed variables (from the resolver LLM) and expands them
        transitively via assignment patterns in the PL/SQL source.

        Args:
            seed_variables: List of actual code variable names from the resolver.
            functions_source: Dict mapping function_name → source lines.

        Returns:
            Dict mapping function_name → set of alias names found.
        """
        global_aliases: Set[str] = set(v.upper() for v in seed_variables)
        per_function: Dict[str, Set[str]] = {}

        logger.info(f"Building alias map from seeds: {global_aliases}")

        # Multi-pass: expand via assignment patterns
        for pass_num in range(5):
            prev_size = len(global_aliases)

            for fn_name, source_lines in functions_source.items():
                if fn_name not in per_function:
                    per_function[fn_name] = set()

                for line_info in source_lines:
                    text = line_info.get("text", "")
                    text_upper = text.upper()

                    if not any(a in text_upper for a in global_aliases):
                        continue

                    new_aliases = self._extract_aliases_from_line(
                        text_upper, global_aliases
                    )
                    for alias in new_aliases:
                        if alias not in global_aliases and alias not in _PLSQL_NOISE:
                            global_aliases.add(alias)
                            per_function[fn_name].add(alias)

                # Tag functions that reference any alias
                for line_info in source_lines:
                    text_upper = line_info.get("text", "").upper()
                    for alias in global_aliases:
                        if re.search(r'\b' + re.escape(alias) + r'\b', text_upper):
                            per_function[fn_name].add(alias)
                            break

            if len(global_aliases) == prev_size:
                break

        logger.info(
            f"Alias map: {len(global_aliases)} aliases "
            f"across {sum(1 for v in per_function.values() if v)} functions "
            f"(passes={pass_num + 1})"
        )
        return per_function

    def _extract_aliases_from_line(
        self, text_upper: str, known_aliases: Set[str]
    ) -> List[str]:
        """Extract new variable aliases from a single PL/SQL line.

        Recognizes patterns:
        - Direct assignment:  NEW_VAR := <expr containing ALIAS>
        - SELECT INTO:        SELECT ... INTO NEW_VAR ... (where alias in SELECT)
        - SET clause:         SET NEW_COL = ALIAS

        Args:
            text_upper: Uppercased line text.
            known_aliases: Current set of known aliases.

        Returns:
            List of newly discovered alias names.
        """
        new_aliases = []

        for alias in known_aliases:
            # Pattern: LHS := <expr with alias>
            match = re.match(
                r'\s*([A-Z_][A-Z0-9_]*)\s*:=\s*.*\b' + re.escape(alias) + r'\b',
                text_upper,
            )
            if match:
                lhs = match.group(1)
                if lhs not in known_aliases and lhs not in _PLSQL_NOISE:
                    new_aliases.append(lhs)

            # Pattern: SELECT ... alias ... INTO new_var
            if "INTO" in text_upper and alias in text_upper:
                into_match = re.search(r'\bINTO\s+([A-Z_][A-Z0-9_]*)', text_upper)
                if into_match:
                    into_var = into_match.group(1)
                    if into_var not in known_aliases and into_var not in _PLSQL_NOISE:
                        new_aliases.append(into_var)

            # Pattern: SET col = alias (in UPDATE/MERGE)
            set_match = re.search(
                r'\bSET\s+\w*\.?([A-Z_][A-Z0-9_]*)\s*=\s*.*\b' + re.escape(alias) + r'\b',
                text_upper,
            )
            if set_match:
                set_col = set_match.group(1)
                if set_col not in known_aliases and set_col not in _PLSQL_NOISE:
                    new_aliases.append(set_col)

        return new_aliases

    # ──────────────────────────────────────────────────────────
    # 2. RELEVANT LINE EXTRACTOR
    # ──────────────────────────────────────────────────────────

    def extract_relevant_lines(
        self,
        target_variable: str,
        functions_source: Dict[str, List[Dict[str, Any]]],
        alias_map: Dict[str, Set[str]],
        seed_variables: List[str],
    ) -> List[Dict[str, Any]]:
        """Extract only lines that reference the target variable or its aliases.

        Args:
            target_variable: The original user-facing variable name.
            functions_source: Dict of function_name → source lines.
            alias_map: Per-function alias sets from build_alias_map().
            seed_variables: The LLM-resolved variable names.

        Returns:
            List of tagged line dicts, sorted by function then line number.
        """
        all_aliases: Set[str] = set(v.upper() for v in seed_variables)
        all_aliases.add(target_variable.upper())
        for fn_aliases in alias_map.values():
            all_aliases.update(fn_aliases)

        tagged_lines: List[Dict[str, Any]] = []

        for fn_name, source_lines in functions_source.items():
            for line_info in source_lines:
                text = line_info.get("text", "")
                stripped = text.strip()

                # Skip commented-out lines — they are not active code
                is_commented = stripped.startswith("--") or stripped.startswith("/*")
                text_upper = text.upper()

                matched = [
                    a for a in all_aliases
                    if re.search(r'\b' + re.escape(a) + r'\b', text_upper)
                ]
                if not matched:
                    continue

                operation = self._classify_operation(text_upper, matched)
                if is_commented:
                    operation = "COMMENTED_OUT"

                tagged_lines.append({
                    "function": fn_name,
                    "line": line_info.get("line", 0),
                    "text": stripped,
                    "aliases_matched": matched,
                    "operation": operation,
                    "commented": is_commented,
                })

        tagged_lines.sort(key=lambda x: (x["function"], x["line"]))

        logger.info(
            f"Extracted {len(tagged_lines)} relevant lines for '{target_variable}' "
            f"from {len(functions_source)} functions"
        )
        return tagged_lines

    def _classify_operation(self, text_upper: str, matched_aliases: List[str]) -> str:
        """Classify what operation a line performs on the variable."""
        for alias in matched_aliases:
            if re.match(r'\s*' + re.escape(alias) + r'\s*:=', text_upper):
                return "ASSIGN"

        if "SELECT" in text_upper and "INTO" in text_upper:
            return "SELECT_INTO"
        if "INSERT" in text_upper:
            return "INSERT"
        if "UPDATE" in text_upper or ("SET" in text_upper and "=" in text_upper):
            return "UPDATE"
        if "MERGE" in text_upper:
            return "MERGE"

        for alias in matched_aliases:
            if re.search(re.escape(alias) + r'\s+(IN|OUT)\b', text_upper):
                return "PARAMETER"

        if ":=" in text_upper:
            return "TRANSFORM"
        if "WHERE" in text_upper:
            return "FILTER"

        return "READ"

    # ──────────────────────────────────────────────────────────
    # 3. TRANSFORMATION CHAIN BUILDER
    # ──────────────────────────────────────────────────────────

    def build_transformation_chain(
        self,
        target_variable: str,
        tagged_lines: List[Dict[str, Any]],
        seed_variables: List[str],
    ) -> str:
        """Build a compact, human-readable transformation chain."""
        if not tagged_lines:
            return f"No lines found referencing '{target_variable}' or its aliases."

        all_aliases: Set[str] = set()
        for line in tagged_lines:
            all_aliases.update(line["aliases_matched"])

        by_function: Dict[str, List[Dict[str, Any]]] = {}
        for line in tagged_lines:
            fn = line["function"]
            by_function.setdefault(fn, []).append(line)

        parts = []
        active_lines = [l for l in tagged_lines if not l.get("commented")]
        commented_lines = [l for l in tagged_lines if l.get("commented")]

        parts.append(f"TARGET VARIABLE: {target_variable}")
        parts.append(f"RESOLVED CODE VARIABLES: {', '.join(seed_variables)}")
        parts.append(f"ALL ALIASES (including transitive): {', '.join(sorted(all_aliases))}")
        parts.append(f"FUNCTIONS INVOLVED: {', '.join(sorted(by_function.keys()))}")
        parts.append(f"ACTIVE LINES: {len(active_lines)}")
        if commented_lines:
            parts.append(f"COMMENTED-OUT LINES: {len(commented_lines)} (deprecated — do NOT treat as active logic)")
        parts.append("")

        for fn_name, lines in sorted(by_function.items()):
            active = [l for l in lines if not l.get("commented")]
            commented = [l for l in lines if l.get("commented")]

            if active:
                parts.append(f"=== {fn_name} ({len(active)} active lines) ===")
                for line in active:
                    aliases_str = ",".join(line["aliases_matched"])
                    parts.append(
                        f"  L{line['line']:>4} [{line['operation']:<12}] "
                        f"({aliases_str}) | {line['text']}"
                    )
                parts.append("")

            if commented:
                parts.append(f"=== {fn_name} — COMMENTED OUT (deprecated, not executed) ===")
                for line in commented:
                    aliases_str = ",".join(line["aliases_matched"])
                    parts.append(
                        f"  L{line['line']:>4} [COMMENTED_OUT ] "
                        f"({aliases_str}) | {line['text']}"
                    )
                parts.append("")

        chain_text = "\n".join(parts)
        logger.info(
            f"Transformation chain: {len(tagged_lines)} lines, "
            f"{len(by_function)} functions, {len(chain_text)} chars"
        )
        return chain_text

    # ──────────────────────────────────────────────────────────
    # 4. LLM EXPLANATION (second LLM call — compact chain only)
    # ──────────────────────────────────────────────────────────

    async def explain_chain(
        self,
        target_variable: str,
        chain_text: str,
        user_query: str,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send the compact transformation chain to the LLM for explanation."""
        correlation_id = get_correlation_id()

        # Use non-JSON mode for markdown responses
        llm = create_llm(
            provider=provider or "openai",
            model=model or "gpt-4o-mini",
            temperature=self._temperature,
            max_tokens=4096,
            json_mode=False,
        )

        user_prompt = (
            f"User Question: {user_query}\n\n"
            f"Trace the calculation lifecycle of variable '{target_variable}' "
            f"using the following transformation chain:\n\n"
            f"{chain_text}\n\n"
            f"Produce a detailed markdown explanation of how '{target_variable}' "
            f"is calculated, transformed, and propagated. "
            f"Cite every function name and line number."
        )

        messages = [
            SystemMessage(content=VARIABLE_TRACE_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        logger.info(
            f"Sending variable trace to LLM: var={target_variable}, "
            f"chain_chars={len(chain_text)}, provider={provider or 'openai'} | "
            f"correlation_id={correlation_id}"
        )

        response = await llm.ainvoke(messages)
        markdown_content = response.content.strip()

        logger.info(
            f"Variable trace explanation received: "
            f"{len(markdown_content)} chars markdown | "
            f"correlation_id={correlation_id}"
        )
        return {
            "markdown": markdown_content,
            "summary": markdown_content[:200] + "..." if len(markdown_content) > 200 else markdown_content,
        }

    async def stream_chain(
        self,
        target_variable: str,
        chain_text: str,
        user_query: str,
        provider: str | None = None,
        model: str | None = None,
    ):
        """Stream variable trace explanation tokens as an async generator.

        Yields markdown chunks for SSE streaming.
        """
        llm = create_llm(
            provider=provider or "openai",
            model=model or "gpt-4o-mini",
            temperature=self._temperature,
            max_tokens=4096,
            json_mode=False,
        )

        user_prompt = (
            f"User Question: {user_query}\n\n"
            f"Trace the calculation lifecycle of variable '{target_variable}' "
            f"using the following transformation chain:\n\n"
            f"{chain_text}\n\n"
            f"Produce a detailed markdown explanation of how '{target_variable}' "
            f"is calculated, transformed, and propagated. "
            f"Cite every function name and line number."
        )

        messages = [
            SystemMessage(content=VARIABLE_TRACE_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        async for chunk in llm.astream(messages):
            if chunk.content:
                yield chunk.content

    # ──────────────────────────────────────────────────────────
    # 5. FULL PIPELINE — called from the graph node
    # ──────────────────────────────────────────────────────────

    async def trace_variable(
        self,
        state: LogicState,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LogicState:
        """Run the full variable tracing pipeline.

        Stage 1: LLM resolves user's business concept → actual code var names
        Stage 2: Pure Python extracts only relevant lines (~60-80 from 5000+)
        Stage 3: LLM explains the compact chain

        Args:
            state: Pipeline state with raw_query, multi_source populated.
            provider: LLM provider.
            model: Model name.

        Returns:
            Updated state with explanation and variable_chain populated.
        """
        correlation_id = get_correlation_id()
        query = state["raw_query"]
        multi_source = state.get("multi_source", {})

        # Use target_variable from orchestrator if available, else extract from query
        target_variable = state.get("target_variable", "").strip()
        if not target_variable:
            target_variable = self._extract_target_variable(query)
        if not target_variable:
            logger.warning(
                f"Could not extract target variable from query: {query[:80]} | "
                f"correlation_id={correlation_id}"
            )
            state["explanation"] = {
                "summary": "Could not identify a target variable to trace in the query.",
                "step_by_step": [],
                "formulas": [],
                "dependencies_used": [],
                "regulatory_refs": [],
                "raw_source_references": [],
                "unclear_items": ["No target variable could be extracted."],
            }
            return state

        # Build functions_source from multi_source
        functions_source: Dict[str, List[Dict[str, Any]]] = {}
        for fn_name, fn_data in multi_source.items():
            source_lines = fn_data.get("source_code", [])
            if source_lines:
                functions_source[fn_name] = source_lines

        if not functions_source:
            state["explanation"] = {
                "summary": f"No source code available to trace '{target_variable}'.",
                "step_by_step": [],
                "formulas": [],
                "dependencies_used": [],
                "regulatory_refs": [],
                "raw_source_references": [],
                "unclear_items": ["No function source code was available."],
            }
            return state

        logger.info(
            f"Variable trace: target='{target_variable}', "
            f"functions={list(functions_source.keys())} | "
            f"correlation_id={correlation_id}"
        )

        # ── Stage 1: LLM Variable Resolution (tiny payload) ──
        seed_variables = await self.resolve_variable_names(
            target_variable, functions_source, provider, model
        )

        # ── Stage 2: Pure Python Extraction ──
        alias_map = self.build_alias_map(seed_variables, functions_source)

        tagged_lines = self.extract_relevant_lines(
            target_variable, functions_source, alias_map, seed_variables
        )

        chain_text = self.build_transformation_chain(
            target_variable, tagged_lines, seed_variables
        )

        # Store chain metadata in state
        state["variable_chain"] = {
            "target_variable": target_variable,
            "resolved_variables": seed_variables,
            "aliases": {fn: list(aliases) for fn, aliases in alias_map.items()},
            "relevant_line_count": len(tagged_lines),
            "total_source_lines": sum(
                len(lines) for lines in functions_source.values()
            ),
            "chain_text": chain_text,
        }

        total_lines = state["variable_chain"]["total_source_lines"]
        logger.info(
            f"Variable trace extraction: "
            f"{len(tagged_lines)} relevant / {total_lines} total lines "
            f"({len(tagged_lines) / max(total_lines, 1) * 100:.1f}% kept) | "
            f"correlation_id={correlation_id}"
        )

        # ── Stage 3: LLM Explanation (compact chain only) ──
        explanation = await self.explain_chain(
            target_variable=target_variable,
            chain_text=chain_text,
            user_query=query,
            provider=provider,
            model=model,
        )
        state["explanation"] = explanation

        return state

    def _extract_target_variable(self, query: str) -> Optional[str]:
        """Extract the target variable name from the user's query.

        Fallback if orchestrator didn't set target_variable.
        """
        query_upper = query.upper()

        patterns = [
            r'HOW\s+IS\s+([A-Z_][A-Z0-9_]{2,})\s+(?:CALCULATED|COMPUTED|DERIVED|POPULATED|UPDATED)',
            r'TRACE\s+(?:VARIABLE\s+)?([A-Z_][A-Z0-9_]{2,})',
            r'WHAT\s+(?:CALCULATES|UPDATES|POPULATES|SETS|COMPUTES)\s+([A-Z_][A-Z0-9_]{2,})',
            r'WHERE\s+DOES\s+([A-Z_][A-Z0-9_]{2,})\s+COME\s+FROM',
            r'(?:CALCULATION|LINEAGE|DERIVATION|FLOW)\s+OF\s+([A-Z_][A-Z0-9_]{2,})',
            r'\b([A-Z_][A-Z0-9_]{3,})\b',
        ]

        skip_words = {
            "HOW", "WHAT", "WHERE", "DOES", "THE", "THIS", "THAT",
            "ACROSS", "FUNCTIONS", "CALCULATED", "COMPUTED", "EXPLAIN",
            "TELL", "SHOW", "TRACE", "VARIABLE", "THESE", "MULTIPLE",
            "FROM", "COME", "ALL", "WITH", "USED",
        }

        for pattern in patterns:
            match = re.search(pattern, query_upper)
            if match:
                candidate = match.group(1)
                if candidate not in skip_words:
                    return candidate

        return None
