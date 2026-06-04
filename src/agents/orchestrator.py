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
from src.parsing.store import get_column_index, get_function_graph, get_literal_index
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

# W58.a: Table-prefix exclusions (OFSAA convention). Tables in the Capital
# Structure / Risk schemas use these multi-letter prefixes and are never
# function names. Real function names like ABL_CAP_MITIGANT_DATA_POPULATION,
# CAPITAL_STD_ACCT_HEAD_POP, FN_LOAD_OPS_RISK_DATA, T2T_FCT_CCP_DETAILS_*
# don't start with any of these prefixes — only their referenced tables do.
_TABLE_NAME_PREFIXES = (
    "FCT_",     # fact tables (FCT_OPS_RISK_DATA, FCT_ENTITY_INFO)
    "DIM_",     # dimension tables (DIM_BASEL_METHODOLOGY, DIM_DATES)
    "STG_",     # staging tables (STG_OPS_RISK_DATA, STG_GL_DATA)
    "FSI_",     # framework / setup / interim tables (FSI_CAP_*)
    "SETUP_",   # setup tables
    "AAI_",     # OFSAA application infrastructure tables
)

# W58.b: OFSAA-generated internal alias / CASE-label patterns. These look like
# function names (uppercase + underscore + length ≥ 6) but are actually local
# identifiers inside generated MERGE/SELECT bodies — column aliases, CASE
# labels, MERGE source/target qualifiers — and never name a callable function.
_INTERNAL_ALIAS_PATTERNS = tuple(re.compile(p) for p in (
    r"^EXP_\d",     # EXP_10, EXP_11, EXP_1470990981178_10, etc.
    r"^COND_\d",    # COND_10, COND_1470990981178_10, etc.
    r"^T_\d",       # T_1470990981178_0, T_5, etc.
    r"^SS_",        # SS_<...> subquery aliases
    r"^TT_",        # TT_<...> MERGE target aliases
))

# W58.c: Column-prefix exclusions (OFSAA Hungarian-style typing). Defense in
# depth — most of these are already caught by the single-letter regex
# _COLUMN_TYPE_PREFIX above (e.g. N_X, V_X, F_X), but listing them
# explicitly covers edge cases like T_<digit> that the regex misses, and
# documents the intent for future readers.
_COLUMN_NAME_PREFIXES = (
    "N_",   # numeric (N_EOP_BAL, N_ANNUAL_GROSS_INCOME, N_SHAREHOLDING_PERCENT)
    "V_",   # varchar (V_LV_CODE, V_STD_ACCT_HEAD_ID, V_GL_CODE)
    "F_",   # flag (F_CAP_CONSL_ENTITY_IND, F_REGULATORY_ENTITY_IND)
    "D_",   # date (D_FINANCIAL_YEAR, D_CALENDAR_DATE)
    "I_",   # indicator/integer
    "T_",   # timestamp (also covered by _INTERNAL_ALIAS_PATTERNS for T_<digit>)
)

# W58.d: manifest process and sub_process names. Populated by main.py once
# all batch manifests have been loaded into Redis (see
# ``set_process_subprocess_names`` / ``refresh_process_subprocess_names``).
# Empty fallback means W58.d is a no-op until populated, which is the right
# default for unit tests that don't care about the manifest set and for
# pre-startup paths.
_PROCESS_SUBPROCESS_NAMES: frozenset[str] = frozenset()

# W76: prefix patterns that explicitly anchor a question inside a named
# PL/SQL function. When matched, the named function becomes the
# asked-about object regardless of what the classifier returned for
# target_variable / object_name. The remaining query text is treated as
# a sub-target inside that function's body — useful when the classifier
# would otherwise mistake a CASE-branch alias literal (EXP_11, COND_5,
# etc.) for the asked-about object.
#
# Triggers:
#   "In <FunctionName>, ..."
#   "Inside <FunctionName>, ..."
#   "Within <FunctionName>, ..."
#   "In the function <FunctionName>, ..."
#   "Within the function <FunctionName>, ..."
#   "<FunctionName>'s ..."     (possessive form)
#
# Anchored at the start of the query (post-whitespace), case-insensitive
# on the keyword. The candidate name is captured as group "name". Suffix
# is required to disambiguate where the name ends — comma, colon, or a
# question word — to prevent the regex from greedily eating tokens past
# the function name.
_NAMED_FUNCTION_ANCHOR_PATTERNS = (
    re.compile(
        r"^\s*(?:in|inside|within)(?:\s+the\s+function)?\s+"
        r"(?P<name>[A-Za-z][A-Za-z0-9_]+)"
        r"(?=\s*[,:]|\s+(?:when|where|how|what|why|which|if))",
        re.IGNORECASE,
    ),
    re.compile(
        r"^\s*(?P<name>[A-Za-z][A-Za-z0-9_]+)'s\s+",
        re.IGNORECASE,
    ),
)


def set_process_subprocess_names(names) -> None:
    """Replace the W58.d exclusion set with *names* (uppercased).

    Called by main.py once after every batch manifest has been parsed and
    stored, and by tests that need a known set without depending on a live
    Redis client. ``names`` may be any iterable of strings.
    """
    global _PROCESS_SUBPROCESS_NAMES
    _PROCESS_SUBPROCESS_NAMES = frozenset(n.upper() for n in names if n)


