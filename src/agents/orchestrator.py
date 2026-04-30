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
from src.llm_errors import sanitize_llm_exception
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id
from src.parsing.store import get_function_graph, get_literal_index
from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.literals import compile_patterns
from src.parsing.schema_discovery import discovered_schemas
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

# Schemas to check when resolving a function name are now discovered at
# runtime via src.parsing.schema_discovery.discovered_schemas(redis_client),
# which scans graph:* keys and falls back to manifest.RECOGNIZED_SCHEMAS
# only when Redis is empty / unavailable. Adding a new schema is now a
# loader/manifest concern, not a code change here.


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
                            * Reconciliation queries comparing values across two
                              tables (typically STG vs FCT) — phrased with
                              "differs from", "differs between", "doesn't match",
                              "reconcile X with Y", "X vs Y for account ...". A
                              bare aggregate / row query against an FCT_* table
                              in any discovered schema is NOT unsupported — it
                              routes as DATA_QUERY against the table's owning
                              schema.
                            * Forecasting / prediction ("likely to fail", "next quarter",
                              "forecast", "will X happen").
                            * Any other capability outside read-only introspection of
                              any discovered schema + its parsed graph.

Routing rules (apply in order):
 1. If the query contains forecasting / future-tense prediction language,
    OR reconciliation language comparing values across two tables ("STG
    vs FCT", "differs from", "doesn't match", "reconcile X with Y") ->
    UNSUPPORTED. Use unsupported_reason to name it. A bare reference to
    an FCT_* table without reconciliation phrasing is NOT a trigger —
    those route as DATA_QUERY against the table's owning schema.
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
- "What is the total N_STD_ACCT_HEAD_AMT in FCT_STANDARD_ACCT_HEAD on 2025-12-31?"
    -> query_type: "DATA_QUERY", target_variable: "N_STD_ACCT_HEAD_AMT",
       schema_name: "OFSERM", mis_date: "2025-12-31",
       start_date: null, end_date: null
       # FCT_* table named without reconciliation phrasing — answerable
       # as a single-table aggregate. Routes to OFSERM via Phase 4
       # schema pivot.
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
        # W35 Phase 7: optional graph Redis client and BI pattern config,
        # injected by main.py post-construction (the graph Redis client is
        # built after the orchestrator). Absence is non-fatal — BI routing
        # becomes a no-op when either is missing.
        self._graph_redis_client: Any = None
        self._bi_patterns: Optional[Dict[str, Any]] = None

    def set_redis_client(self, redis_client: Any) -> None:
        """Inject the graph Redis client used by BI routing.

        Wired post-construction from main.py because the graph Redis
        client is created later in the FastAPI lifespan than the
        Orchestrator itself. Optional — when absent
        :meth:`apply_bi_routing` short-circuits as a no-op.
        """
        self._graph_redis_client = redis_client

    def set_bi_patterns(self, patterns: Optional[Dict[str, Any]]) -> None:
        """Inject the ``business_identifier_patterns`` config.

        ``None`` (the default) tells :meth:`apply_bi_routing` to fall back
        to the default ``CAP\\d{3}`` pattern in
        :data:`src.parsing.literals.DEFAULT_BUSINESS_IDENTIFIER_PATTERNS`.
        """
        self._bi_patterns = patterns

    def apply_bi_routing(self, state: LogicState) -> LogicState:
        """Instance-level convenience for the module-level helper.

        Forwards to :func:`apply_bi_routing` using the injected redis
        client and pattern config. Safe to call without injection — the
        underlying helper short-circuits when ``redis_client`` is None.
        """
        return apply_bi_routing(
            state,
            state.get("raw_query", "") or "",
            self._graph_redis_client,
            self._bi_patterns,
        )

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

        try:
            with stage_timer("llm_api_classify", correlation_id, provider=(provider or "default")):
                response = await llm.ainvoke(messages)
        except Exception as exc:
            raise sanitize_llm_exception(
                exc, context="classify_query", correlation_id=correlation_id
            ) from exc
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
    schemas = list(schemas) if schemas else discovered_schemas(redis_client)
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
    schemas = list(schemas) if schemas else discovered_schemas(redis_client)
    all_names: set[str] = set()
    for schema in schemas:
        try:
            cursor = 0
            pattern = SchemaAwareKeyspace.graph_scan_pattern(schema)
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


# ---------------------------------------------------------------------------
# W35 Phase 7 — business-identifier (BI) routing
# ---------------------------------------------------------------------------
#
# BI routing turns a query like "How is CAP943 calculated?" into a routing
# decision *before* semantic search runs. It uses the graph:literal:<schema>:
# <id> index Phase 5 built (and the derivation summaries Phase 6 attached
# to case_when_target records) to pick the function that COMPUTES the
# identifier rather than the function that loads it.
#
# Role priority (most preferred first):
#   1. case_when_target with an embedded derivation
#   2. case_when_target without a derivation
#   3. case_when_source
#   4. in_list_member
#   5. filter
#
# BI routing only fires for COLUMN_LOGIC / FUNCTION_LOGIC queries — the
# logic-explainer paths. DATA_QUERY, VARIABLE_TRACE, VALUE_TRACE,
# DIFFERENCE_EXPLANATION, and UNSUPPORTED queries are left untouched. An
# explicitly-named function in the query (e.g. "How does
# CS_Deferred_Tax_... work?") also short-circuits BI routing — the user's
# explicit choice wins over the literal-index lookup.

# Routes BI fires for. COLUMN_LOGIC is what the live classifier emits;
# FUNCTION_LOGIC is the forward-compatible alias kept in
# logic_explainer._REQUIRES_CITATIONS.
_BI_ROUTING_QUERY_TYPES = frozenset({"COLUMN_LOGIC", "FUNCTION_LOGIC"})

# Role priority ordering — lower number wins. Mirrors the priority list in
# the Phase 7 prompt.
_BI_ROLE_PRIORITY = {
    "case_when_target": 1,    # +derivation: 0 (handled separately)
    "case_when_source": 2,
    "in_list_member": 3,
    "filter": 4,
}


def detect_business_identifiers(
    raw_query: str,
    patterns: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return business identifiers found in the user query, in order.

    Reuses the configured ``business_identifier_patterns`` block from
    settings.yaml (default ``CAP\\d{3}``) — same source as Phase 5's
    literal extraction, so the indexer and the router agree on what
    counts as a business identifier.

    Matching is case-sensitive: CAP-codes are uppercase by convention,
    so ``cap973`` does NOT match. Word boundaries on either side prevent
    matches inside larger tokens (``XCAP943Y`` does not match).

    Args:
        raw_query: The user's question string.
        patterns: Optional ``business_identifier_patterns`` dict (same
            shape ``compile_patterns`` accepts). When ``None`` or empty,
            the default ``CAP\\d{3}`` pattern set is used.

    Returns:
        Ordered list of identifier strings. Duplicates are removed,
        first occurrence wins, ordering follows the user's query.
        Empty list when no patterns are configured or no matches found.
    """
    compiled = compile_patterns(patterns)
    if not compiled or not raw_query:
        return []

    seen: set[str] = set()
    found: list[tuple[int, str]] = []
    for pat in compiled:
        # Wrap the bare regex in word boundaries for query-side detection
        # (literals.py uses string-quote anchors for SQL-side detection).
        try:
            search_re = re.compile(rf"\b(?:{pat.raw_regex})\b")
        except re.error:
            continue
        for m in search_re.finditer(raw_query):
            ident = m.group(0)
            if ident in seen:
                continue
            seen.add(ident)
            found.append((m.start(), ident))
    found.sort(key=lambda x: x[0])
    return [ident for _, ident in found]


def _bi_record_priority(record: Dict[str, Any]) -> tuple:
    """Return a sort key — lower tuples win. Used by resolve_bi_to_function.

    Tie-breakers: function name (alphabetical) then line number, both
    ascending — deterministic across reloads.
    """
    role = record.get("role", "")
    role_rank = _BI_ROLE_PRIORITY.get(role, 99)
    has_derivation = bool(record.get("derivation"))
    # case_when_target with derivation beats case_when_target without.
    derivation_rank = 0 if (role == "case_when_target" and has_derivation) else 1
    fn = (record.get("function") or "").upper()
    line = record.get("line") or 0
    return (role_rank, derivation_rank, fn, line)


def resolve_bi_to_function(
    identifier: str,
    redis_client: Any,
    schemas: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve *identifier* to its best-match function via the literal index.

    Reads ``graph:literal:<schema>:<identifier>`` for each schema in scope,
    flattens the records, and picks the highest-priority record by role.

    Args:
        identifier: The business identifier (e.g. ``"CAP943"``).
        redis_client: Active Redis client used to read the literal index.
        schemas: Optional list of schemas to restrict the lookup to.
            When ``None``, every discovered schema is scanned.

    Returns:
        Dict with keys ``function``, ``schema``, ``role``, ``derivation``,
        and ``candidates`` (the full list of records considered, sorted by
        priority — useful for logging / debugging). ``derivation`` is the
        embedded summary Phase 6 attached to case_when_target records, or
        ``None`` when the routed record has no derivation.

        Returns ``None`` when:
          - ``identifier`` is empty or ``redis_client`` is None
          - the identifier is absent from every in-scope schema's index
          - the schemas list is empty (caller-restricted to no schemas)
    """
    if not identifier or redis_client is None:
        return None
    if schemas is None:
        schemas = discovered_schemas(redis_client)
    if not schemas:
        return None

    candidates: list[tuple[tuple, str, Dict[str, Any]]] = []
    for schema in schemas:
        try:
            records = get_literal_index(redis_client, schema, identifier)
        except Exception as exc:
            logger.warning(
                "resolve_bi_to_function: literal-index read failed for %s.%s: %s",
                schema, identifier, exc,
            )
            continue
        if not records:
            continue
        for rec in records:
            candidates.append((_bi_record_priority(rec), schema, rec))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0])
    _, primary_schema, primary = candidates[0]
    derivation = primary.get("derivation")

    return {
        "function": primary.get("function", ""),
        "schema": primary_schema,
        "role": primary.get("role", ""),
        "derivation": dict(derivation) if isinstance(derivation, dict) else None,
        "candidates": [
            {"schema": sch, **rec} for _, sch, rec in candidates
        ],
    }


