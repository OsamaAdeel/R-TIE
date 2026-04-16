"""
RTIE LangGraph Logic Graph.

Builds and compiles the unified semantic search StateGraph. All queries
go through: parse → semantic_search → fetch_multi → explain → validate → render.

Command queries (starting with '/') bypass the graph entirely and are
routed to cache_manager/indexer tools before graph execution.
"""

import asyncio
import os
from typing import Optional

from langgraph.graph import StateGraph, END
from langchain_core.runnables import RunnableConfig
from langchain_openai import OpenAIEmbeddings
from psycopg_pool import AsyncConnectionPool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from src.graph.state import LogicState
from src.agents.orchestrator import Orchestrator
from src.agents.metadata_interpreter import MetadataInterpreter
from src.agents.logic_explainer import LogicExplainer
from src.agents.variable_tracer import VariableTracer
from src.agents.validator import Validator
from src.agents.renderer import Renderer
from src.tools.vector_store import VectorStore
from src.logger import get_logger

logger = get_logger(__name__, concern="app")


def _extract_llm_config(config: RunnableConfig) -> dict:
    """Extract provider and model from the RunnableConfig.

    Args:
        config: The LangGraph RunnableConfig for the current invocation.

    Returns:
        Dict with 'provider' and 'model' keys (may be None).
    """
    configurable = config.get("configurable", {})
    return {
        "provider": configurable.get("provider"),
        "model": configurable.get("model"),
    }


def build_logic_graph(
    orchestrator: Orchestrator,
    metadata_interpreter: MetadataInterpreter,
    logic_explainer: LogicExplainer,
    variable_tracer: VariableTracer,
    validator: Validator,  # noqa: ARG001 — kept for compile_graph API compatibility
    renderer: Renderer,
    vector_store: Optional[VectorStore] = None,
) -> StateGraph:
    """Build the RTIE unified semantic search StateGraph.

    Queries flow through: parse → search → fetch → (branch) → validate → render.
    The branch routes to either variable_trace or explain_semantic based on
    the query_type set by the orchestrator.

    Args:
        orchestrator: The Orchestrator agent instance.
        metadata_interpreter: The MetadataInterpreter agent instance.
        logic_explainer: The LogicExplainer agent instance.
        variable_tracer: The VariableTracer agent instance.
        validator: The Validator agent instance.
        renderer: The Renderer agent instance.
        vector_store: Redis vector store for semantic search. Optional.

    Returns:
        A LangGraph StateGraph ready for compilation.
    """

    async def parse_query(state: LogicState, config: RunnableConfig) -> LogicState:
        """Classify the user query and extract search terms.

        Args:
            state: Current pipeline state.
            config: LangGraph config with provider/model.

        Returns:
            Updated state with enriched search query in object_name.
        """
        logger.info(f"[parse_query] Classifying: {state['raw_query'][:80]}...")
        llm_cfg = _extract_llm_config(config)
        return await orchestrator.classify_query(
            state["raw_query"], state,
            provider=llm_cfg["provider"],
            model=llm_cfg["model"],
        )

    async def semantic_search(state: LogicState) -> LogicState:
        """Perform vector similarity search to find relevant functions.

        Embeds the enriched query and searches Redis for the top-K
        most relevant indexed functions.

        Args:
            state: Current pipeline state with enriched query in object_name.

        Returns:
            Updated state with search_results list.
        """
        logger.info("[semantic_search] Searching for relevant functions...")

        if not vector_store:
            state["search_results"] = []
            state["warnings"] = state.get("warnings", []) + [
                "Vector store not available — no semantic search results"
            ]
            logger.warning("[semantic_search] Vector store not available")
            return state

        # Use the enriched query (original + intent + search terms)
        search_query = state.get("object_name", state["raw_query"])

        import ssl as _ssl
        import httpx as _httpx
        _ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        _ssl_ctx.maximum_version = _ssl.TLSVersion.TLSv1_2
        _ssl_ctx.load_default_certs()
        embeddings = OpenAIEmbeddings(
            model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
            http_client=_httpx.Client(verify=_ssl_ctx, timeout=60),
            http_async_client=_httpx.AsyncClient(verify=_ssl_ctx, timeout=60),
        )
        query_embedding = await embeddings.aembed_query(search_query)

        results = await vector_store.search(
            query_embedding=query_embedding,
            top_k=5,
        )

        state["search_results"] = results
        state["schema"] = state.get("schema") or "OFSMDM"

        logger.info(
            f"[semantic_search] Found {len(results)} results: "
            f"{[r['function_name'] for r in results]}"
        )
        return state

    async def fetch_multi_logic(state: LogicState) -> LogicState:
        """Fetch source code for all functions from semantic search results.

        Args:
            state: Current pipeline state with search_results.

        Returns:
            Updated state with multi_source dict.
        """
        logger.info(
            f"[fetch_multi_logic] Fetching source for "
            f"{len(state.get('search_results', []))} functions"
        )
        return await metadata_interpreter.fetch_multi_logic(state)

    async def explain_semantic(state: LogicState, config: RunnableConfig) -> LogicState:
        """Generate cross-function explanation for the user's query.

        Args:
            state: Current pipeline state with multi_source.
            config: LangGraph config with provider/model.

        Returns:
            Updated state with explanation dict.
        """
        logger.info("[explain_semantic] Generating cross-function explanation...")
        llm_cfg = _extract_llm_config(config)
        return await logic_explainer.explain_semantic(
            state,
            provider=llm_cfg["provider"],
            model=llm_cfg["model"],
        )

    async def variable_trace(state: LogicState, config: RunnableConfig) -> LogicState:
        """Trace a specific variable across multiple functions.

        Runs the pure Python extraction pipeline to build a compact
        transformation chain, then sends only that chain to the LLM.

        Args:
            state: Current pipeline state with multi_source.
            config: LangGraph config with provider/model.

        Returns:
            Updated state with explanation and variable_chain populated.
        """
        logger.info(
            f"[variable_trace] Tracing variable: "
            f"{state.get('target_variable', 'unknown')}"
        )
        llm_cfg = _extract_llm_config(config)
        return await variable_tracer.trace_variable(
            state,
            provider=llm_cfg["provider"],
            model=llm_cfg["model"],
        )

    def route_after_fetch(state: LogicState) -> str:
        """Route to variable_trace or explain_semantic based on query_type.

        Args:
            state: Current pipeline state.

        Returns:
            Node name to route to.
        """
        if state.get("query_type") == "VARIABLE_TRACE":
            logger.info("[route] Routing to variable_trace")
            return "variable_trace"
        logger.info("[route] Routing to explain_semantic")
        return "explain_semantic"

    async def output_validate(state: LogicState) -> LogicState:
        """Validate the explanation output.

        For semantic search, validates that mentioned functions exist in
        the multi_source results.

        Args:
            state: Current pipeline state with explanation and multi_source.

        Returns:
            Updated state with validated flag and confidence score.
        """
        logger.info("[output_validate] Validating output")

        explanation = state.get("explanation", {})
        multi_source = state.get("multi_source", {})
        warnings = list(state.get("warnings", []))

        # Check that referenced functions exist in search results
        known_functions = {name.upper() for name in multi_source.keys()}
        mentioned = []

        for fn in explanation.get("relevant_functions", []):
            name = fn.get("name", "")
            if name:
                mentioned.append(name)
                if name.upper() not in known_functions:
                    warnings.append(
                        f"Function '{name}' mentioned in explanation but not "
                        f"in search results"
                    )

        total = len(mentioned)
        resolved = sum(1 for m in mentioned if m.upper() in known_functions)
        confidence = resolved / total if total > 0 else 1.0

        state["validated"] = len(warnings) == 0
        state["confidence"] = round(confidence, 4)
        state["warnings"] = warnings
        state["call_tree"] = {}

        logger.info(
            f"[output_validate] validated={state['validated']}, "
            f"confidence={confidence:.4f}, "
            f"resolved={resolved}/{total}"
        )
        return state

    async def render_response(state: LogicState) -> LogicState:
        """Render the final structured response.

        Args:
            state: Fully populated pipeline state.

        Returns:
            Updated state with output dict.
        """
        logger.info("[render_response] Rendering final output")
        return await renderer.render_response(state)

    # Build the graph — with conditional branch after fetch
    graph = StateGraph(LogicState)

    graph.add_node("parse_query", parse_query)
    graph.add_node("semantic_search", semantic_search)
    graph.add_node("fetch_multi_logic", fetch_multi_logic)
    graph.add_node("explain_semantic", explain_semantic)
    graph.add_node("variable_trace", variable_trace)
    graph.add_node("output_validator", output_validate)
    graph.add_node("render_response", render_response)

    # Linear edges up to fetch
    graph.set_entry_point("parse_query")
    graph.add_edge("parse_query", "semantic_search")
    graph.add_edge("semantic_search", "fetch_multi_logic")

    # Conditional branch: variable trace or semantic explanation
    graph.add_conditional_edges(
        "fetch_multi_logic",
        route_after_fetch,
        {
            "variable_trace": "variable_trace",
            "explain_semantic": "explain_semantic",
        },
    )

    # Both branches converge at output_validator
    graph.add_edge("variable_trace", "output_validator")
    graph.add_edge("explain_semantic", "output_validator")
    graph.add_edge("output_validator", "render_response")
    graph.add_edge("render_response", END)

    logger.info("Semantic search graph built with 7 nodes and conditional routing")
    return graph