def refresh_process_subprocess_names(redis_client) -> frozenset[str]:
    """Rebuild the W58.d exclusion set from every stored batch hierarchy.

    Reads from the canonical ``hierarchy:<batch>`` keys via
    ``store.get_all_process_and_subprocess_names``. Returns the new set so
    callers can log its size.
    """
    from src.parsing.store import get_all_process_and_subprocess_names
    names = get_all_process_and_subprocess_names(redis_client)
    set_process_subprocess_names(names)
    return names

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

    def apply_column_provenance_anchor(self, state: LogicState) -> LogicState:
        """Instance-level convenience for the module-level helper.

        Forwards to :func:`apply_column_provenance_anchor` using the
        injected graph redis client. Safe to call without injection —
        the underlying helper short-circuits when ``redis_client`` is
        None (mirrors :meth:`apply_bi_routing`'s no-op contract).
        """
        return apply_column_provenance_anchor(
            state,
            state.get("raw_query", "") or "",
            self._graph_redis_client,
        )

    def apply_named_function_anchor(self, state: LogicState) -> LogicState:
        """W76 — anchor on an explicit function name when the raw query
        starts with ``"In <FunctionName>, ..."`` (or ``Inside`` / ``Within``
        / possessive variants), or when the classifier put an alias
        literal into ``target_variable`` while the user's query mentions
        a real function elsewhere.

        Defends against the classifier mistaking CASE-branch alias
        literals (``EXP_11``, ``COND_5``, …) for the asked-about object
        — those are local identifiers inside a generated SQL body, not
        callable functions or column traces.

        Mutates and returns *state*. Idempotent: with no anchor and no
        recoverable function, leaves state untouched (apart from
        clearing an alias-literal ``target_variable`` so the downstream
        variable tracer doesn't chase it globally).

        On fire stamps:

          * ``state["object_name"]`` — the resolved function name (so
            downstream consumers that read object_name as a function
            identifier — metadata fetch, validator, renderer — pick up
            the right body).
          * ``state["query_type"]`` — promoted to ``COLUMN_LOGIC``
            when it would otherwise have been ``VARIABLE_TRACE`` /
            empty, so the graph pipeline takes the function-lookup
            path instead of the variable-trace path that would chase
            an alias literal globally.
          * ``state["target_variable"]`` — cleared if the classifier
            put an alias literal there. The alias is a sub-target
            inside the anchored function's body, not a top-level
            column the variable tracer should chase.
          * ``state["w76_anchor"]`` — diagnostic record of what the
            rule did (which function it anchored on, what it
            overrode).
        """
        raw = state.get("raw_query", "") or ""
        if not raw:
            return state

        # Mechanism 1 — explicit "In <FunctionName>, ..." prefix.
        anchor_name = detect_named_function_anchor(raw)
        anchor_source = "prefix" if anchor_name else None

        original_query_type = state.get("query_type", "")
        original_target = (state.get("target_variable") or "").strip()
        target_upper = original_target.upper()
        target_is_alias = bool(original_target) and any(
            p.match(target_upper) for p in _INTERNAL_ALIAS_PATTERNS
        )

        # Mechanism 2 — alias-literal fallback. The classifier put a
        # CASE-branch alias in target_variable; try to recover the
        # enclosing function from elsewhere in the query.
        if anchor_name is None and target_is_alias:
            candidates = extract_function_candidates(raw)
            if candidates:
                anchor_name = candidates[0]
                anchor_source = "alias_fallback"
                logger.info(
                    "apply_named_function_anchor (M2): alias literal %s in "
                    "target_variable; anchoring on candidate %s from query",
                    original_target, anchor_name,
                )
            else:
                # Alias literal with no enclosing function context. Clear
                # target_variable so the variable tracer doesn't chase
                # an alias globally; downstream W57 catches will surface
                # NAMED_FUNCTION_NOT_RETRIEVED.
                state["target_variable"] = ""
                state["w76_anchor"] = {
                    "function": "",
                    "alias_literal_cleared": original_target,
                    "reason": "alias literal with no enclosing function",
                    "source": "alias_fallback_no_function",
                }
                logger.info(
                    "apply_named_function_anchor (M2): cleared alias-literal "
                    "target_variable %s — no enclosing function in query",
                    original_target,
                )
                return state

        if not anchor_name:
            return state

        state["object_name"] = anchor_name

        # Promote into the FUNCTION_LOGIC path so the graph pipeline
        # treats *anchor_name* as the function-of-interest. COLUMN_LOGIC
        # is what the live classifier emits for the same shape.
        if original_query_type in ("", "VARIABLE_TRACE"):
            state["query_type"] = "COLUMN_LOGIC"

        # Drop alias-literal target_variable values — they're sub-
        # targets inside the anchored function, not columns to trace.
        if target_is_alias:
            state["target_variable"] = ""

        state["w76_anchor"] = {
            "function": anchor_name,
            "source": anchor_source,
            "original_query_type": original_query_type,
            "original_target_variable": original_target,
        }

        logger.info(
            "apply_named_function_anchor: anchored on %s "
            "(source=%s, was query_type=%s target_variable=%r)",
            anchor_name, anchor_source, original_query_type, original_target,
        )
        return state

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
            Updated LogicState with query_type, schema, target_variable,
            and phase2 fields populated. W80: does NOT set object_name —
            that field is owned by the W76 anchor / BI routing post-passes,
            which produce clean function names rather than the classifier's
            synthesised search blob.
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

        # W80: classify_query must NOT write object_name. The classifier's
        # synthesised intent + search_terms used to be concatenated with the
        # raw query and stamped here, but that blob poisoned the vector-
        # search embedding for anchorless queries (cf. stakeholder test 2's
        # significant-investment trace). object_name is now populated only by
        # apply_named_function_anchor (W76, line 526) or apply_bi_routing
        # (line 1163); embedding sites fall back to raw_query when neither
        # fired. The blob is not stored anywhere — discovery confirmed no
        # consumer reads it.
        state["query_type"] = result.query_type
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
      * W58.a: table-prefixed tokens (``FCT_``, ``DIM_``, ``STG_``,
        ``FSI_``, ``SETUP_``, ``AAI_``) are dropped — these name tables
      * W58.b: OFSAA-generated alias patterns (``EXP_<digit>``,
        ``COND_<digit>``, ``T_<digit>``, ``SS_*``, ``TT_*``) are dropped
        — these are local identifiers inside generated SQL bodies
      * W58.c: multi-character column-prefix tokens are dropped as
        defense-in-depth alongside the single-letter regex
      * W58.d: manifest process and sub_process names are dropped — these
        are workflow labels users mention to scope a question, never
        callable PL/SQL functions. The set is populated at startup from
        every loaded batch hierarchy.

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
        # W58.a: OFSAA table-name prefixes.
        if any(cand_upper.startswith(p) for p in _TABLE_NAME_PREFIXES):
            continue
        # W58.c: OFSAA column-name prefixes (defense-in-depth).
        if any(cand_upper.startswith(p) for p in _COLUMN_NAME_PREFIXES):
            continue
        # W58.b: OFSAA-generated internal alias / CASE-label patterns.
        if any(p.match(cand_upper) for p in _INTERNAL_ALIAS_PATTERNS):
            continue
        # W58.d: manifest process and sub_process names.
        if cand_upper in _PROCESS_SUBPROCESS_NAMES:
            continue
        out.append(cand)
    return out


