"""
RTIE Logic Explainer Agent.

Uses an LLM (OpenAI or Claude) to generate structured, fully-cited
explanations of PL/SQL functions and procedures. Every claim in the
explanation must reference specific line numbers from the source code.
LangSmith tracing is enabled on all LLM calls. Supports dynamic model
switching per request.
"""

import json
import re
from typing import Any, Dict, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage

from src.pipeline.state import LogicState
from src.llm_factory import create_llm
from src.llm_errors import sanitize_llm_exception
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id
from src.parsing.schema_discovery import fallback_to_default_schema

logger = get_logger(__name__, concern="app")

# Phrases that signal the LLM is flagging missing information. If the model
# emits one of these AND then continues to generate substantive text, we
# treat the response as self-contradictory and downgrade the badge.
_FORBIDDEN_CONTRADICTION_PHRASES = (
    "source not provided",
    "source not available",
    "i cannot determine",
    "i do not have access",
    "source was not included",
    "could not locate",
    "was not provided",
)

# Business-identifier pattern: "CAP973", "ABL013" etc. — at least two letters
# followed by at least two digits, optionally with trailing alphanumerics.
_IDENTIFIER_CODE_RE = re.compile(r"\b([A-Z]{2,}[0-9]{2,}[A-Z0-9]*)\b")

# Inline line references in markdown: "Line 203", "Lines 5-10", "L42".
_LINE_REF_RE = re.compile(
    r"\b(?:Lines?|L)\s*(\d+)(?:\s*[-\u2013]\s*(\d+))?\b"
)

# Query types for which grounded citations are expected. Other types
# (DATA_QUERY, VALUE_TRACE, etc.) have their own validation paths.
_REQUIRES_CITATIONS = frozenset({"VARIABLE_TRACE", "COLUMN_LOGIC", "FUNCTION_LOGIC"})


def evaluate_grounding(
    raw_query: str,
    markdown: str,
    multi_source: Dict[str, Any],
    functions_analyzed: List[str],
    query_type: str,
) -> Dict[str, Any]:
    """Evaluate whether a streamed explanation is grounded in retrieved source.

    Runs four independent checks:
      - forbidden-phrase self-contradiction
      - business-identifier grounding (CAP codes, etc.)
      - line-citation presence
      - empty source_citations rule for logic-explaining query types

    Returns a dict with keys ``badge`` (VERIFIED | UNVERIFIED), ``confidence``,
    ``source_citations`` (line-reference stubs), ``warnings`` (machine-readable),
    and ``sanity_messages`` (user-facing caveats that the caller should append
    to the streamed response).
    """
    warnings: List[str] = []
    sanity_messages: List[str] = []

    citations = _extract_line_citations(markdown)

    if _has_self_contradiction(markdown):
        warnings.append(
            "CONTRADICTION: response claims missing information but continues "
            "to provide substantive explanation"
        )
        sanity_messages.append(
            "The response appears to contradict itself — it states information "
            "is missing but then provides it. Please verify against the "
            "production code before relying on this explanation."
        )

    query_identifiers = set(_IDENTIFIER_CODE_RE.findall(raw_query.upper()))
    if query_identifiers:
        raw_source_text = _concat_multi_source(multi_source).upper()
        ungrounded = sorted(
            ident for ident in query_identifiers if ident not in raw_source_text
        )
        if ungrounded:
            ident_list = ", ".join(ungrounded)
            warnings.append(
                f"UNGROUNDED_IDENTIFIERS: {ident_list} mentioned in query but "
                f"not found in any loaded function source"
            )
            sanity_messages.append(
                f"This explanation may not fully describe {ident_list}. The "
                f"identifier was mentioned but no loaded function was confirmed "
                f"to compute it. The explanation below reflects what the loaded "
                f"functions do — please verify against the actual production "
                f"code."
            )

    # Requested-function grounding: if the user named a specific PL/SQL
    # function and it didn't make it into functions_analyzed, the semantic
    # search produced adjacent functions instead of the real one. This
    # catches the exact W37 failure mode where the vector store doesn't
    # index a schema (e.g. OFSERM) but the graph does have it — the
    # pre-check passes but semantic search silently substitutes neighbors.
    requested_functions = _extract_function_candidates_local(raw_query)
    if requested_functions:
        analyzed_upper = {f.upper() for f in functions_analyzed}
        missing = [
            name for name in requested_functions
            if name.upper() not in analyzed_upper
        ]
        if missing:
            names = ", ".join(missing)
            warnings.append(
                f"NAMED_FUNCTION_NOT_RETRIEVED: {names} named in query but not "
                f"present in functions_analyzed={list(analyzed_upper)}"
            )
            sanity_messages.append(
                f"The explanation below may describe functions related to "
                f"{names} rather than {names} itself — the semantic search "
                f"returned different functions than the one you asked about. "
                f"Please verify against the actual production code."
            )

    requires_citations = query_type in _REQUIRES_CITATIONS
    # A response is "citationally grounded" if it either has explicit line
    # references OR analyzed at least one function (which implies the LLM
    # was given real source to work from).
    has_citations = bool(citations) or bool(functions_analyzed)

    if requires_citations and not has_citations:
        badge = "UNVERIFIED"
        confidence = 0.0
        warnings.append(
            "CITATIONS: response has no line references and no functions were "
            "analyzed — cannot verify grounding"
        )
    elif warnings:
        badge = "UNVERIFIED"
        confidence = 0.4 if citations else 0.2
    else:
        badge = "VERIFIED"
        confidence = 0.95 if citations else 0.8

    return {
        "badge": badge,
        "confidence": confidence,
        "source_citations": citations,
        "warnings": warnings,
        "sanity_messages": sanity_messages,
    }