def apply_bi_routing(
    state: LogicState,
    raw_query: str,
    redis_client: Any,
    patterns: Optional[Dict[str, Any]] = None,
) -> LogicState:
    """Conditionally rewrite *state* to route through a BI-resolved function.

    Idempotent and safe to call: when any precondition fails the state is
    returned unchanged. Mutates and also returns *state* for callers that
    prefer a chainable form.

    Fires when ALL of:
      - ``state["query_type"]`` is in ``{COLUMN_LOGIC, FUNCTION_LOGIC}``
      - the user did NOT name a function from the indexed corpus in
        ``raw_query`` (explicit choice wins)
      - at least one configured business identifier appears in *raw_query*
      - the first such identifier resolves via the literal index

    On fire it stamps:
      - ``state["bi_routing"]`` — the resolved record (identifier,
        function, schema, role, derivation)
      - ``state["object_name"]`` — overridden to the resolved function so
        the graph pipeline / source retrieval load the right body
      - ``state["schema"]`` — overridden to the resolved schema

    Off-fire (any precondition fails) the state is left untouched.

    Args:
        state: Current pipeline state. Mutated in place.
        raw_query: The user's original query string.
        redis_client: Active graph Redis client. ``None`` short-circuits
            the call (no BI routing — pipeline runs unchanged).
        patterns: Optional ``business_identifier_patterns`` config block.
            ``None`` uses the default ``CAP\\d{3}`` pattern.

    Returns:
        The same state dict, possibly with bi_routing/object_name/schema
        rewritten.
    """
    if redis_client is None:
        return state
    if not raw_query:
        return state

    qt = state.get("query_type", "")

    # Decide where to look for the identifier and whether to promote.
    promoted_from_variable_trace = False
    if qt in _BI_ROUTING_QUERY_TYPES:
        # COLUMN_LOGIC / FUNCTION_LOGIC: scan the user's whole query for
        # configured business identifiers.
        identifiers = detect_business_identifiers(raw_query, patterns)
    elif qt == "VARIABLE_TRACE":
        # TODO(W36-followup): The classifier rule "VARIABLE_TRACE: how is X
        # calculated" routes CAP-code queries here despite their being
        # formula-definition questions. This branch corrects that downstream
        # by promoting query_type to FUNCTION_LOGIC when the VARIABLE_TRACE
        # target is a business identifier. A cleaner fix would amend the
        # classifier prompt to route CAP-code-shaped targets to COLUMN_LOGIC /
        # FUNCTION_LOGIC directly. Deferred to keep classifier prompt changes
        # out of Phase 7's scope and avoid LLM-determinism regressions.
        target_var = (state.get("target_variable") or "").strip()
        if not target_var:
            return state
        # Gate strictly on the target_variable matching a BI pattern — a
        # query like "what writes N_EOP_BAL" must NOT fire BI routing,
        # only CAP-code-shaped targets should.
        identifiers = detect_business_identifiers(target_var, patterns)
        if not identifiers:
            return state
        promoted_from_variable_trace = True
    else:
        return state

    # Explicit-function-name override: if the query mentions a function
    # that exists in the indexed corpus, preserve the user's choice.
    candidates = extract_function_candidates(raw_query)
    if candidates:
        for cand in candidates:
            if function_exists_in_graph(cand, redis_client):
                logger.info(
                    "apply_bi_routing: explicit function %s named in query — "
                    "skipping BI routing",
                    cand,
                )
                return state

    if not identifiers:
        return state

    primary = identifiers[0]
    resolved = resolve_bi_to_function(primary, redis_client)
    if resolved is None:
        logger.info(
            "apply_bi_routing: identifier %s not found in any literal index",
            primary,
        )
        return state

    bi_routing = {
        "identifier": primary,
        "function": resolved["function"],
        "schema": resolved["schema"],
        "role": resolved["role"],
        "derivation": resolved.get("derivation"),
    }
    state["bi_routing"] = bi_routing

    # Stamp routing target so semantic search / graph pipeline pick this
    # function rather than relying on enriched-string ranking. The schema
    # override is a happy by-product that fixes the classifier-default
    # OFSMDM mis-routing for OFSERM-only identifiers (CAP-codes live in
    # OFSERM, but the classifier defaults schema to OFSMDM when it sees
    # no other signal — without this override the graph pipeline would
    # query graph:index:OFSMDM for an identifier that lives in
    # graph:literal:OFSERM, miss every time, and fall back to a wrong
    # answer).
    state["object_name"] = resolved["function"]
    state["schema"] = resolved["schema"]

    if promoted_from_variable_trace:
        # Promote the classifier's verdict. The literal-index hit is
        # stronger downstream evidence than the classifier's regex-style
        # "how is X calculated -> VARIABLE_TRACE" rule, and the
        # variable-tracer agent would otherwise miss the derivation
        # banner because the streaming endpoint branches on query_type.
        state["query_type"] = "FUNCTION_LOGIC"
        logger.info(
            "apply_bi_routing: promoted VARIABLE_TRACE -> FUNCTION_LOGIC "
            "for BI target %s (classifier mis-routed CAP-code-shaped "
            "target_variable)",
            primary,
        )

    logger.info(
        "apply_bi_routing: %s -> %s.%s (role=%s, derivation=%s)",
        primary,
        resolved["schema"],
        resolved["function"],
        resolved["role"],
        "yes" if resolved.get("derivation") else "no",
    )
    return state


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