def detect_named_function_anchor(query: str) -> Optional[str]:
    """Return the function name when *query* explicitly anchors itself
    inside a named PL/SQL function via an "In <FunctionName>, ..." style
    prefix, otherwise ``None``.

    Recognises (case-insensitive on the keyword):

      * ``"In <NAME>[,:|when|where|how|what|why|which|if]"``
      * ``"Inside <NAME>..."`` / ``"Within <NAME>..."``
      * ``"In/Within the function <NAME>..."``
      * ``"<NAME>'s ..."``  (possessive)

    Returns ``None`` when no pattern matches OR when the captured name
    fails the W58 exclusion gates (table prefix, internal alias regex,
    column prefix, manifest process name, stopword) — those tokens are
    never callable PL/SQL function names. Also requires at least one
    underscore in the name and a minimum length of 6 to avoid binding
    to bare keywords like ``CASE`` or ``SELECT``.

    Reuses the W58 pattern constants read-only — W76 does not modify
    candidate-extraction filters.
    """
    if not query:
        return None
    for pat in _NAMED_FUNCTION_ANCHOR_PATTERNS:
        m = pat.match(query)
        if not m:
            continue
        name = m.group("name")
        if not name or len(name) < 6:
            continue
        # Bare uppercase keywords ("CASE", "SELECT", "WHERE") never name
        # a function — require at least one underscore.
        if "_" not in name:
            continue
        name_upper = name.upper()
        if name_upper in _NAME_STOPWORDS:
            continue
        if _COLUMN_TYPE_PREFIX.match(name_upper):
            continue
        if any(name_upper.startswith(p) for p in _TABLE_NAME_PREFIXES):
            continue
        if any(name_upper.startswith(p) for p in _COLUMN_NAME_PREFIXES):
            continue
        if any(p.match(name_upper) for p in _INTERNAL_ALIAS_PATTERNS):
            continue
        if name_upper in _PROCESS_SUBPROCESS_NAMES:
            continue
        return name
    return None


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


def w155_named_functions_in_query(
    raw_query: str,
    redis_client: Any,
) -> List[str]:
    """Return upper-cased corpus functions named in *raw_query*.

    W155. A "corpus function" is a function-looking token from
    :func:`extract_function_candidates` that actually resolves to a parsed
    graph via :func:`function_exists_in_graph`. Non-existent tokens (e.g.
    ``SOME_FAKE_FN``) are dropped — this is the deliberate W155 scope limit:
    the association gate only engages when a *real* function is named.

    Fails open: a ``None`` redis client makes ``function_exists_in_graph``
    return ``False`` for every candidate, so the result is an empty list and
    the caller's gate is skipped.
    """
    return sorted({
        c.upper()
        for c in extract_function_candidates(raw_query)
        if function_exists_in_graph(c, redis_client)
    })


def w155_cap_associated_with_named_fn(
    cap_code: str,
    named_funcs: List[str],
    redis_client: Any,
) -> bool:
    """Return True if *cap_code* is computed/contained by any *named_funcs*.

    W155. Uses the canonical registry CAP (``cap_code``) against the literal
    index via :func:`resolve_bi_to_function`, and tests whether any of the
    named corpus functions appears among that CAP's candidate functions.

    Membership — NOT ``resolve is None`` — is the correct test: every anchor
    registry CAP resolves to *some* function (e.g. CAP169 ->
    ABL_CAPITAL_SOURCE_STANDARD_ACCT_HEAD_DATA_POP), so ``resolve is None``
    would never catch the bluff. We require the *named* function to be one of
    the CAP's literal-index candidates.

    Fails open: a ``None`` redis client makes ``resolve_bi_to_function``
    return ``None`` -> empty candidate set -> returns ``False`` here, but the
    caller skips the gate before reaching this when ``named_funcs`` is empty;
    when ``named_funcs`` is non-empty and redis is None the gate would fall
    through. In practice ``named_funcs`` is itself empty under a None client
    (see :func:`w155_named_functions_in_query`), so the gate is skipped.
    """
    if not named_funcs:
        return False
    resolved = resolve_bi_to_function(cap_code, redis_client)
    cap_fns = {
        (c.get("function") or "").upper()
        for c in (resolved or {}).get("candidates", [])
    }
    return bool(set(named_funcs) & cap_fns)


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


