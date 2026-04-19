"""
RTIE LangGraph State Definition.

Defines the typed state dictionary that flows through every node in the
logic explanation graph. Each agent reads from and writes to this shared
state, building up the complete analysis incrementally.
"""

from typing import TypedDict, Optional


class LogicState(TypedDict):
    """Typed state dictionary for the RTIE LangGraph pipeline.

    Attributes:
        session_id: Unique session identifier for conversation continuity.
        correlation_id: Request-scoped UUID for end-to-end tracing.
        raw_query: The original user query string.
        query_type: Classification result — 'COLUMN_LOGIC' or 'COMMAND'.
        object_name: Name of the PL/SQL object being analyzed.
        object_type: Type of object — FUNCTION, PROCEDURE, T2T, or RRF.
        schema: Oracle schema containing the object (e.g. OFSMDM).
        source_code: Lines of PL/SQL source from ALL_SOURCE.
        call_tree: Nested dictionary of function dependencies.
        cache_hit: Whether the source was served from Redis cache.
        cache_stale: Whether the cached version is outdated vs Oracle.
        explanation: Structured LLM explanation of the logic.
        validated: Whether the explanation passed output validation.
        confidence: Confidence score from 0.0 to 1.0.
        warnings: List of validation warnings or issues.
        output: Final rendered response dictionary.
        partial_flag: Whether the response is incomplete.
    """

    # Input
    session_id: str
    correlation_id: str
    raw_query: str
    # Orchestrator output
    query_type: str
    object_name: str
    object_type: str
    schema: str
    # Fetch output
    source_code: list
    call_tree: dict
    cache_hit: bool
    cache_stale: bool
    # Explanation output
    explanation: dict
    # Validation output
    validated: bool
    confidence: float
    warnings: list
    # Semantic search output
    search_results: list
    multi_source: dict
    # Variable trace output
    target_variable: str
    variable_chain: dict
    # Graph pipeline output
    llm_payload: str
    graph_node_ids: list
    graph_available: bool
    # Phase 2 fields (data-trace queries)
    phase2_filters: dict
    phase2_expected_value: Optional[float]
    phase2_actual_value: Optional[float]
    # Populated only when query_type == "UNSUPPORTED"
    unsupported_reason: str
    # Final output
    output: dict
    partial_flag: bool
