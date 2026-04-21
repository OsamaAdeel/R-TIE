"""
RTIE Orchestrator Agent.

Handles query classification and command routing. Determines whether
user input is a slash command or a logic query, and extracts structured
metadata using an LLM with strict JSON output. All queries are routed
through semantic search — the orchestrator simply validates and
prepares the query for the pipeline.
"""

import asyncio
import json
import re
from difflib import get_close_matches
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import SystemMessage, HumanMessage

from src.pipeline.state import LogicState
from src.llm_factory import create_llm
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id
from src.parsing.store import get_function_graph
from src.telemetry import stage_timer

logger = get_logger(__name__, concern="app")

# Candidate PL/SQL function identifiers: letter-start, at least one underscore,
# word-chars only. Post-filtered on length and stopwords.
_FUNCTION_NAME_CANDIDATE = re.compile(
    r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+)\b"
)

# OFSAA column naming convention: a single type-prefix letter followed by an
# underscore and caps (N_, V_, F_, D_, T_ prefixes). These are never PL/SQL
# function names — they're always staging-table columns — so the pre-check
# must not decline on them.
_COLUMN_TYPE_PREFIX = re.compile(r"^[A-Z]_[A-Z]")

# Tokens that look like function names but are really PL/SQL parameters,
# date identifiers, or English phrases. These are NEVER checked against the graph.
_NAME_STOPWORDS = frozenset({
    "FIC_MIS_DATE", "MIS_DATE", "RUN_ID", "BATCH_ID", "RUN_SKEY", "RUN_EXECUTION_ID",
    "START_DATE", "END_DATE", "ACCOUNT_NUMBER", "TARGET_VARIABLE",
    "STG_GL_DATA", "V_GL_CODE", "V_PROD_CODE", "V_LOB_CODE", "V_LV_CODE",
})

# Schemas to check when resolving a function name. Kept in sync with the
# schemas discovered by the loader. Extending this list is the minimal
# cross-schema support required for W37 pre-check.
_PRECHECK_SCHEMAS = ("OFSMDM", "OFSERM")


class ClassificationResult(BaseModel):
    """Pydantic model for LLM classification output.

    Attributes:
        query_type: 'COLUMN_LOGIC' or 'VARIABLE_TRACE'.
        intent: What the user is asking about.
        search_terms: Key terms for semantic search enrichment.
        target_variable: Variable/column name for VARIABLE_TRACE queries.
        schema_name: Oracle schema name (e.g. OFSMDM).
        confidence: Model's confidence in understanding the query.
    """

    model_config = {"strict": True}

    query_type: str
    intent: str
    search_terms: List[str]
    target_variable: Optional[str] = None
    schema_name: str
    confidence: float
    # Phase 2 fields -- populated only for data-trace queries.
    account_number: Optional[str] = None
    mis_date: Optional[str] = None
    # Date range (populated only for time-series queries; both must be set).
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    expected_value: Optional[float] = None
    actual_value: Optional[float] = None
    lob_code: Optional[str] = None
    lv_code: Optional[str] = None
    gl_code: Optional[str] = None
    branch_code: Optional[str] = None
    # Populated only when query_type == "UNSUPPORTED"
    unsupported_reason: Optional[str] = None


class CommandResult(BaseModel):
    """Parsed slash command result.

    Attributes:
        is_command: Whether the input was a slash command.
        command: The command name (e.g. 'refresh-cache').
        args: List of command arguments.
    """

    model_config = {"strict": True}

    is_command: bool
    command: str
    args: List[str]