# ---------------------------------------------------------------------------
# Column-provenance routing — extend BI-routing's pre-search lookup to columns
# ---------------------------------------------------------------------------
#
# A query like "How is N_EOP_BAL written / populated?" names a *column*, not a
# CAP-code and not a function. BI routing (CAP codes) and the W76 named-function
# anchor both decline on it, so pre-fix it fell through to unanchored narrow
# semantic search (top_k=5) and retrieved functions that never write the column
# — the LLM then fabricated a relationship between name-similar siblings.
#
# This pass mirrors apply_bi_routing's mechanism: a deterministic pre-search
# index lookup that, when the query names a known column, resolves the
# column's WRITER function(s) and routes the query to the VARIABLE_TRACE path
# (top_k=20, writer/INSERT-aware tracer) with the writer set force-included
# into retrieval (see anchor_resolution.ensure_column_writers_in_search_results).
#
# Direction-awareness is load-bearing. The column index (graph:index:<schema>)
# is direction-BLIND — it registers a column under a node whether the column is
# written or merely read (builder.build_function_column_index). Anchoring on a
# *reader* would reproduce the original bug. So writer classification is done
# per-node from the structured graph: a node WRITES a column only when the
# column is one of that node's structured write targets (INSERT columns list /
# mapping keys, UPDATE assignment targets, MERGE when-matched/not-matched
# targets, SCALAR_COMPUTE output_variable) — NEVER a source expression. The
# operation LABEL is produced by reusing VariableTracer._classify_operation
# rather than duplicating INSERT/UPDATE detection here (see
# _classify_node_operation).

# Query types the column-provenance pass may fire for. Mirrors the entity-
# seeking set; DATA_QUERY / VALUE_TRACE / DIFFERENCE_EXPLANATION / UNSUPPORTED
# are routed before this pass's call site (main.py) and are excluded here so
# the logic_graph call site (no early-returns) stays in lockstep.
_COLUMN_PROVENANCE_QUERY_TYPES = frozenset({
    "COLUMN_LOGIC", "VARIABLE_TRACE", "FUNCTION_LOGIC",
})

# Operation labels (as emitted by VariableTracer._classify_operation) that
# count as writing a value into the target column.
_WRITE_OPERATIONS = frozenset({"INSERT", "UPDATE", "MERGE", "SELECT_INTO"})

# Canonical statement heads per structured node type, fed to
# _classify_operation so the operation label comes from the existing detector
# rather than a duplicated keyword check here. SCALAR_COMPUTE maps to a
# SELECT ... INTO head so it classifies as SELECT_INTO.
_NODE_TYPE_CANONICAL = {
    "INSERT": "INSERT INTO {t}",
    "UPDATE": "UPDATE {t} SET col = val",
    "MERGE": "MERGE INTO {t}",
    "SCALAR_COMPUTE": "SELECT x INTO {t}",
}

# Column token in a query: OFSAA single-letter type prefix + underscore + caps
# (N_EOP_BAL, F_CAP_CONSL_ENTITY_IND, V_LV_CODE). Matched case-sensitively —
# columns are uppercase by convention. Validated against _COLUMN_TYPE_PREFIX so
# multi-letter table prefixes (STG_, FCT_, DIM_) never match.
_COLUMN_TOKEN_CANDIDATE = re.compile(r"\b([A-Z]_[A-Z0-9]+(?:_[A-Z0-9]+)*)\b")