def detect_ungrounded_identifiers(
    raw_query: str,
    multi_source: Dict[str, Any],
) -> List[str]:
    """Return business identifiers named in the query but absent from every
    retrieved function's source_code body.

    Pure function — no side effects, no LLM calls, no Redis reads. Uses the
    same identifier regex and matching logic as evaluate_grounding so the
    pre-generation branch and the post-hoc backstop always agree on which
    identifiers are ungrounded. Call this BEFORE the LLM generation step to
    decide whether to route to the ungrounded branch.

    An empty list means either (a) the query contains no business identifiers,
    or (b) every identifier is present in at least one retrieved function.
    In either case the normal generation path should run.
    """
    query_identifiers = set(_IDENTIFIER_CODE_RE.findall(raw_query.upper()))
    if not query_identifiers:
        return []
    source_text = _concat_multi_source(multi_source).upper()
    return sorted(
        ident for ident in query_identifiers if ident not in source_text
    )


# Minimum source-body length (in characters) below which we consider a
# function's retrieved source effectively empty. A real PL/SQL function body
# even for a one-liner has a CREATE/BEGIN/END structure well over 50 chars,
# so anything shorter is treated as "no real source available".
_PARTIAL_SOURCE_MIN_CHARS = 50


def detect_partial_source_function(
    function_name: str,
    schema: str,
    retrieved_source: Any,
    redis_client: Any = None,
) -> bool:
    """Return True when *function_name* has graph metadata but no usable
    source body to feed the LLM.

    This is the W49 partial-indexed state: the function name and hierarchy
    are known (``graph:meta:<schema>:<function_name>`` exists), but
    semantic search / source retrieval did not return its PL/SQL body. The
    response generator must NOT speculate using related functions when
    this is true.

    Args:
        function_name: The asked-about function (case-insensitive).
        schema: Schema to check for parse metadata. Pass empty string to
            skip the metadata check (caller already verified).
        retrieved_source: The source body returned for *function_name* by
            the pipeline. Acceptable shapes mirror ``multi_source`` entries:
            ``None``, an empty string, a list of dicts ``[{"line": N,
            "text": "..."}]``, or a list of strings. Treated as missing
            when the joined text is below ``_PARTIAL_SOURCE_MIN_CHARS``.
        redis_client: Redis client used to verify metadata presence. When
            ``None``, the check falls open (returns False) to avoid a
            false positive on a misconfigured environment.

    Returns:
        True only when both conditions hold:
          - graph metadata exists for (schema, function_name)
          - retrieved_source is missing/empty/below threshold

    Pure-ish function: no LLM calls, only a single Redis GET on the
    parse_metadata key. Reuses the existing client connection.
    """
    if not function_name:
        return False
    if redis_client is None:
        return False

    body_len = _retrieved_source_length(retrieved_source)
    if body_len >= _PARTIAL_SOURCE_MIN_CHARS:
        return False

    try:
        from src.parsing.store import get_parse_metadata
        metadata = get_parse_metadata(
            redis_client, schema, function_name.upper()
        )
    except Exception as exc:
        logger.debug(
            "partial-source metadata lookup failed for %s.%s: %s",
            schema, function_name, exc,
        )
        return False
    return metadata is not None


