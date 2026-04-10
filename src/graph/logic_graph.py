"""
RTIE LangGraph Logic Graph.

Builds and compiles the deterministic StateGraph that orchestrates the
full logic explanation pipeline. Nodes execute in a fixed linear order:
parse -> resolve -> fetch -> cache_validate -> dependencies -> explain
-> relevance_validate -> output_validate -> render.

Command queries (starting with '/') bypass the graph entirely and are
routed to cache_manager tools before graph execution.

LLM provider and model are passed through the graph config so each
request can select OpenAI or Anthropic dynamically.
"""

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langchain_core.runnables import RunnableConfig
from psycopg_pool import AsyncConnectionPool

from src.graph.state import LogicState
from src.agents.orchestrator import Orchestrator
from src.agents.metadata_interpreter import MetadataInterpreter
from src.agents.logic_explainer import LogicExplainer
from src.agents.validator import Validator
from src.agents.renderer import Renderer
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
    validator: Validator,
    renderer: Renderer,
    postgres_dsn: str,
) -> StateGraph:
    """Build and compile the RTIE logic explanation StateGraph.

    Constructs a deterministic linear graph where each node represents
    an agent operation. All edges are unconditional — command routing
    happens before graph invocation.

    Args:
        orchestrator: The Orchestrator agent instance.
        metadata_interpreter: The MetadataInterpreter agent instance.
        logic_explainer: The LogicExplainer agent instance.
        validator: The Validator agent instance.
        renderer: The Renderer agent instance.
        postgres_dsn: PostgreSQL connection string for the checkpointer.

    Returns:
        A compiled LangGraph StateGraph ready for invocation.
    """

    async def parse_query(state: LogicState, config: RunnableConfig) -> LogicState:
        """Classify the user query and extract object metadata.

        Args:
            state: Current pipeline state.
            config: LangGraph config with provider/model in configurable.

        Returns:
            Updated state with query_type, object_name, schema populated.
        """
        logger.info(f"[parse_query] Classifying query: {state['raw_query'][:80]}...")
        llm_cfg = _extract_llm_config(config)
        return await orchestrator.classify_query(
            state["raw_query"], state,
            provider=llm_cfg["provider"],
            model=llm_cfg["model"],
        )

    async def resolve_object(state: LogicState) -> LogicState:
        """Resolve the target object in Oracle metadata.

        Args:
            state: Current pipeline state with object_name.

        Returns:
            Updated state with object_type confirmed.
        """
        logger.info(f"[resolve_object] Resolving: {state['object_name']}")
        return await metadata_interpreter.resolve_object(state)

    async def fetch_logic(state: LogicState) -> LogicState:
        """Fetch PL/SQL source code from cache or Oracle.

        Args:
            state: Current pipeline state.

        Returns:
            Updated state with source_code and cache_hit.
        """
        logger.info(f"[fetch_logic] Fetching source for: {state['object_name']}")
        return await metadata_interpreter.fetch_logic(state)

    async def cache_validate(state: LogicState) -> LogicState:
        """Validate cache freshness against Oracle DDL timestamps.

        Args:
            state: Current pipeline state with cached data.

        Returns:
            Updated state with cache_stale flag.
        """
        logger.info("[cache_validate] Checking cache freshness")
        return await validator.cache_validator(state)

    async def fetch_dependencies(state: LogicState) -> LogicState:
        """Build the dependency call tree from source code analysis.

        Args:
            state: Current pipeline state with source_code.

        Returns:
            Updated state with call_tree populated.
        """
        logger.info(f"[fetch_dependencies] Scanning dependencies for: {state['object_name']}")
        return await metadata_interpreter.fetch_dependencies(state)

    async def explain_logic(state: LogicState, config: RunnableConfig) -> LogicState:
        """Generate structured LLM explanation of the PL/SQL logic.

        Args:
            state: Current pipeline state with source_code and call_tree.
            config: LangGraph config with provider/model in configurable.

        Returns:
            Updated state with explanation dict.
        """
        logger.info(f"[explain_logic] Explaining: {state['object_name']}")
        llm_cfg = _extract_llm_config(config)
        return await logic_explainer.explain_logic(
            state,
            provider=llm_cfg["provider"],
            model=llm_cfg["model"],
        )

    async def query_relevance_validate(state: LogicState) -> LogicState:
        """Validate that the explanation references the queried object.

        Args:
            state: Current pipeline state with explanation.

        Returns:
            Updated state with relevance warnings if any.
        """
        logger.info("[query_relevance_validate] Checking explanation relevance")
        return await validator.query_relevance_validator(state)

    async def output_validate(state: LogicState) -> LogicState:
        """Validate that all referenced functions exist in the call tree.

        Args:
            state: Current pipeline state with explanation and call_tree.

        Returns:
            Updated state with validated flag and confidence score.
        """
        logger.info("[output_validate] Validating output references")
        return await validator.output_validator(state)

    async def render_response(state: LogicState) -> LogicState:
        """Render the final structured response.

        Args:
            state: Fully populated pipeline state.

        Returns:
            Updated state with output dict.
        """
        logger.info("[render_response] Rendering final output")
        return await renderer.render_response(state)

    # Build the graph
    graph = StateGraph(LogicState)

    # Add nodes
    graph.add_node("parse_query", parse_query)
    graph.add_node("resolve_object", resolve_object)
    graph.add_node("fetch_logic", fetch_logic)
    graph.add_node("cache_validator", cache_validate)
    graph.add_node("fetch_dependencies", fetch_dependencies)
    graph.add_node("explain_logic", explain_logic)
    graph.add_node("query_relevance_validator", query_relevance_validate)
    graph.add_node("output_validator", output_validate)
    graph.add_node("render_response", render_response)

    # Add deterministic edges
    graph.set_entry_point("parse_query")
    graph.add_edge("parse_query", "resolve_object")
    graph.add_edge("resolve_object", "fetch_logic")
    graph.add_edge("fetch_logic", "cache_validator")
    graph.add_edge("cache_validator", "fetch_dependencies")
    graph.add_edge("fetch_dependencies", "explain_logic")
    graph.add_edge("explain_logic", "query_relevance_validator")
    graph.add_edge("query_relevance_validator", "output_validator")
    graph.add_edge("output_validator", "render_response")
    graph.add_edge("render_response", END)

    logger.info("Logic graph built with 9 nodes and deterministic edges")
    return graph


async def compile_graph(
    orchestrator: Orchestrator,
    metadata_interpreter: MetadataInterpreter,
    logic_explainer: LogicExplainer,
    validator: Validator,
    renderer: Renderer,
    postgres_dsn: str,
):
    """Compile the logic graph with PostgreSQL checkpointing.

    Args:
        orchestrator: The Orchestrator agent instance.
        metadata_interpreter: The MetadataInterpreter agent instance.
        logic_explainer: The LogicExplainer agent instance.
        validator: The Validator agent instance.
        renderer: The Renderer agent instance.
        postgres_dsn: PostgreSQL connection string for persistence.

    Returns:
        A compiled, checkpointed graph ready for invocation.
    """
    graph = build_logic_graph(
        orchestrator=orchestrator,
        metadata_interpreter=metadata_interpreter,
        logic_explainer=logic_explainer,
        validator=validator,
        renderer=renderer,
        postgres_dsn=postgres_dsn,
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