def detect_column_tokens(text: str) -> List[str]:
    """Return OFSAA column-shaped tokens from *text*, first occurrence wins.

    A token qualifies when it matches the single-letter type-prefix shape
    (``N_``, ``V_``, ``F_``, ``D_``, ``I_``, ``T_`` + caps) — the same shape
    ``extract_function_candidates`` *excludes*. Here we *select* those tokens
    for the column-provenance lookup path only; the answer-grounding W58.c
    exclusions are untouched. Multi-letter table prefixes (``STG_``, ``FCT_``)
    do not match ``_COLUMN_TYPE_PREFIX`` and are dropped.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for m in _COLUMN_TOKEN_CANDIDATE.finditer(text):
        tok = m.group(1).upper()
        if tok in seen:
            continue
        seen.add(tok)
        if _COLUMN_TYPE_PREFIX.match(tok):
            out.append(tok)
    return out


def _node_target_columns(node: Dict[str, Any]) -> set:
    """Return the UPPERCASE set of columns *node* writes to (its targets only).

    Reads the structured write-target fields and DELIBERATELY ignores source
    expressions / values / conditions — that asymmetry is what makes the
    classification writer-aware rather than reader/writer-blind:

      * INSERT: ``column_maps["columns"]`` list + ``column_maps["mapping"]``
        keys (the values half of the mapping is the source — excluded);
        plus the same for each ``union_arms`` arm.
      * UPDATE: ``column_maps["assignments"]`` target column, or the keys of a
        flat ``{col: expr}`` map (expr values are sources — excluded).
      * MERGE: the ``when_matched`` / ``when_not_matched`` clause maps' targets.
      * SCALAR_COMPUTE: ``output_variable``.
    """
    targets: set = set()

    def _add_from_maps(cm: Any) -> None:
        if not isinstance(cm, dict):
            return
        if "mapping" in cm:
            for col in cm.get("columns") or []:
                if isinstance(col, str) and col.strip():
                    targets.add(col.strip().upper())
            mapping = cm.get("mapping")
            if isinstance(mapping, dict):
                for key in mapping.keys():
                    if isinstance(key, str) and key.strip():
                        targets.add(key.strip().upper())
        elif "assignments" in cm:
            for pair in cm.get("assignments") or []:
                if isinstance(pair, (list, tuple)) and pair:
                    col = pair[0]
                    if isinstance(col, str) and col.strip():
                        targets.add(col.strip().upper())
        else:
            # Flat {col: expr} (UPDATE) or a bare INSERT columns/values dict.
            for col in cm.get("columns") or []:
                if isinstance(col, str) and col.strip():
                    targets.add(col.strip().upper())
            for key in cm.keys():
                if isinstance(key, str) and key.strip() and key not in ("columns", "values"):
                    targets.add(key.strip().upper())

    _add_from_maps(node.get("column_maps"))
    for arm in node.get("union_arms") or []:
        if isinstance(arm, dict):
            _add_from_maps(arm.get("column_maps"))
    for clause_key in ("when_matched", "when_not_matched"):
        clause = node.get(clause_key)
        if isinstance(clause, dict):
            _add_from_maps(clause.get("column_maps"))
    output_var = node.get("output_variable")
    if isinstance(output_var, str) and output_var.strip():
        targets.add(output_var.strip().upper())
    return targets


def _classify_node_operation(node: Dict[str, Any]) -> str:
    """Return the write-operation label for *node*, or ``""``.

    Reuses :meth:`VariableTracer._classify_operation` (the existing operation
    detector) by feeding it a canonical statement head built from the node's
    structured ``type`` — so the INSERT/UPDATE/MERGE/SELECT_INTO taxonomy is
    single-sourced and not duplicated here. ``_classify_operation`` does not
    use ``self``, so it is invoked unbound with a ``None`` receiver.
    """
    ntype = (node.get("type") or "").upper()
    template = _NODE_TYPE_CANONICAL.get(ntype)
    if template is None:
        return ""
    target = node.get("target_table") or node.get("output_variable") or "T"
    canonical = template.format(t=target).upper()
    # Lazy import keeps the orchestrator import-graph narrow and avoids any
    # future import cycle (variable_tracer does not import orchestrator today).
    from src.agents.variable_tracer import VariableTracer
    return VariableTracer._classify_operation(None, canonical, [])


def _column_write_operation(
    column_upper: str,
    node: Dict[str, Any],
) -> Optional[str]:
    """Return the write-operation label if *node* writes *column_upper*, else None.

    Direction is decided by structured target membership
    (:func:`_node_target_columns`); the label is classified via
    :func:`_classify_node_operation`. Recurses into loop sub-nodes
    (``inner_node`` for WHILE_LOOP, ``inner_operations`` for FOR_LOOP) because
    the global column index registers loop-body columns under the OUTER loop
    node's id (builder.build_function_column_index).
    """
    if not isinstance(node, dict):
        return None
    if column_upper in _node_target_columns(node):
        op = _classify_node_operation(node)
        if op in _WRITE_OPERATIONS:
            return op
    inner = node.get("inner_node")
    if isinstance(inner, dict):
        found = _column_write_operation(column_upper, inner)
        if found:
            return found
    for inner_op in node.get("inner_operations") or []:
        if isinstance(inner_op, dict):
            found = _column_write_operation(column_upper, inner_op)
            if found:
                return found
    return None


def resolve_column_writers(
    column: str,
    redis_client: Any,
    schemas: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Resolve *column* to the function(s) that WRITE it via the column index.

    Reads ``graph:index:<schema>`` for each schema in scope, groups the
    direction-blind ``"FN:node_id"`` entries by function, then confirms each
    candidate function actually writes the column by inspecting the structured
    graph node (:func:`_column_write_operation`). Reader-only references are
    excluded — a function that merely SELECTs the column is never returned.

    Returns a list of ``{function, schema, operation}`` dicts, deduped by
    (schema, function). Empty list when:
      * *column* is empty or *redis_client* is None
      * no schema's index lists the column
      * the column is referenced only by readers (no writer)
    Any Redis / deserialization failure degrades to skipping that schema so
    the pass fails open (no exception leaked) — mirrors the
    ``resolve_bi_to_function`` contract.
    """
    col_upper = (column or "").strip().upper()
    if not col_upper or redis_client is None:
        return []
    if schemas is None:
        schemas = discovered_schemas(redis_client)
    if not schemas:
        return []

    writers: List[Dict[str, Any]] = []
    seen: set = set()
    for schema in schemas:
        try:
            index = get_column_index(redis_client, schema)
        except Exception as exc:
            logger.warning(
                "resolve_column_writers: column-index read failed for %s: %s",
                schema, exc,
            )
            continue
        if not index:
            continue
        entries = index.get(col_upper)
        if not entries:
            continue

        fn_to_node_ids: Dict[str, set] = {}
        for entry in entries:
            if not isinstance(entry, str) or ":" not in entry:
                continue
            fn_name, node_id = entry.split(":", 1)
            fn_to_node_ids.setdefault(fn_name, set()).add(node_id)

        for fn_name, node_ids in fn_to_node_ids.items():
            key = (schema, fn_name.upper())
            if key in seen:
                continue
            try:
                graph = get_function_graph(redis_client, schema, fn_name.upper())
            except Exception:
                graph = None
            if not graph:
                continue
            nodes_by_id = {
                n.get("id"): n
                for n in graph.get("nodes", [])
                if isinstance(n, dict)
            }
            operation: Optional[str] = None
            for node_id in node_ids:
                node = nodes_by_id.get(node_id)
                if node is None:
                    continue
                operation = _column_write_operation(col_upper, node)
                if operation:
                    break
            if operation:
                seen.add(key)
                writers.append({
                    "function": fn_name,
                    "schema": schema,
                    "operation": operation,
                })
    return writers