def _retrieved_source_length(retrieved_source: Any) -> int:
    """Return the joined character length of *retrieved_source*.

    Handles the same shapes as ``_concat_multi_source`` (list of dicts,
    list of strings, plain string, None). Whitespace-only content
    collapses to length 0 so it triggers the partial-source path.
    """
    if retrieved_source is None:
        return 0
    if isinstance(retrieved_source, str):
        return len(retrieved_source.strip())
    if isinstance(retrieved_source, list):
        parts: List[str] = []
        for item in retrieved_source:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return len(" ".join(parts).strip())
    return 0


def _extract_line_citations(markdown: str) -> List[Dict[str, Any]]:
    """Return citation stubs for every Line-N reference found in *markdown*.

    Ranges like "Lines 203-223" expand into one stub per line. De-duplicated
    by line number so a heavily-cited line only shows up once.
    """
    citations: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for match in _LINE_REF_RE.finditer(markdown):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start or end - start > 500:
            # Guard against degenerate regex matches (huge spans, reversed).
            continue
        for line_num in range(start, end + 1):
            if line_num in seen:
                continue
            seen.add(line_num)
            citations.append({
                "line": line_num,
                "text": "",
                "context": "inline reference",
                "source": "markdown",
            })
    return citations


def _has_self_contradiction(markdown: str) -> bool:
    """Return True if a forbidden phrase precedes >50 tokens of continuation."""
    low = markdown.lower()
    for phrase in _FORBIDDEN_CONTRADICTION_PHRASES:
        idx = low.find(phrase)
        if idx < 0:
            continue
        rest = markdown[idx + len(phrase):]
        tokens = [t for t in re.split(r"\s+", rest) if t]
        if len(tokens) > 50:
            return True
    return False


def _concat_multi_source(multi_source: Dict[str, Any]) -> str:
    """Flatten every function's source_code lines into one searchable string."""
    parts: List[str] = []
    for fn_data in multi_source.values():
        src = fn_data.get("source_code") or []
        for item in src:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
    return " ".join(parts)


# Local copy of the function-name extractor used during grounding evaluation.
# Kept in sync with src.agents.orchestrator.extract_function_candidates —
# duplicated here to avoid an import cycle (orchestrator imports from store,
# grounding is called from main.py after orchestrator has already run).
_FN_CANDIDATE_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\b")
_FN_COLUMN_PREFIX_RE = re.compile(r"^[A-Z]_[A-Z]")
_FN_STOPWORDS = frozenset({
    "FIC_MIS_DATE", "MIS_DATE", "RUN_ID", "BATCH_ID", "RUN_SKEY",
    "RUN_EXECUTION_ID", "START_DATE", "END_DATE", "ACCOUNT_NUMBER",
    "TARGET_VARIABLE", "STG_GL_DATA", "V_GL_CODE", "V_PROD_CODE",
    "V_LOB_CODE", "V_LV_CODE",
})