async def compile_graph(
    orchestrator: Orchestrator,
    metadata_interpreter: MetadataInterpreter,
    logic_explainer: LogicExplainer,
    variable_tracer: VariableTracer,
    validator: Validator,
    renderer: Renderer,
    postgres_dsn: str,
    vector_store: Optional[VectorStore] = None,
):
    """Compile the logic graph with PostgreSQL checkpointing.

    Args:
        orchestrator: The Orchestrator agent instance.
        metadata_interpreter: The MetadataInterpreter agent instance.
        logic_explainer: The LogicExplainer agent instance.
        variable_tracer: The VariableTracer agent instance.
        validator: The Validator agent instance.
        renderer: The Renderer agent instance.
        postgres_dsn: PostgreSQL connection string for persistence.
        vector_store: Redis vector store for semantic search.

    Returns:
        A compiled, checkpointed graph ready for invocation.
    """
    graph = build_logic_graph(
        orchestrator=orchestrator,
        metadata_interpreter=metadata_interpreter,
        logic_explainer=logic_explainer,
        variable_tracer=variable_tracer,
        validator=validator,
        renderer=renderer,
        vector_store=vector_store,
    )

    pool = AsyncConnectionPool(conninfo=postgres_dsn, open=False)
    await pool.open()

    checkpointer = AsyncPostgresSaver(pool)
    try:
        await checkpointer.setup()
    except Exception as exc:
        logger.warning(f"Checkpointer setup skipped (already initialized): {exc}")

    compiled = graph.compile(checkpointer=checkpointer)
    logger.info("Logic graph compiled with PostgreSQL checkpointer")
    return compiled