def apply_column_provenance_anchor(
    state: LogicState,
    raw_query: str,
    redis_client: Any,
) -> LogicState:
    """Conditionally route a column-provenance query to the writer/trace path.

    Idempotent and safe: when any precondition fails the state is returned
    unchanged. Mutates and also returns *state* for chainable callers.

    Fires when ALL of:
      - ``redis_client`` is available
      - ``state["query_type"]`` is in ``_COLUMN_PROVENANCE_QUERY_TYPES``
      - neither the W76 anchor nor BI routing already claimed the query
      - the user did NOT name a function from the indexed corpus (explicit
        choice wins — mirrors ``apply_bi_routing``)
      - a column token is present (the classifier's ``target_variable`` if it
        is column-shaped, else a column token scanned from *raw_query*)
      - that column resolves to a non-empty WRITER set via the column index

    On fire it stamps:
      - ``state["query_type"]`` → ``"VARIABLE_TRACE"`` (the writer/INSERT-aware
        trace path with top_k=20, not the generic top_k=5 explain path)
      - ``state["target_variable"]`` → the resolved column
      - ``state["schema"]`` → the writer schema when all writers share one
      - ``state["column_provenance"]`` → ``{column, writers, writer_functions}``
        which :func:`ensure_column_writers_in_search_results` force-includes
        into retrieval so the tracer's ``multi_source`` contains the writers.

    A column that resolves but has zero writers (reader-only), or a query that
    names no column, leaves the state untouched — never anchors on a reader.
    """
    if redis_client is None:
        return state
    if not raw_query:
        return state
    if state.get("query_type", "") not in _COLUMN_PROVENANCE_QUERY_TYPES:
        return state

    # Defer to upstream anchors that already claimed the query.
    w76 = state.get("w76_anchor") or {}
    if isinstance(w76, dict) and w76.get("function"):
        return state
    if state.get("bi_routing"):
        return state

    # Explicit-function-name override: the user named a real function — honour
    # the explicit choice rather than re-routing on a column it happens to
    # mention (mirrors apply_bi_routing's short-circuit).
    for cand in extract_function_candidates(raw_query):
        if function_exists_in_graph(cand, redis_client):
            return state

    # Candidate columns: classifier's target_variable first (if column-shaped),
    # then any column token scanned from the raw query.
    candidates: List[str] = []
    target_var = (state.get("target_variable") or "").strip().upper()
    if target_var and _COLUMN_TYPE_PREFIX.match(target_var):
        candidates.append(target_var)
    for tok in detect_column_tokens(raw_query):
        if tok not in candidates:
            candidates.append(tok)
    if not candidates:
        return state

    for column in candidates:
        writers = resolve_column_writers(column, redis_client)
        if not writers:
            continue

        original_query_type = state.get("query_type", "")
        state["query_type"] = "VARIABLE_TRACE"
        state["target_variable"] = column

        writer_schemas = {w["schema"] for w in writers}
        if len(writer_schemas) == 1:
            state["schema"] = next(iter(writer_schemas))

        state["column_provenance"] = {
            "column": column,
            "writers": writers,
            "writer_functions": [w["function"] for w in writers],
            "original_query_type": original_query_type,
        }

        logger.info(
            "apply_column_provenance_anchor: column %s -> writers %s "
            "(was query_type=%s, schemas=%s)",
            column,
            [f"{w['schema']}.{w['function']}({w['operation']})" for w in writers],
            original_query_type,
            sorted(writer_schemas),
        )
        return state

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


# ---------------------------------------------------------------------------
# W87 — Unrecognized-term clarification gate
# ---------------------------------------------------------------------------
#
# W87 fires when the orchestrator's entity-extraction paths all fail on an
# entity-seeking query type (FUNCTION_LOGIC / COLUMN_LOGIC / VARIABLE_TRACE).
# Without W87 the pipeline would pass the concatenated enriched_query blob
# from classify_query (line 669) into semantic search, which then ranks
# unrelated functions by name-similarity and the narrative LLM fabricates an
# anchor on one of them. Stakeholder-test-1 Q11 ("what is the threshold value
# for G Test") is the canonical failure case.
#
# Architectural sibling of W37 (function_not_found): pre-search, deterministic
# body, _stream_unrecognized_term_response emits it. Unlike W37 the badge is
# UNVERIFIED — the query was not declined, the system has simply asked the
# user to clarify what they meant.

# English question / command words that pass the W87 single-capitalized-token
# heuristic but are never the unknown term the user is asking about.
_W87_QUERY_STOPWORDS = frozenset({
    "WHAT", "HOW", "WHY", "WHEN", "WHERE", "WHICH", "WHO", "WHOSE",
    "FIND", "SHOW", "TELL", "EXPLAIN", "DESCRIBE", "GIVE", "LIST",
    "THE", "AND", "OR", "BUT", "FOR", "WITH", "FROM", "INTO", "OVER",
    "TRUE", "FALSE", "NULL", "NONE",
    # W127: calendar terms — months, days, quarters, period words.
    # Without these, "Where is the December-only gate set?" gets
    # "December" extracted as a synthetic identifier and declined by W87.
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE",
    "JULY", "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER",
    "JAN", "FEB", "MAR", "APR", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY",
    "FRIDAY", "SATURDAY", "SUNDAY",
    "MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN",
    "Q1", "Q2", "Q3", "Q4",
    "QUARTER", "YEAR", "MONTH", "WEEK", "DAY",
    "MONTHLY", "QUARTERLY", "ANNUALLY", "YEARLY", "ANNUAL",
})

# Query types that ask about a specific entity. W87 only fires on these —
# DATA_QUERY has its own clarification path, UNSUPPORTED short-circuits,
# VALUE_TRACE / DIFFERENCE_EXPLANATION route through Phase 2.
_W87_ENTITY_SEEKING_TYPES = frozenset({
    "FUNCTION_LOGIC", "COLUMN_LOGIC", "VARIABLE_TRACE",
})