def _extract_function_candidates_local(query: str) -> List[str]:
    """Same heuristic as orchestrator.extract_function_candidates; duplicated
    here to avoid an import cycle during grounding evaluation."""
    seen: set[str] = set()
    out: List[str] = []
    for match in _FN_CANDIDATE_RE.finditer(query):
        cand = match.group(1)
        cu = cand.upper()
        if cu in seen:
            continue
        seen.add(cu)
        if len(cand) < 6:
            continue
        if cu in _FN_STOPWORDS:
            continue
        if _FN_COLUMN_PREFIX_RE.match(cu):
            continue
        out.append(cand)
    return out


EXPLANATION_SYSTEM_PROMPT = """You are an expert PL/SQL analyst for the RTIE system (Regulatory Trace & Intelligence Engine).
You analyze Oracle OFSAA PL/SQL functions and procedures used in regulatory capital computations.

You will receive:
1. The complete source code of a PL/SQL function/procedure with line numbers.
2. A call tree showing all dependencies and their source code.

Your task is to produce a structured JSON explanation. You MUST respond with ONLY valid JSON — no markdown, no extra text.

STRICT RULES:
- ONLY reference line numbers and content that exist in the provided source code.
- NEVER hallucinate logic, functions, or formulas that are not in the source.
- Cite specific line numbers for EVERY claim you make.
- If something is unclear or ambiguous, FLAG IT rather than guessing.
- Every formula must map to exact lines in the source code.
- Every dependency mentioned must exist in the call tree provided.

Output JSON schema:
{
  "summary": "A concise plain-English summary of what the function/procedure does",
  "step_by_step": [
    {
      "step": 1,
      "description": "What this step does",
      "lines": [10, 11, 12],
      "code_snippet": "relevant code from those lines"
    }
  ],
  "formulas": [
    {
      "name": "Formula name or description",
      "formula": "The mathematical formula",
      "lines": [15, 16],
      "variables": {"var_name": "description of what it represents"}
    }
  ],
  "dependencies_used": [
    {
      "name": "FN_DEPENDENCY_NAME",
      "purpose": "What this dependency does in context",
      "called_at_lines": [25, 30]
    }
  ],
  "regulatory_refs": [
    "Any regulatory framework references found (Basel III, IFRS 9, etc.)"
  ],
  "raw_source_references": [
    {
      "line": 10,
      "text": "exact text from that line",
      "significance": "why this line matters"
    }
  ],
  "unclear_items": [
    "Anything that could not be determined from the source code alone"
  ]
}
"""


SEMANTIC_EXPLANATION_PROMPT = """You are an expert in Oracle OFSAA FSAPPS regulatory capital calculations.
You receive source code from one or more PL/SQL functions and must explain the BUSINESS MEANING and DATA FLOW — not the syntax.

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
- Use ## for main heading, ### for each function/step
- Include ```sql code blocks with the relevant PL/SQL
- Put line references in section headers: ### Step 1: Initial Insert (Lines 203-223)
- Do NOT repeat line references separately below code blocks
"""