CLASSIFICATION_SYSTEM_PROMPT = """You are a query classifier for the RTIE system (Regulatory Trace & Intelligence Engine).
Your job is to understand user queries about Oracle OFSAA PL/SQL objects, tables, columns, and data flows.

You MUST respond with ONLY a valid JSON object — no markdown, no explanation, no extra text.

{
  "query_type": "COLUMN_LOGIC" | "VARIABLE_TRACE" | "VALUE_TRACE" | "DIFFERENCE_EXPLANATION" | "DATA_QUERY" | "UNSUPPORTED",
  "intent": "<concise description of what the user wants to know>",
  "search_terms": ["<keyword1>", "<keyword2>", "..."],
  "target_variable": "<variable/column name, or null>",
  "schema_name": "<Oracle schema name, default OFSMDM>",
  "confidence": <float between 0.0 and 1.0>,
  "account_number": "<account number mentioned in the query, or null>",
  "mis_date": "<MIS date in YYYY-MM-DD format, or null>",
  "start_date": "<ISO date YYYY-MM-DD, or null — set only for date-range queries>",
  "end_date":   "<ISO date YYYY-MM-DD, or null — set only for date-range queries>",
  "expected_value": <number the user says is expected / what the bank reports, or null>,
  "actual_value": <number the user says the system shows, or null>,
  "lob_code": "<line-of-business code, or null>",
  "lv_code": "<LV code, or null>",
  "gl_code": "<GL code, or null>",
  "branch_code": "<branch code, or null>",
  "unsupported_reason": "<short phrase naming the missing capability, or null>"
}

Query types:
- VARIABLE_TRACE:         how is X calculated -- logic only, no data needed.
- COLUMN_LOGIC:           what does X do, explain function X -- logic only.
- VALUE_TRACE:            why is X showing value Y for a specific account on a
                          specific MIS date? Single-date, single-row trace.
                          Requires mis_date. Extract account_number if given.
- DIFFERENCE_EXPLANATION: bank says A, we show B -- why? Extract both values.
                          Requires mis_date. expected_value = bank value, actual_value = system.
- DATA_QUERY:             question about a SET of rows, an aggregate value, or a
                          comparison across dates. Answer requires running SQL,
                          NOT graph tracing. Triggers:
                            * "total", "sum", "average", "count", "how many"
                            * "which accounts", "list all", "breakdown by"
                            * "changed between X and Y", "from X to Y",
                              "difference between DATE1 and DATE2" — any
                              comparison involving TWO MIS dates (time-series).
                            * Every question without a specific single account_number
                              that asks for numbers/rows.
- UNSUPPORTED:            question the system cannot honestly answer. Set
                          unsupported_reason. Triggers:
                            * References to FCT_* tables or downstream result tables
                              not present in the graph (cross-table reconciliation).
                            * Forecasting / prediction ("likely to fail", "next quarter",
                              "forecast", "will X happen").
                            * Any other capability outside read-only introspection of
                              the current schema + graph.

Routing rules (apply in order):
 1. If the query contains forecasting / FCT-vs-STG / future-tense prediction
    language -> UNSUPPORTED. Use unsupported_reason to name it.
 2. Otherwise, if the query mentions TWO MIS dates (a date range / time-series
    comparison) -> DATA_QUERY, regardless of whether an account_number is
    present. Set start_date and end_date; leave mis_date null.
 3. Otherwise, if the query asks about a single specific account_number on
    a single MIS date and wants to understand a value (why / how / breakdown)
    -> VALUE_TRACE (or DIFFERENCE_EXPLANATION if two values are compared).
 4. Otherwise, if the query uses aggregation ("total", "sum", "average",
    "count", "how many") OR asks for a row list without specifying one
    account ("which accounts", "list all", "show me all", "breakdown by")
    -> DATA_QUERY.
 5. Otherwise -> VARIABLE_TRACE or COLUMN_LOGIC as before.
 6. When in doubt between VALUE_TRACE and DATA_QUERY, prefer VALUE_TRACE
    ONLY when a single specific account_number + single mis_date are present.
    Otherwise prefer DATA_QUERY.

Date extraction rules:
- For single-date queries: set `mis_date` to that date. Leave `start_date`
  and `end_date` null.
- For date-range queries ("between X and Y", "from X to Y", "changed from X
  to Y", "between DATE1 and DATE2"): set `start_date` to the EARLIER date,
  `end_date` to the LATER date. Leave `mis_date` NULL. Never silently drop
  one of the two dates.
- Never populate all three of mis_date, start_date, end_date. It's either
  (mis_date only) or (start_date + end_date only).

Field rules:
- target_variable: extract the exact column/variable name (e.g. EAD_AMOUNT,
  N_ANNUAL_GROSS_INCOME).
- search_terms: extract ALL relevant keywords -- function/table/column names
  and business concepts.
- schema_name defaults to "OFSMDM" unless another schema is specified.
- For VALUE_TRACE / DIFFERENCE_EXPLANATION: mis_date is required -- set
  confidence low if not found.
- For DATA_QUERY: either mis_date OR (start_date + end_date) is required --
  set confidence low if neither is found.
- Extract account_number, lob_code, lv_code, gl_code, branch_code only if
  mentioned.
- unsupported_reason: only populated for UNSUPPORTED. Examples:
    "cross-table reconciliation against FCT tables (not in scope)",
    "forecasting / prediction (system is read-only introspection only)",
    "references table X which is not parsed in the graph".

Examples:
- "Explain FN_LOAD_OPS_RISK_DATA"
    -> query_type: "COLUMN_LOGIC", target_variable: null
- "How is EAD_AMOUNT calculated across functions?"
    -> query_type: "VARIABLE_TRACE", target_variable: "EAD_AMOUNT"
- "Why is N_EOP_BAL for account LD1323300008 showing 50000000 on 2025-12-31?"
    -> query_type: "VALUE_TRACE", target_variable: "N_EOP_BAL",
       account_number: "LD1323300008", mis_date: "2025-12-31",
       start_date: null, end_date: null, actual_value: 50000000
- "Bank says EAD is 52M but system shows 50M for account X on 2025-12-31"
    -> query_type: "DIFFERENCE_EXPLANATION", target_variable: "EAD",
       expected_value: 52000000, actual_value: 50000000,
       mis_date: "2025-12-31", account_number: "X"
- "What is the total N_EOP_BAL for all accounts with V_LV_CODE='ABL' on 2025-12-31?"
    -> query_type: "DATA_QUERY", target_variable: "N_EOP_BAL",
       mis_date: "2025-12-31", lv_code: "ABL",
       start_date: null, end_date: null
- "How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?"
    -> query_type: "DATA_QUERY", target_variable: "F_EXPOSURE_ENABLED_IND",
       mis_date: "2025-12-31", start_date: null, end_date: null
- "Which accounts have N_EOP_BAL = 0 on 2025-12-31?"
    -> query_type: "DATA_QUERY", target_variable: "N_EOP_BAL",
       mis_date: "2025-12-31", start_date: null, end_date: null
- "Show me all accounts on 2025-12-31"
    -> query_type: "DATA_QUERY", target_variable: null, mis_date: "2025-12-31",
       start_date: null, end_date: null
- "How did N_EOP_BAL change for account TF1528012748-T24-COLLBLG between 2025-09-30 and 2025-12-31?"
    -> query_type: "DATA_QUERY", target_variable: "N_EOP_BAL",
       account_number: "TF1528012748-T24-COLLBLG",
       mis_date: null,
       start_date: "2025-09-30", end_date: "2025-12-31"
- "N_EOP_BAL changed from 100M on 2025-09-30 to 120M on 2025-12-31 — why?"
    -> query_type: "DATA_QUERY", target_variable: "N_EOP_BAL",
       mis_date: null,
       start_date: "2025-09-30", end_date: "2025-12-31"
- "Why does N_EOP_BAL differ between STG and FCT for account X on 2025-12-31?"
    -> query_type: "UNSUPPORTED",
       unsupported_reason: "cross-table reconciliation against FCT tables (not in scope)"
- "Which accounts are likely to fail next quarter?"
    -> query_type: "UNSUPPORTED",
       unsupported_reason: "forecasting / prediction (system is read-only introspection only)"
- "FCT_PRODUCT_EXPOSURES value differs from STG_PRODUCT_PROCESSOR for account X on 2025-12-31"
    -> query_type: "UNSUPPORTED",
       unsupported_reason: "cross-table reconciliation against FCT tables (not in scope)"
"""