def _w121b2_is_synthesized_target(
    target_variable: str,
    raw_query: str,
) -> bool:
    """W121-broad-2: detect classifier-synthesized compound target_variables.

    The LLM classifier is instructed to "extract the exact column/variable
    name" and tends to normalize prose like "LVE cap" / "RRP eligibility"
    into OFSAA-shaped column identifiers "LVE_CAP" / "RRP_ELIGIBILITY".
    W87 then declines on the synthesized form because no such column
    exists. Baseline: A4 / B1 of quality harness baseline; see
    scratch/w121b_empirical_finding.md for the data-flow trace.

    Returns True (reject as synthesized) when all hold:
      (a) target_variable contains an underscore (a join character)
      (b) target_variable does NOT appear (case-insensitive) in raw_query
          — the user did not type the joined form
      (c) every underscore-split token of target_variable appears as a
          standalone word in raw_query (case-insensitive, word-boundary)
    """
    if "_" not in target_variable:
        return False
    if target_variable.lower() in raw_query.lower():
        return False
    tokens = [t for t in target_variable.split("_") if t]
    if len(tokens) < 2:
        return False
    raw_lower = raw_query.lower()
    return all(
        re.search(rf"\b{re.escape(t.lower())}\b", raw_lower) is not None
        for t in tokens
    )


def _extract_unrecognized_term(
    raw_query: str,
    target_variable: str,
) -> Optional[str]:
    """Return the most plausible term the user is asking about, or None.

    Priority:
      1. The classifier's ``target_variable`` (its best guess at the entity
         the user named) — already extracted during classification, no need
         to re-derive. W121-broad-2 rejects target_variables that look
         synthesized from prose (see _w121b2_is_synthesized_target).
      2. The first quoted phrase in the raw query.
      3. The longest run of consecutive capitalized words.
      4. The longest single capitalized token that is not a query stopword
         (What, How, Find, ...).

    Returns None when no heuristic isolates a term — the caller treats this
    as "W87 cannot identify what the user meant, fall through to the
    existing classifier-partial_flag clarification path."
    """
    if target_variable and not _w121b2_is_synthesized_target(
        target_variable, raw_query,
    ):
        return target_variable.strip()
    if not raw_query:
        return None

    # Quoted phrase ("G Test", 'CAP973-equivalent', etc.).
    quoted = re.search(r'["\']([^"\']{2,80})["\']', raw_query)
    if quoted:
        candidate = quoted.group(1).strip()
        if candidate:
            return candidate

    # Multi-word capitalized run ("G Test", "Hypothetical Calculation").
    multiword = re.findall(
        r"\b(?:[A-Z][A-Za-z0-9_]*)(?:\s+[A-Z][A-Za-z0-9_]*)+\b",
        raw_query,
    )
    if multiword:
        return max(multiword, key=len).strip()

    # Single capitalized token, filtered against query stopwords.
    single = re.findall(r"\b([A-Z][A-Za-z0-9_]{2,})\b", raw_query)
    single = [t for t in single if t.upper() not in _W87_QUERY_STOPWORDS]
    if single:
        return max(single, key=len).strip()

    return None


def _generate_term_variations(term: str) -> List[str]:
    """Return up to 5 spelling variations of *term* a user might mean.

    Variations are presented in the W87 response body so the user knows
    which alternate spellings RTIE would have matched if they existed in
    the indexed corpus. The variations are NOT actually searched —
    listing them is honest about the kind of normalization RTIE would
    apply on a re-ask with the canonical spelling.
    """
    if not term:
        return []
    seen: set[str] = {term}
    out: List[str] = []

    def _maybe_add(v: str) -> None:
        if v and v not in seen:
            seen.add(v)
            out.append(v)

    upper = term.upper()
    # space <-> underscore swaps
    if " " in term:
        _maybe_add(term.replace(" ", "_"))
    if "_" in term:
        _maybe_add(term.replace("_", " "))
    # case
    _maybe_add(upper)
    # collapsed (no separators)
    collapsed = re.sub(r"[\s_]+", "", term)
    _maybe_add(collapsed)
    # _TEST suffix common in OFSAA validation routines
    if not upper.endswith("TEST"):
        _maybe_add(f"{upper.replace(' ', '_')}_TEST")
    return out[:5]


def _detect_unrecognized_term_query(
    state: LogicState,
    raw_query: str,
    redis_client,
) -> Optional[str]:
    """W87 gate. Return the unrecognized term when every entity-extraction
    path failed AND the classifier routed this as an entity-seeking query.

    Fires when ALL of the following hold:
      (a) ``state["query_type"]`` is FUNCTION_LOGIC / COLUMN_LOGIC /
          VARIABLE_TRACE
      (b) ``extract_function_candidates(raw_query)`` returned empty (so the
          W37 function precheck did NOT fire either)
      (c) BI routing did not fire — ``state["bi_routing"]`` is absent
      (d) The W76 named-function anchor did not fire — either
          ``state["w76_anchor"]`` is absent or its ``function`` field is
          empty
      (e) If the classifier set ``target_variable``, that column does NOT
          resolve in any indexed schema's column index

    Returns the extracted term, or None — None means "fall through, let the
    normal pipeline run" (W87 does not gate when no term can be isolated;
    that falls to the classifier-partial_flag clarification path).
    """
    if state.get("query_type") not in _W87_ENTITY_SEEKING_TYPES:
        return None
    if not raw_query:
        return None

    # (b) Function-name extraction. Non-empty list means W37 already had a
    # chance to fire and either fired (in which case we never reached
    # here) or the function exists and the pipeline should continue.
    if extract_function_candidates(raw_query):
        return None

    # (c) BI routing fired.
    if state.get("bi_routing"):
        return None

    # (d) W76 anchored on an explicit function.
    w76 = state.get("w76_anchor") or {}
    if w76.get("function"):
        return None

    # (e) Column resolves in some schema's index.
    target_var = (state.get("target_variable") or "").strip()
    if target_var and redis_client is not None:
        try:
            # Imported lazily to keep the orchestrator module import-graph
            # narrow — schema_discovery already imports store, and store
            # already imports orchestrator-adjacent helpers elsewhere.
            from src.parsing.schema_discovery import schemas_for_column
            if schemas_for_column(target_var, redis_client):
                return None
        except Exception as exc:
            # Lookup failure should not block the W87 gate — log and
            # treat as "did not resolve."
            logger.warning(
                "W87 column-resolution check failed for %r: %s",
                target_var, exc,
            )

    term = _extract_unrecognized_term(raw_query, target_var)
    if term is None:
        return None
    return term