class LogicExplainer:
    """Agent for generating structured PL/SQL logic explanations.

    Uses OpenAI or Anthropic LLMs with LangSmith tracing to analyze
    source code and produce fully-cited, step-by-step explanations of
    regulatory capital computation logic. Model is selectable per request.
    """

    def __init__(
        self,
        temperature: float = 0,
        max_tokens: int = 2000,
        langsmith_project: str = "RTIE",
    ) -> None:
        """Initialize the LogicExplainer with LLM settings.

        Args:
            temperature: LLM temperature. Defaults to 0.
            max_tokens: Maximum tokens for LLM response. Defaults to 2000.
            langsmith_project: LangSmith project name for tracing. Defaults to 'RTIE'.
        """
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._langsmith_project = langsmith_project
        # Optional Redis client used to look up batch/process/sub-process
        # hierarchy for streamed explanations. Wired post-construction from
        # main.py because the graph Redis client is created later in the
        # lifespan. Absence is non-fatal — the hierarchy header is then
        # simply omitted.
        self._redis_client = None

    def set_redis_client(self, redis_client) -> None:
        """Inject the Redis client used to look up hierarchy metadata."""
        self._redis_client = redis_client

    def hierarchy_header(self, state: LogicState) -> str:
        """Build the one-line hierarchy context header for a response.

        Resolves the primary function (top-ranked ``multi_source`` entry by
        score, falling back to ``object_name``) and fetches its graph from
        Redis. If the graph carries a ``hierarchy`` block, returns a
        prefix string that can be prepended to the explanation; otherwise
        returns an empty string. Also prefixes the inactive-task notice
        when the primary function is marked inactive.
        """
        if self._redis_client is None:
            return ""

        multi_source = state.get("multi_source") or {}
        primary_fn: str = ""
        if multi_source:
            ranked = sorted(
                multi_source.items(),
                key=lambda kv: (kv[1] or {}).get("score", 0) or 0,
                reverse=True,
            )
            primary_fn = ranked[0][0]
        if not primary_fn:
            primary_fn = (state.get("object_name") or "").strip()
        if not primary_fn:
            return ""

        schema = (state.get("schema") or "").strip() or fallback_to_default_schema(
            "logic_explainer._render_hierarchy_header",
            state.get("correlation_id", ""),
        )

        try:
            from src.parsing.store import get_function_graph
            graph = get_function_graph(self._redis_client, schema, primary_fn.upper())
        except Exception as exc:  # Redis miss / serialisation error shouldn't
            logger.debug("hierarchy lookup failed for %s: %s", primary_fn, exc)
            return ""
        if graph is None:
            return ""

        hierarchy = graph.get("hierarchy")
        if not hierarchy:
            return ""

        batch = hierarchy.get("batch") or ""
        process = hierarchy.get("process") or ""
        sub_process = hierarchy.get("sub_process") or ""
        order = hierarchy.get("task_order")
        parts = [p for p in (batch, process, sub_process) if p]
        if not parts:
            return ""

        order_suffix = f" (task #{order})" if isinstance(order, int) else ""
        header = (
            f"This function runs in {' → '.join(parts)}{order_suffix}.\n\n"
        )

        if hierarchy.get("active") is False:
            reason = hierarchy.get("inactive_reason") or "reason not recorded"
            header = (
                "_Note: This task is marked inactive in the current batch "
                f"configuration (reason: {reason}). The explanation below "
                "describes what it would do if it were active._\n\n"
                + header
            )
        return header

    def _get_llm(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> BaseChatModel:
        """Get an LLM instance for the specified provider.

        Args:
            provider: 'openai' or 'anthropic'. None uses default.
            model: Specific model name. None uses default for provider.

        Returns:
            A LangChain chat model instance.
        """
        return create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            json_mode=(provider or "openai") != "anthropic",
        )

    async def explain_logic(
        self,
        state: LogicState,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LogicState:
        """Generate a structured explanation of the PL/SQL source code.

        Sends the full source code and call tree to the selected LLM,
        which returns a structured JSON explanation with line citations.
        LangSmith tracing is active for this call.

        Args:
            state: Current pipeline state with source_code and call_tree.
            provider: LLM provider ('openai' or 'anthropic'). None uses default.
            model: Specific model name. None uses default for provider.

        Returns:
            Updated state with explanation dict populated.
        """
        correlation_id = get_correlation_id()
        object_name = state["object_name"]
        schema = state["schema"]

        logger.info(
            f"Generating explanation for {schema}.{object_name} | "
            f"provider={provider}, model={model} | "
            f"correlation_id={correlation_id}"
        )

        llm = self._get_llm(provider, model)

        # Format source code for the LLM
        source_text = self._format_source_code(state["source_code"])
        call_tree_text = self._format_call_tree(state["call_tree"])

        system_prompt = EXPLANATION_SYSTEM_PROMPT
        if (provider or "").lower() == "anthropic":
            system_prompt += (
                "\n\nIMPORTANT: Respond with ONLY the raw JSON object. "
                "No markdown code fences, no explanation before or after."
            )

        user_prompt = (
            f"Analyze the following PL/SQL object: {schema}.{object_name}\n\n"
            f"=== SOURCE CODE ===\n{source_text}\n\n"
            f"=== CALL TREE (Dependencies) ===\n{call_tree_text}\n\n"
            f"Produce a complete structured JSON explanation following the "
            f"schema in your instructions. Cite every claim with line numbers."
        )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]

        try:
            response = await llm.ainvoke(messages)
        except Exception as exc:
            raise sanitize_llm_exception(
                exc, context="explain_logic", correlation_id=correlation_id
            ) from exc

        raw_content = response.content.strip()

        # Strip markdown fences if present (Claude sometimes adds them)
        if raw_content.startswith("```"):
            raw_content = raw_content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        logger.info(
            f"LLM explanation received for {schema}.{object_name} "
            f"({len(raw_content)} chars) | correlation_id={correlation_id}"
        )

        # Parse the JSON response
        explanation = json.loads(raw_content)

        state["explanation"] = explanation

        logger.info(
            f"Explanation parsed: {len(explanation.get('step_by_step', []))} steps, "
            f"{len(explanation.get('formulas', []))} formulas, "
            f"{len(explanation.get('dependencies_used', []))} deps | "
            f"correlation_id={correlation_id}"
        )
        return state

    async def explain_semantic(
        self,
        state: LogicState,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LogicState:
        """Generate explanation across multiple functions found via semantic search.

        Receives all relevant function sources and the user's original question,
        then produces a unified cross-function explanation with citations.

        Args:
            state: Pipeline state with raw_query and multi_source.
            provider: LLM provider. None uses default.
            model: Model name. None uses default.

        Returns:
            Updated state with explanation dict.
        """
        correlation_id = get_correlation_id()
        query = state["raw_query"]
        multi_source = state.get("multi_source", {})

        logger.info(
            f"Generating semantic explanation for: {query[:80]}... "
            f"({len(multi_source)} functions) | "
            f"provider={provider}, model={model} | "
            f"correlation_id={correlation_id}"
        )

        llm = self._get_llm(provider, model)

        # Check if graph pipeline produced a structured payload
        llm_payload = state.get("llm_payload")
        if llm_payload and state.get("graph_available"):
            logger.info("explain_semantic: using graph pipeline payload (%d chars)", len(llm_payload))
            user_prompt = (
                f"User Question: {query}\n\n"
                f"The following structured analysis was produced from the parsed PL/SQL graph:\n\n"
                f"{llm_payload}\n\n"
                "Answer the user's question with a detailed markdown explanation. "
                "Cite specific function names and line numbers for every claim."
            )
        else:
            logger.info("explain_semantic: falling back to raw source")
            # Format all function sources
            function_sections = []
            for fn_name, fn_data in multi_source.items():
                source_text = self._format_source_code(fn_data.get("source_code", []))
                section = (
                    f"=== FUNCTION: {fn_name} (relevance: {fn_data.get('score', 0):.4f}) ===\n"
                    f"Description: {fn_data.get('description', 'N/A')}\n"
                    f"Tables Read: {fn_data.get('tables_read', 'N/A')}\n"
                    f"Tables Written: {fn_data.get('tables_written', 'N/A')}\n\n"
                    f"Source Code:\n{source_text}\n"
                )
                function_sections.append(section)

            user_prompt = (
                f"User Question: {query}\n\n"
                f"The following {len(multi_source)} functions were found via semantic search:\n\n"
                + "\n".join(function_sections)
                + "\n\nAnswer the user's question with a detailed markdown explanation. "
                "Cite specific function names and line numbers for every claim."
            )

        # Use non-JSON mode for markdown responses
        llm = create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=4096,
            json_mode=False,
        )

        messages = [
            SystemMessage(content=SEMANTIC_EXPLANATION_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        try:
            response = await llm.ainvoke(messages)
        except Exception as exc:
            raise sanitize_llm_exception(
                exc, context="explain_semantic", correlation_id=correlation_id
            ) from exc
        markdown_content = response.content.strip()

        # Prepend hierarchy context header when available
        header = self.hierarchy_header(state)
        if header:
            markdown_content = header + markdown_content

        # Store as markdown explanation
        state["explanation"] = {
            "markdown": markdown_content,
            "summary": markdown_content[:200] + "..." if len(markdown_content) > 200 else markdown_content,
        }

        logger.info(
            f"Semantic explanation generated: "
            f"{len(markdown_content)} chars markdown | "
            f"correlation_id={correlation_id}"
        )
        return state

    async def stream_semantic(
        self,
        state: LogicState,
        provider: str | None = None,
        model: str | None = None,
    ):
        """Stream semantic explanation tokens as an async generator.

        Yields markdown tokens one chunk at a time for SSE streaming.
        Does NOT update state — the caller collects the full text.

        Args:
            state: Pipeline state with raw_query and multi_source.
            provider: LLM provider.
            model: Model name.

        Yields:
            String chunks of the markdown response.
        """
        query = state["raw_query"]

        # Check if graph pipeline produced a structured payload
        llm_payload = state.get("llm_payload")
        if llm_payload and state.get("graph_available"):
            logger.info("stream_semantic: using graph pipeline payload (%d chars)", len(llm_payload))
            user_prompt = (
                f"User Question: {query}\n\n"
                f"The following structured analysis was produced from the parsed PL/SQL graph:\n\n"
                f"{llm_payload}\n\n"
                "Answer the user's question with a detailed markdown explanation. "
                "Cite specific function names and line numbers for every claim."
            )
        else:
            logger.info("stream_semantic: falling back to raw source")
            multi_source = state.get("multi_source", {})
            function_sections = []
            for fn_name, fn_data in multi_source.items():
                source_text = self._format_source_code(fn_data.get("source_code", []))
                section = (
                    f"=== FUNCTION: {fn_name} (relevance: {fn_data.get('score', 0):.4f}) ===\n"
                    f"Description: {fn_data.get('description', 'N/A')}\n"
                    f"Tables Read: {fn_data.get('tables_read', 'N/A')}\n"
                    f"Tables Written: {fn_data.get('tables_written', 'N/A')}\n\n"
                    f"Source Code:\n{source_text}\n"
                )
                function_sections.append(section)

            user_prompt = (
                f"User Question: {query}\n\n"
                f"The following {len(multi_source)} functions were found via semantic search:\n\n"
                + "\n".join(function_sections)
                + "\n\nAnswer the user's question with a detailed markdown explanation. "
                "Cite specific function names and line numbers for every claim."
            )

        llm = create_llm(
            provider=provider,
            model=model,
            temperature=self._temperature,
            max_tokens=4096,
            json_mode=False,
        )

        messages = [
            SystemMessage(content=SEMANTIC_EXPLANATION_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        # The hierarchy header is emitted by the caller (main.py's stream
        # endpoint) once before any stream_* call, so that VARIABLE_TRACE
        # queries that bypass this method still get a header. We
        # deliberately do NOT emit it here to avoid duplication.

        try:
            async for chunk in llm.astream(messages):
                if chunk.content:
                    yield chunk.content
        except Exception as exc:
            raise sanitize_llm_exception(
                exc, context="stream_semantic"
            ) from exc

    def _format_source_code(self, source_lines: list) -> str:
        """Format source code lines for LLM consumption.

        Args:
            source_lines: List of dicts with 'line' and 'text' keys,
                or raw strings.

        Returns:
            Formatted string with line numbers and code text.
        """
        lines = []
        for item in source_lines:
            if isinstance(item, dict):
                line_num = item.get("line", "?")
                text = item.get("text", "").rstrip("\n")
                lines.append(f"L{line_num}: {text}")
            else:
                lines.append(str(item))
        return "\n".join(lines)

    def _format_call_tree(self, call_tree: dict) -> str:
        """Format the call tree for LLM consumption.

        Args:
            call_tree: Nested dependency dictionary.

        Returns:
            Human-readable string representation of the call tree.
        """
        return json.dumps(call_tree, indent=2, default=str)