class Orchestrator:
    """Orchestrator agent for query classification and command routing.

    Classifies incoming queries and extracts search terms for semantic
    search. Supports dynamic model switching between OpenAI and Claude.
    """

    def __init__(
        self,
        temperature: float = 0,
        max_tokens: int = 2000,
    ) -> None:
        """Initialize the Orchestrator with LLM settings.

        Args:
            temperature: LLM temperature. Defaults to 0.
            max_tokens: Maximum tokens for LLM response. Defaults to 2000.
        """
        self._temperature = temperature
        self._max_tokens = max_tokens

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

    def check_command(self, query: str) -> CommandResult:
        """Check if the query is a slash command.

        Parses queries starting with '/' into command name and arguments.

        Args:
            query: The raw user query string.

        Returns:
            CommandResult with is_command=True and parsed command/args if
            the query starts with '/', otherwise is_command=False.
        """
        query = query.strip()
        if not query.startswith("/"):
            logger.info(f"Query is not a command: {query[:50]}...")
            return CommandResult(is_command=False, command="", args=[])

        parts = query.split()
        command = parts[0].lstrip("/")
        args = parts[1:] if len(parts) > 1 else []

        logger.info(
            f"Command detected: /{command} with args: {args} | "
            f"correlation_id={get_correlation_id()}"
        )
        return CommandResult(is_command=True, command=command, args=args)

    async def classify_query(
        self,
        query: str,
        state: LogicState,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> LogicState:
        """Classify a query and extract search terms for semantic search.

        Sends the query to the LLM to extract intent and search terms.
        These terms enrich the semantic search embedding for better results.

        Args:
            query: The raw user query string.
            state: Current LogicState to update.
            provider: LLM provider. None uses default.
            model: Specific model name. None uses default.

        Returns:
            Updated LogicState with query_type, object_name, schema populated.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"Classifying query: {query[:80]}... | "
            f"provider={provider}, model={model} | "
            f"correlation_id={correlation_id}"
        )

        llm = self._get_llm(provider, model)

        system_prompt = CLASSIFICATION_SYSTEM_PROMPT
        if (provider or "").lower() == "anthropic":
            system_prompt += (
                "\n\nIMPORTANT: Respond with ONLY the raw JSON object. "
                "No markdown code fences, no explanation before or after."
            )

        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=query),
        ]

        with stage_timer("llm_api_classify", correlation_id, provider=(provider or "default")):
            response = await llm.ainvoke(messages)
        raw_content = response.content.strip()

        # Strip markdown fences if present
        if raw_content.startswith("```"):
            raw_content = raw_content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        logger.info(
            f"LLM classification response: {raw_content} | "
            f"correlation_id={correlation_id}"
        )

        parsed = json.loads(raw_content)
        result = ClassificationResult(**parsed)

        # Build enriched search query from intent + search terms
        enriched_query = f"{query} {result.intent} {' '.join(result.search_terms)}"

        state["query_type"] = result.query_type
        state["object_name"] = enriched_query
        state["object_type"] = ""
        state["schema"] = result.schema_name
        state["target_variable"] = result.target_variable or ""
        state["warnings"] = []
        state["partial_flag"] = False

        # Phase 2 fields -- only non-empty for data-trace queries.
        state["phase2_filters"] = {
            "account_number": result.account_number,
            "mis_date": result.mis_date,
            "start_date": result.start_date,
            "end_date": result.end_date,
            "lob_code": result.lob_code,
            "lv_code": result.lv_code,
            "gl_code": result.gl_code,
            "branch_code": result.branch_code,
        }
        state["phase2_expected_value"] = result.expected_value
        state["phase2_actual_value"] = result.actual_value
        state["unsupported_reason"] = result.unsupported_reason or ""

        logger.info(
            f"Query classified: type={result.query_type}, "
            f"intent='{result.intent}', "
            f"target_variable={result.target_variable}, "
            f"search_terms={result.search_terms}, "
            f"schema={result.schema_name}, "
            f"confidence={result.confidence} | "
            f"correlation_id={correlation_id}"
        )
        return state


def extract_function_candidates(query: str) -> List[str]:
    """Return PL/SQL-looking identifiers from the query.

    Heuristic: a candidate must start with a letter, contain at least one
    underscore, and be at least 6 characters long. Further filters:
      * stopwords (known parameter/column names) are dropped
      * single-letter type-prefixed tokens (``N_...``, ``V_...``, ``F_...``)
        are dropped — OFSAA uses these for column names, never functions

    Case is preserved on the way out so callers can log the original
    spelling.
    """
    seen: set[str] = set()
    out: List[str] = []
    for match in _FUNCTION_NAME_CANDIDATE.finditer(query):
        cand = match.group(1)
        cand_upper = cand.upper()
        if cand_upper in seen:
            continue
        seen.add(cand_upper)
        if len(cand) < 6:
            continue
        if cand_upper in _NAME_STOPWORDS:
            continue
        if _COLUMN_TYPE_PREFIX.match(cand_upper):
            continue
        out.append(cand)
    return out


def function_exists_in_graph(
    function_name: str,
    redis_client,
    schemas: Optional[List[str]] = None,
) -> bool:
    """Return True if any of *schemas* holds a parsed graph for *function_name*.

    Lookup is case-insensitive on the function name (Redis keys are stored
    upper-cased by the loader). Returns False on any Redis exception so the
    caller can fail open rather than decline legitimate queries.
    """
    if redis_client is None:
        return False
    schemas = list(schemas) if schemas else list(_PRECHECK_SCHEMAS)
    func_upper = function_name.upper()
    for schema in schemas:
        try:
            if get_function_graph(redis_client, schema, func_upper) is not None:
                return True
        except Exception:
            continue
    return False


def find_similar_function_names(
    target: str,
    redis_client,
    schemas: Optional[List[str]] = None,
    top_n: int = 3,
) -> List[str]:
    """Return up to *top_n* graph function names similar to *target*.

    Scans ``graph:<schema>:<function_name>`` keys only (three-segment keys)
    and returns the closest matches by ratio. Empty list on Redis failure.
    """
    if redis_client is None:
        return []
    schemas = list(schemas) if schemas else list(_PRECHECK_SCHEMAS)
    all_names: set[str] = set()
    for schema in schemas:
        try:
            cursor = 0
            pattern = f"graph:{schema}:*"
            while True:
                cursor, keys = redis_client.scan(
                    cursor=cursor, match=pattern, count=200
                )
                for k in keys:
                    key_str = k.decode("utf-8") if isinstance(k, (bytes, bytearray)) else k
                    parts = key_str.split(":")
                    # Three-segment keys only — skip graph:full:<schema>,
                    # graph:source:<schema>:<fn>, graph:meta:..., graph:aliases:...
                    if len(parts) == 3 and parts[0] == "graph":
                        all_names.add(parts[2])
                if cursor == 0:
                    break
        except Exception:
            continue
    if not all_names:
        return []
    return get_close_matches(
        target.upper(), list(all_names), n=top_n, cutoff=0.5
    )


def build_function_not_found_response(
    requested_function: str,
    similar_functions: List[str],
    correlation_id: str,
) -> Dict[str, Any]:
    """Assemble a DECLINED response for a query that names a function we don't have.

    The frontend renders the message as a single-block markdown response; the
    structured fields let automated checks assert the DECLINED outcome.
    """
    parts = [
        f"The function `{requested_function}` was not found in the loaded graph.",
        "",
        "RTIE can only explain functions that have been indexed. If you believe "
        "this function should be available, verify the file exists under "
        "`db/modules/<module>/functions/` and that the module is configured.",
    ]
    if similar_functions:
        parts.append("")
        parts.append("Did you mean one of these?")
        for name in similar_functions:
            parts.append(f"- `{name}`")
    message = "\n".join(parts)
    return {
        "type": "function_not_found",
        "status": "declined",
        "requested_function": requested_function,
        "similar_functions": similar_functions,
        "validated": False,
        "badge": "DECLINED",
        "confidence": 0.0,
        "source_citations": [],
        "message": message,
        "explanation": {"markdown": message, "summary": message[:200]},
        "correlation_id": correlation_id,
    }