def build_unrecognized_term_response(
    term: str,
    similar_functions: List[str],
    schemas_loaded: List[str],
    correlation_id: str,
) -> Dict[str, Any]:
    """Assemble the W87 UNVERIFIED clarification payload.

    Deterministic markdown — no LLM call. Mirrors W37's
    :func:`build_function_not_found_response` shape: the same SSE emitter
    delivers stage / meta / token / done events. The structural fields
    let automated checks assert the W87 outcome.
    """
    variations = _generate_term_variations(term)
    schemas_block = ", ".join(sorted(schemas_loaded)) if schemas_loaded else "(none discovered)"

    parts: List[str] = [
        f'## Unrecognized Term: "{term}"',
        "",
        f'I couldn\'t resolve "{term}" against the indexed corpus.',
        "",
        "### What I searched",
        "",
        f"- Loaded function names in {schemas_block}",
        f"- Column indexes across {schemas_block}",
        "- Business-identifier literal index (CAP codes)",
        "- Named-function anchor patterns (`In <FunctionName>, ...`)",
        "",
    ]
    if variations:
        parts.append(
            f'No match was found for "{term}". Common spelling variations '
            f"like {', '.join(repr(v) for v in variations)} would also have "
            "matched if they existed in the indexed corpus."
        )
        parts.append("")

    parts.extend([
        "### What you can do",
        "",
        f'If "{term}" is local terminology or shorthand, please clarify '
        "what it maps to:",
        "",
        "- A specific function name (e.g., `CS_Threshold_Treatment_*`)",
        "- A CAP code (e.g., `CAP973`)",
        "- A column name (e.g., `N_EOP_BAL_NPL`)",
        "- A standard account head code or other identifier RTIE indexes",
        "",
        "### Related items I searched",
        "",
    ])
    if similar_functions:
        for name in similar_functions:
            parts.append(
                f"- `{name}` — retrieved by name-similarity only; "
                "NOT the answer to your question."
            )
        parts.append("")
        parts.append(
            "(Listed in case one of them is what you meant. None were "
            f'confirmed to be the answer to "{term}".)'
        )
    else:
        parts.append("No close name-similarity matches found.")

    message = "\n".join(parts)
    return {
        "type": "unrecognized_term",
        "status": "unverified",
        "requested_term": term,
        "similar_functions": similar_functions,
        "validated": False,
        "badge": "UNVERIFIED",
        "confidence": 0.2,
        "source_citations": [],
        "warnings": [f"UNRECOGNIZED_TERM: '{term}' not in indexed corpus"],
        "schemas_searched": list(schemas_loaded) if schemas_loaded else [],
        "message": message,
        "explanation": {"markdown": message, "summary": message[:200]},
        "correlation_id": correlation_id,
    }


def build_near_twin_hedge_response(
    anchor_function: str,
    siblings: List[str],
    schemas_searched: List[str],
    correlation_id: str,
) -> Dict[str, Any]:
    """Assemble the W150 near-twin disambiguation hedge payload.

    Deterministic markdown — no LLM call. Emitted (via
    :func:`main._stream_near_twin_hedge_response`) INSTEAD of a confident
    explainer body when :func:`anchor_resolution.detect_near_twin_ambiguity`
    fires: the described query landed in a tight cluster of near-identical
    functions the embedding can't separate, so RTIE hedges UNVERIFIED rather
    than confidently describing the wrong twin. Mirrors the W87 unrecognized-
    term payload shape so the existing SSE emitter and the frontend's generic
    UNVERIFIED render path (badge + explanation.markdown) handle it with no
    frontend change.
    """
    sib_lines = "\n".join(f"- `{s}`" for s in siblings)
    parts: List[str] = [
        f"## Closest match: `{anchor_function}`",
        "",
        "This function sits in a tight cluster of near-identical functions, and "
        "your description didn't name one specifically. I can't confidently pick "
        "between these closely-related candidates:",
        "",
        sib_lines,
        "",
        "### What you can do",
        "",
        f"Re-ask naming the exact function — e.g. `In {anchor_function}, ...` — "
        "for a verified trace.",
    ]
    message = "\n".join(parts)
    n_sib = max(len(siblings) - 1, 0)
    return {
        "type": "near_twin_disambiguation",
        "status": "unverified",
        "anchor_function": anchor_function,
        "near_twin_siblings": list(siblings),
        "validated": False,
        "badge": "UNVERIFIED",
        "confidence": 0.2,
        "source_citations": [],
        "warnings": [
            "W150-NEAR-TWIN-AMBIGUOUS: described query matched a tight near-twin "
            f"cohort ({anchor_function} + {n_sib} sibling(s)); hedged rather than "
            "answering confidently"
        ],
        "schemas_searched": list(schemas_searched) if schemas_searched else [],
        "message": message,
        "explanation": {"markdown": message, "summary": message[:200]},
        "correlation_id": correlation_id,
    }
