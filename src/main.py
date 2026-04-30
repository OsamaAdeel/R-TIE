"""
RTIE FastAPI Application.

Provides the HTTP API layer for the Regulatory Trace & Intelligence Engine.
Endpoints include POST /v1/query for logic explanation, GET /health for
dependency status checks, and GET /v1/models for listing available LLM
providers. All queries flow through semantic vector search.
"""

import asyncio
import json as json_mod
import os
import platform
import traceback
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

# Note: ProactorEventLoop (Windows default) is used for httpx compatibility.
# psycopg uses psycopg-binary which handles the event loop internally.

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.agents.orchestrator import (
    Orchestrator,
    extract_function_candidates,
    function_exists_in_graph,
    find_similar_function_names,
    build_function_not_found_response,
)
from src.agents.metadata_interpreter import MetadataInterpreter
from src.agents.logic_explainer import (
    LogicExplainer,
    detect_ungrounded_identifiers,
    detect_partial_source_function,
    evaluate_grounding,
    render_derivation_header,
)
from src.agents.variable_tracer import VariableTracer
from src.agents.value_tracer import ValueTracerAgent
from src.agents.data_query import DataQueryAgent
from src.agents.validator import Validator
from src.agents.cache_manager import CacheManager
from src.agents.indexer import IndexerAgent
from src.agents.renderer import Renderer
from src.pipeline.logic_graph import compile_graph
from src.pipeline.state import LogicState
from src.parsing.query_engine import (
    resolve_query_to_nodes,
    fetch_nodes_by_ids,
    fetch_relevant_edges,
    determine_execution_order,
    assemble_llm_payload,
)
from src.parsing.schema_discovery import (
    discovered_schemas,
    fallback_to_default_schema,
    identifier_grounded_in_any_schema,
    schema_for_function,
    schemas_for_column,
)
from src.tools.schema_tools import SchemaTools
from src.tools.cache_tools import CacheClient
from src.tools.vector_store import VectorStore
from src.monitoring.health import HealthChecker
from src.middleware.correlation_id import CorrelationIdMiddleware, get_correlation_id
from src.llm_factory import list_available_models, get_default_provider, get_default_model
from src.llm_errors import (
    LLMSanitizedError,
    build_declined_response,
    GENERIC_LLM_ERROR_MESSAGE,
)
from src.logger import get_logger
from src.telemetry import stage_timer, mark_event
import yaml

logger = get_logger(__name__, concern="app")
_w43_diag = get_logger("rtie.w43_diag", concern="app")

# Load environment based on ENVIRONMENT variable
env = os.getenv("ENVIRONMENT", "dev")
load_dotenv(f".env.{env}")

# LangSmith: langchain auto-enables tracing when LANGSMITH_TRACING=true
# and LANGSMITH_API_KEY is set. No extra wiring needed — this log just
# surfaces the state at boot so misconfig is visible.
if os.getenv("LANGSMITH_TRACING", "").lower() == "true" and os.getenv("LANGSMITH_API_KEY"):
    # Older langchain builds still read LANGCHAIN_* — mirror for safety.
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", os.environ["LANGSMITH_API_KEY"])
    os.environ.setdefault("LANGCHAIN_PROJECT", os.getenv("LANGSMITH_PROJECT", "RTIE"))
    os.environ.setdefault("LANGCHAIN_ENDPOINT", os.getenv("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com"))


def _load_settings() -> Dict[str, Any]:
    """Load and merge YAML configuration files.

    Returns:
        Merged configuration dictionary.
    """
    config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")

    with open(os.path.join(config_dir, "settings.yaml"), "r", encoding="utf-8") as f:
        base = yaml.safe_load(f)

    env_file = os.path.join(config_dir, f"settings.{env}.yaml")
    if os.path.exists(env_file):
        with open(env_file, "r", encoding="utf-8") as f:
            env_overrides = yaml.safe_load(f) or {}
        base = _deep_merge(base, env_overrides)

    return base


def _deep_merge(base: dict, overrides: dict) -> dict:
    """Recursively merge two dictionaries.

    Args:
        base: The base dictionary.
        overrides: Dictionary with values to overlay.

    Returns:
        Merged dictionary with overrides applied.
    """
    result = base.copy()
    for key, value in overrides.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


# Global references for app state
_schema_tools: SchemaTools = None
_cache_client: CacheClient = None
_vector_store: VectorStore = None
_orchestrator: Orchestrator = None
_metadata_interpreter: MetadataInterpreter = None
_logic_explainer: LogicExplainer = None
_variable_tracer: VariableTracer = None
_value_tracer: ValueTracerAgent = None
_data_query: DataQueryAgent = None
_validator: Validator = None
_cache_manager: CacheManager = None
_indexer: IndexerAgent = None
_renderer: Renderer = None
_compiled_graph = None
_graph_available: bool = False
_graph_redis = None
_health_checker: HealthChecker = None
_settings: Dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup and shutdown.

    Initializes all agents, connection pools, vector store, and the
    LangGraph pipeline on startup. Auto-indexes configured modules.
    Cleans up connections on shutdown.

    Args:
        app: The FastAPI application instance.
    """
    global _schema_tools, _cache_client, _vector_store
    global _orchestrator, _metadata_interpreter, _logic_explainer
    global _variable_tracer, _value_tracer, _data_query, _validator, _cache_manager, _indexer, _renderer
    global _compiled_graph, _health_checker, _settings, _graph_available, _graph_redis

    _settings = _load_settings()
    oracle_cfg = _settings["oracle"]
    redis_cfg = _settings["redis"]
    llm_cfg = _settings["llm"]
    embedding_cfg = _settings.get("embedding", {})

    # Initialize Oracle connection pool
    _schema_tools = SchemaTools(
        host=os.getenv("ORACLE_HOST"),
        port=int(os.getenv("ORACLE_PORT", "1521")),
        sid=os.getenv("ORACLE_SID"),
        user=os.getenv("ORACLE_USER"),
        password=os.getenv("ORACLE_PASSWORD"),
        pool_min=oracle_cfg["pool_min"],
        pool_max=oracle_cfg["pool_max"],
    )
    await _schema_tools.initialize()

    # Initialize Redis cache client
    _cache_client = CacheClient(
        host=os.getenv("REDIS_HOST"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        key_prefix=redis_cfg["key_prefix"],
    )
    await _cache_client.connect()

    # Initialize Redis vector store
    _vector_store = VectorStore(
        host=os.getenv("REDIS_HOST"),
        port=int(os.getenv("REDIS_PORT", "6379")),
    )
    await _vector_store.connect()
    await _vector_store.ensure_index()

    # Initialize agents
    _orchestrator = Orchestrator(
        temperature=llm_cfg["temperature"],
        max_tokens=llm_cfg["max_tokens"],
    )

    _metadata_interpreter = MetadataInterpreter(
        schema_tools=_schema_tools,
        cache_client=_cache_client,
        default_schema=oracle_cfg["schema"],
    )

    _logic_explainer = LogicExplainer(
        temperature=llm_cfg["temperature"],
        max_tokens=llm_cfg["max_tokens"],
        langsmith_project=_settings["langsmith"]["project"],
    )

    _variable_tracer = VariableTracer(
        temperature=llm_cfg["temperature"],
        max_tokens=llm_cfg["max_tokens"],
    )

    # Phase 2 value tracer -- constructed after _graph_redis is set below,
    # see lifespan completion of graph pipeline initialisation.

    _validator = Validator(
        schema_tools=_schema_tools,
        cache_client=_cache_client,
    )

    _cache_manager = CacheManager(
        schema_tools=_schema_tools,
        cache_client=_cache_client,
    )

    _indexer = IndexerAgent(
        vector_store=_vector_store,
        embedding_model=os.getenv(
            "EMBEDDING_MODEL",
            embedding_cfg.get("model", "text-embedding-3-small"),
        ),
        llm_provider=embedding_cfg.get("description_provider", "openai"),
        llm_model=embedding_cfg.get("description_model", "gpt-4o"),
        temperature=llm_cfg["temperature"],
        max_tokens=llm_cfg["max_tokens"],
    )

    _renderer = Renderer()

    # PostgreSQL DSN for LangGraph checkpointer
    postgres_dsn = (
        f"postgresql://{os.getenv('POSTGRES_USER')}:{os.getenv('POSTGRES_PASSWORD')}"
        f"@{os.getenv('POSTGRES_HOST')}:{os.getenv('POSTGRES_PORT')}"
        f"/{os.getenv('POSTGRES_DB')}"
    )

    # Compile the LangGraph pipeline
    _compiled_graph = await compile_graph(
        orchestrator=_orchestrator,
        metadata_interpreter=_metadata_interpreter,
        logic_explainer=_logic_explainer,
        variable_tracer=_variable_tracer,
        validator=_validator,
        renderer=_renderer,
        postgres_dsn=postgres_dsn,
        vector_store=_vector_store,
    )

    # Initialize health checker
    _health_checker = HealthChecker(
        schema_tools=_schema_tools,
        cache_client=_cache_client,
        postgres_dsn=postgres_dsn,
    )

    # Load graph pipeline for PL/SQL function parsing
    graph_cfg = _settings.get("graph", {})
    _graph_available = False
    try:
        import redis as _redis
        from src.parsing.loader import load_all_functions, discover_module_folders
        from src.parsing.manifest import ManifestValidationError
        _graph_redis = _redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
        )
        # Wire the graph Redis client into the logic explainer so it can
        # look up batch/process hierarchy and prepend a one-line context
        # header on streamed explanations.
        _logic_explainer.set_redis_client(_graph_redis)

        # W35 Phase 7: wire the same client + business-identifier
        # pattern config into the orchestrator so BI routing
        # (apply_bi_routing) can read graph:literal:<schema>:<id>
        # without an additional plumbing layer.
        _orchestrator.set_redis_client(_graph_redis)
        _orchestrator.set_bi_patterns(
            _settings.get("business_identifier_patterns")
        )

        # Phase 3: same client into MetadataInterpreter so source
        # retrieval can read graph:source:<schema>:<fn> (the loader's
        # canonical source cache) before falling through to rtie:logic /
        # Oracle / disk. Without this wiring the Phase 1 chain runs
        # unchanged — fine for OFSMDM but it's why W49 fired for OFSERM.
        if _metadata_interpreter is not None:
            _metadata_interpreter.set_graph_redis_client(_graph_redis)

        # W38: auto-discover every module folder under db/modules/ that has a
        # functions/ subdirectory. Union with any explicit functions_dirs from
        # config so existing deployments keep working.
        modules_base = graph_cfg.get("modules_base_dir", "db/modules")
        discovered = discover_module_folders(modules_base)
        logger.info(
            "Discovered %d module folders: %s",
            len(discovered),
            [m["module_name"] for m in discovered],
        )
        for mod in discovered:
            logger.info(
                "Module %s: %d .sql files found",
                mod["module_name"], mod["sql_count"],
            )

        # Build the final load list: discovered modules first, then any
        # explicit functions_dirs from config that weren't already discovered.
        load_targets: list[tuple[str, str]] = [
            (mod["module_name"], mod["functions_dir"]) for mod in discovered
        ]
        seen_dirs = {os.path.abspath(t[1]) for t in load_targets}
        for fn_dir in graph_cfg.get("functions_dirs", []):
            abs_dir = os.path.abspath(fn_dir)
            if abs_dir in seen_dirs:
                continue
            seen_dirs.add(abs_dir)
            # Derive a module name from the path for log consistency.
            mod_name = os.path.basename(os.path.dirname(abs_dir)) or fn_dir
            load_targets.append((mod_name, fn_dir))

        # W35 Phase 5: pass the business-identifier pattern config from
        # settings.yaml so the loader can build the per-schema literal
        # index at graph:literal:<schema>:<identifier>. Default
        # (CAP\d{3}) applies when the block is absent.
        bi_patterns = _settings.get("business_identifier_patterns")

        for mod_name, fn_dir in load_targets:
            result = load_all_functions(
                functions_dir=fn_dir,
                schema=oracle_cfg["schema"],
                redis_client=_graph_redis,
                force_reparse=graph_cfg.get("force_reparse_on_startup", False),
                business_identifier_patterns=bi_patterns,
            )
            logger.info(
                "Module %s: loaded %d, skipped %d, failed %d (status=%s)",
                mod_name,
                result["functions_parsed"],
                result["functions_skipped"],
                result["functions_failed"],
                result["status"],
            )
            if result["status"] in ("success", "partial"):
                _graph_available = True

        if _graph_available:
            from src.phase2.origins_catalog import build_catalog
            # Phase 2: build per-schema catalogs for every schema the loader
            # populated. build_catalog(redis) without schema iterates
            # discovered_schemas() and returns a {schema: OriginsCatalog}
            # dict; per-schema build failures are logged but do not abort
            # the iteration.
            catalogs = build_catalog(_graph_redis)
            for sch, cat in catalogs.items():
                logger.info(
                    "Origins catalog built for %s: "
                    "%d PLSQL origins, %d ETL origins, "
                    "%d blocked GL codes, %d EOP overrides",
                    sch,
                    len(cat.plsql_origins),
                    len(cat.etl_origins),
                    len(cat.gl_block_list),
                    len(cat.gl_eop_overrides),
                )
    except ManifestValidationError as exc:
        # A malformed manifest is a developer error: refuse to start so the
        # broken module is fixed rather than silently loaded from cache.
        logger.error("Manifest validation failed: %s", exc)
        raise
    except Exception as exc:
        logger.warning(f"Graph pipeline failed (non-fatal): {exc}")

    # Phase 2 value tracer -- needs schema_tools, the sync Redis client
    # used by the graph pipeline, and SQLGuardian for SELECT validation.
    try:
        from src.tools.sql_guardian import SQLGuardian
        _value_tracer = ValueTracerAgent(
            schema_tools=_schema_tools,
            redis_client=_graph_redis,
            sql_guardian=SQLGuardian(),
            temperature=llm_cfg["temperature"],
            max_tokens=llm_cfg["max_tokens"],
        )
        logger.info("Phase 2 value tracer initialised")
    except Exception as exc:
        logger.warning(f"Phase 2 value tracer init failed (non-fatal): {exc}")

    # Data-query agent (Option A): handles aggregates + row-list questions
    # by generating a read-only SELECT through SQLGuardian.
    try:
        from src.tools.sql_guardian import SQLGuardian
        dq_cfg = (_settings.get("data_query") or {})
        _data_query = DataQueryAgent(
            schema_tools=_schema_tools,
            redis_client=_graph_redis,
            sql_guardian=SQLGuardian(),
            temperature=llm_cfg["temperature"],
            max_tokens=llm_cfg["max_tokens"],
            hard_row_limit=int(dq_cfg.get("hard_row_limit", 10_000)),
            warn_row_limit=int(dq_cfg.get("warn_row_limit", 100)),
            display_row_limit=int(dq_cfg.get("display_row_limit", 100)),
        )
        logger.info(
            "DataQueryAgent initialised (hard=%s, warn=%s, display=%s)",
            dq_cfg.get("hard_row_limit", 10_000),
            dq_cfg.get("warn_row_limit", 100),
            dq_cfg.get("display_row_limit", 100),
        )
    except Exception as exc:
        logger.warning(f"DataQueryAgent init failed (non-fatal): {exc}")

    # Prime the schema-type snapshot in Redis so DataQueryAgent can
    # render column data types in its LLM catalog and SQLGuardian can
    # reject CHAR bind comparisons. Non-fatal: on failure the catalog
    # falls back to name-only columns, matching pre-W33 behavior.
    # Phase 2: prime the schema-type snapshot for every discovered schema.
    # Pre-Phase-2 this only ran for `oracle_cfg["schema"]` (OFSMDM) so
    # DataQueryAgent's catalog had no OFSERM column types. Per-schema
    # failures are logged but never abort the loop — a transient OFSERM
    # outage must not prevent OFSMDM from priming.
    if _cache_manager is not None:
        snapshot_schemas = discovered_schemas(_graph_redis)
        for sch in snapshot_schemas:
            try:
                snap = await _cache_manager.refresh_schema_snapshot(sch)
                logger.info(
                    "Schema-type snapshot primed for %s (%s)",
                    sch,
                    snap.get("summary") if isinstance(snap, dict) else snap,
                )
            except Exception as exc:
                logger.warning(
                    "Schema snapshot refresh failed for %s at startup "
                    "(non-fatal): %s",
                    sch,
                    exc,
                )

    # Phase 3: auto-index every function the loader populated, across
    # every discovered schema. Reads from graph:<schema>:<fn> +
    # graph:source:<schema>:<fn> (Redis is the source of truth) rather
    # than re-walking disk — naturally honours the manifest's
    # active/inactive filter and so produces ~141 OFSERM embeddings
    # rather than 554. Per-schema failures are logged but never abort
    # the run.
    if _graph_redis is not None:
        try:
            result = await _indexer.index_all_loaded(
                _graph_redis, force=False
            )
            for sch, sch_result in (result.get("results") or {}).items():
                logger.info(
                    "Auto-index %s: %d indexed, %d skipped, %d errors",
                    sch,
                    sch_result.get("indexed", 0),
                    sch_result.get("skipped", 0),
                    sch_result.get("errors", 0),
                )
        except Exception as exc:
            logger.warning(
                f"Auto-indexing failed (non-fatal): {exc}"
            )
    else:
        logger.info(
            "Auto-indexing skipped — graph Redis client not available "
            "(loader did not run)."
        )

    logger.info(
        "LangSmith tracing: %s (project=%s)",
        "ENABLED" if os.getenv("LANGSMITH_TRACING", "").lower() == "true"
        and os.getenv("LANGSMITH_API_KEY") else "DISABLED",
        os.getenv("LANGSMITH_PROJECT", "RTIE"),
    )

    logger.info("RTIE application started successfully")
    yield

    # Shutdown
    if _graph_redis:
        _graph_redis.close()
    await _vector_store.close()
    await _cache_client.close()
    await _schema_tools.close()
    logger.info("RTIE application shut down cleanly")


app = FastAPI(
    title="RTIE — Regulatory Trace & Intelligence Engine",
    version="1.0.0",
    description=(
        "Read-only multi-agent AI system that explains regulatory capital "
        "computation logic from Oracle OFSAA FSAPPS."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CorrelationIdMiddleware)


class QueryRequest(BaseModel):
    """Request body for the /v1/query endpoint.

    Attributes:
        query: The user's natural language query or slash command.
        session_id: Unique session identifier for conversation continuity.
        engineer_id: Identifier for the requesting engineer.
        provider: LLM provider to use. Optional.
        model: Specific model name to use. Optional.
    """

    model_config = {"strict": True}

    query: str
    session_id: str
    engineer_id: str
    provider: Optional[str] = None
    model: Optional[str] = None


@app.post("/v1/query")
async def query_endpoint(request: QueryRequest, req: Request) -> Dict[str, Any]:
    """Process a logic query or slash command.

    All logic queries flow through the unified semantic search pipeline.
    Slash commands are routed directly to their handlers.

    Args:
        request: The query request body.
        req: The raw Starlette request for correlation ID.

    Returns:
        Full output dict from the pipeline, or command result.
    """
    correlation_id = get_correlation_id()
    provider = request.provider
    model = request.model

    logger.info(
        f"Query received: '{request.query[:80]}...' "
        f"session={request.session_id} "
        f"engineer={request.engineer_id} "
        f"provider={provider} model={model} | "
        f"correlation_id={correlation_id}"
    )

    try:
        # Check for slash commands
        cmd = _orchestrator.check_command(request.query)
        if cmd.is_command:
            result = await _handle_command(
                cmd.command, cmd.args, request.session_id
            )
            return {"type": "command", "result": result, "correlation_id": correlation_id}

        # Run the unified semantic search pipeline
        initial_state: LogicState = {
            "session_id": request.session_id,
            "correlation_id": correlation_id,
            "raw_query": request.query,
            "query_type": "",
            "object_name": "",
            "object_type": "",
            "schema": "",
            "source_code": [],
            "call_tree": {},
            "cache_hit": False,
            "cache_stale": False,
            "explanation": {},
            "validated": False,
            "confidence": 0.0,
            "warnings": [],
            "search_results": [],
            "multi_source": {},
            "target_variable": "",
            "variable_chain": {},
            "llm_payload": "",
            "graph_node_ids": [],
            "graph_available": _graph_available,
            "bi_routing": {},
            "output": {},
            "partial_flag": False,
        }

        config = {
            "configurable": {
                "thread_id": request.session_id,
                "provider": provider,
                "model": model,
            },
            "metadata": {
                "correlation_id": correlation_id,
                "engineer_id": request.engineer_id,
                "provider": provider,
                "model": model,
            },
            "tags": ["query", request.engineer_id],
        }

        final_state = await _compiled_graph.ainvoke(initial_state, config=config)

        logger.info(
            f"Query completed: "
            f"functions={list(final_state.get('multi_source', {}).keys())} "
            f"confidence={final_state.get('confidence', 0)} | "
            f"correlation_id={correlation_id}"
        )

        return final_state.get("output", {})

    except LLMSanitizedError as exc:
        logger.warning(
            "Query sanitized LLM failure | category=%s context=%s correlation_id=%s",
            exc.category, exc.context, exc.correlation_id or correlation_id,
        )
        declined = build_declined_response(
            exc.category, exc.user_message,
            correlation_id=exc.correlation_id or correlation_id,
            context=exc.context,
        )
        return JSONResponse(status_code=200, content=declined)
    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f"Query failed: {exc}\n{tb} | correlation_id={correlation_id}")
        return JSONResponse(
            status_code=500,
            content={
                "error": GENERIC_LLM_ERROR_MESSAGE,
                "correlation_id": correlation_id,
            },
        )


@app.post("/v1/stream")
async def stream_endpoint(request: QueryRequest, req: Request):
    """Stream a logic query response via Server-Sent Events.

    Runs the pipeline (classify, search, fetch) synchronously, then
    streams the LLM explanation tokens one chunk at a time. The frontend
    receives partial markdown and renders it incrementally.

    SSE event format:
        event: meta     → JSON with metadata (schema, functions, correlation_id)
        event: token    → partial markdown text chunk
        event: done     → final JSON with confidence, validated, citations
        event: error    → error message
    """
    correlation_id = get_correlation_id()
    provider = request.provider
    model = request.model

    async def event_stream():
        mark_event("request_arrived", correlation_id, endpoint="/v1/stream")
        try:
            # Check for slash commands — not streamable, return as single event
            cmd = _orchestrator.check_command(request.query)
            if cmd.is_command:
                result = await _handle_command(cmd.command, cmd.args, request.session_id)
                payload = {"type": "command", "result": result, "correlation_id": correlation_id}
                yield f"event: done\ndata: {json_mod.dumps(payload)}\n\n"
                return

            # Run the pipeline up to (but not including) the LLM explanation
            initial_state: LogicState = {
                "session_id": request.session_id,
                "correlation_id": correlation_id,
                "raw_query": request.query,
                "query_type": "",
                "object_name": "",
                "object_type": "",
                "schema": "",
                "source_code": [],
                "call_tree": {},
                "cache_hit": False,
                "cache_stale": False,
                "explanation": {},
                "validated": False,
                "confidence": 0.0,
                "warnings": [],
                "search_results": [],
                "multi_source": {},
                "target_variable": "",
                "variable_chain": {},
                "llm_payload": "",
                "graph_node_ids": [],
                "graph_available": _graph_available,
                "phase2_filters": {},
                "phase2_expected_value": None,
                "phase2_actual_value": None,
                "unsupported_reason": "",
                "bi_routing": {},
                "output": {},
                "partial_flag": False,
            }

            config = {
                "configurable": {
                    "thread_id": request.session_id,
                    "provider": provider,
                    "model": model,
                },
            }

            # Run the full pipeline (non-streaming) to get the final state
            # We'll use the pipeline for everything, then stream only the LLM part
            # First: run classify + search + fetch via the graph (stop before explain)
            state = dict(initial_state)

            # Stage 1: Classify
            yield f"event: stage\ndata: {json_mod.dumps({'stage': 'classify', 'message': 'Understanding your question...'})}\n\n"
            with stage_timer("orchestrator_classify", correlation_id):
                state = await _orchestrator.classify_query(
                    request.query, state, provider=provider, model=model
                )

            if state.get("partial_flag"):
                yield f"event: done\ndata: {json_mod.dumps({'type': 'clarification', 'message': state.get('output', {}).get('message', 'Could you clarify?')})}\n\n"
                return

            # --- Phase 1 schema-from-graph hook: when the classifier did
            # not stamp a schema (LLM error / minimal output) and the user
            # named a PL/SQL function, recover the owning schema from the
            # parsed graph rather than falling back to OFSMDM downstream.
            # Conservative: never overrides a schema the classifier set.
            # Phase 4 broadens this to override mis-classified schemas.
            if not state.get("schema") and _graph_redis is not None:
                candidates = extract_function_candidates(request.query)
                if candidates:
                    owner = schema_for_function(candidates[0], _graph_redis)
                    if owner:
                        state["schema"] = owner
                        logger.info(
                            "Schema resolved from graph: schema_for_function(%s) -> %r",
                            candidates[0], owner,
                        )

            # --- Date-range override: any query with BOTH start_date and
            # end_date is a time-series question, which DataQueryAgent must
            # handle via a two-date SQL comparison. Force DATA_QUERY even if
            # the classifier guessed something else (defensive belt-and-
            # suspenders against mis-classification into VALUE_TRACE).
            _p2_filters = state.get("phase2_filters") or {}
            if _p2_filters.get("start_date") and _p2_filters.get("end_date"):
                if state.get("query_type") != "DATA_QUERY":
                    logger.info(
                        "Forcing DATA_QUERY route: date-range detected "
                        "(start=%s end=%s), classifier said %s",
                        _p2_filters.get("start_date"),
                        _p2_filters.get("end_date"),
                        state.get("query_type"),
                    )
                    state["query_type"] = "DATA_QUERY"

            # --- Phase 2 routing: single-row value traces go to the
            # ValueTracerAgent, which runs its own graph resolve + Oracle
            # value fetch + LLM narration.
            if state.get("query_type") in ("VALUE_TRACE", "DIFFERENCE_EXPLANATION"):
                async for event in _phase2_stream(state, request.query, correlation_id, provider, model):
                    yield event
                return

            # --- Option A routing: aggregate / filter / time-series questions
            # go to the DataQueryAgent which generates + executes a read-only
            # SELECT.
            if state.get("query_type") == "DATA_QUERY":
                async for event in _data_query_stream(
                    state, request.query, correlation_id, provider, model
                ):
                    yield event
                return

            # --- Unsupported: explicit capability-limitation response,
            # no handler, no partial answer, no trace.
            if state.get("query_type") == "UNSUPPORTED":
                async for event in _unsupported_stream(state, correlation_id):
                    yield event
                return

            # --- Function-name pre-check (W37): if the user named a specific
            # PL/SQL function that isn't in the graph, short-circuit with a
            # DECLINED response. This prevents the semantic-search fallback
            # from fabricating an explanation from adjacent functions.
            if state.get("query_type") in ("COLUMN_LOGIC", "VARIABLE_TRACE", "FUNCTION_LOGIC"):
                with stage_timer("function_precheck", correlation_id):
                    precheck = _run_function_precheck(request.query, correlation_id)
                if precheck is not None:
                    async for event in _stream_declined_response(precheck):
                        yield event
                    return

            # --- W35 Phase 7: business-identifier (BI) routing. For
            # COLUMN_LOGIC / FUNCTION_LOGIC queries that mention a CAP-code
            # (or other configured identifier), route to the function the
            # literal index says COMPUTES that identifier rather than
            # whichever loader the enriched-string semantic search ranks
            # first. The pre-check above already passed; an explicit
            # function name in the query is honoured by apply_bi_routing
            # itself (it skips when extract_function_candidates returns a
            # name that exists in the graph).
            if _graph_redis is not None:
                with stage_timer("bi_routing", correlation_id):
                    _orchestrator.apply_bi_routing(state)

            # Stage 2: Semantic search
            yield f"event: stage\ndata: {json_mod.dumps({'stage': 'search', 'message': 'Searching across database schemas...'})}\n\n"
            from langchain_openai import OpenAIEmbeddings
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
            search_query = state.get("object_name", state["raw_query"])
            with stage_timer("embedding_create", correlation_id):
                query_embedding = await embeddings.aembed_query(search_query)
            with stage_timer("vector_search", correlation_id):
                results = await _vector_store.search(query_embedding=query_embedding, top_k=5)
            state["search_results"] = results
            state["schema"] = state.get("schema") or fallback_to_default_schema(
                "main.semantic_search", correlation_id,
            )

            # Stage 3: Fetch source code
            fn_names = list(dict.fromkeys(r["function_name"] for r in results)) if results else []
            yield f"event: stage\ndata: {json_mod.dumps({'stage': 'fetch', 'message': f'Reading source code for {len(fn_names)} functions...', 'functions': fn_names})}\n\n"
            with stage_timer("metadata_fetch_multi", correlation_id, functions=len(fn_names)):
                state = await _metadata_interpreter.fetch_multi_logic(state)

            # Send metadata event
            meta = {
                "schema": state.get("schema", ""),
                "object_name": state.get("object_name", "")[:100],
                "query_type": state.get("query_type", ""),
                "functions_analyzed": list(state.get("multi_source", {}).keys()),
                "correlation_id": correlation_id,
            }
            yield f"event: meta\ndata: {json_mod.dumps(meta)}\n\n"

            # --- Graph pipeline: resolve nodes for structured LLM payload ---
            if _graph_available and _graph_redis:
                try:
                    target_var = state.get("target_variable", "").strip()
                    obj_name = state.get("object_name", "").strip()
                    g_schema = state.get("schema") or fallback_to_default_schema(
                        "main.graph_pipeline", correlation_id,
                    )

                    # Phase 4: when the target is a column, prefer the
                    # schema that actually owns the column over the
                    # orchestrator-classified default. This makes
                    # `What writes <OFSERM_COLUMN>?` consume the right
                    # graph:index:<schema> instead of looking up an
                    # OFSERM column in graph:index:OFSMDM (a guaranteed
                    # miss). When the column lives in multiple schemas,
                    # we keep the orchestrator's default — main.py's
                    # downstream caveat / clarification path still
                    # applies, and the multi-schema multi_source from
                    # Phase 3 keeps the user-visible response useful.
                    if target_var and _graph_redis is not None:
                        column_owners = schemas_for_column(
                            target_var, _graph_redis
                        )
                        if len(column_owners) == 1 and column_owners[0] != g_schema:
                            logger.info(
                                "Graph pipeline schema pivot: %s -> %s "
                                "(column %s lives in %s)",
                                g_schema, column_owners[0],
                                target_var, column_owners[0],
                            )
                            g_schema = column_owners[0]

                    if target_var:
                        g_query_type = "variable"
                        g_search_term = target_var
                    elif obj_name:
                        # W43: object_name is the enriched semantic-search
                        # blob, not a function identifier. Prefer the clean
                        # name the W37 pre-check already extracts from the
                        # raw query.
                        candidates = extract_function_candidates(state["raw_query"])
                        g_query_type = "function"
                        g_search_term = candidates[0] if candidates else obj_name
                        logger.debug(
                            "[W43] raw_query candidates=%s, identifier=%s",
                            candidates, g_search_term,
                        )
                    else:
                        g_query_type = "variable"
                        g_search_term = state["raw_query"]

                    _w43_diag.info(
                        "[W43_DIAG] correlation_id=%s stage=graph_pipeline_entry"
                        " query_type=%r target_variable=%r object_name_len=%d"
                        " g_query_type=%r g_search_term=%r g_schema=%r",
                        correlation_id,
                        state.get("query_type"),
                        target_var or None,
                        len(obj_name),
                        g_query_type,
                        g_search_term[:120] if g_search_term else "",
                        g_schema,
                    )

                    with stage_timer("graph_resolve_nodes", correlation_id):
                        node_ids = resolve_query_to_nodes(
                            query_type=g_query_type,
                            target_variable=g_search_term if g_query_type == "variable" else "",
                            function_name=g_search_term if g_query_type == "function" else "",
                            table_name="",
                            schema=g_schema,
                            redis_client=_graph_redis,
                        )

                    _w43_diag.info(
                        "[W43_DIAG] correlation_id=%s stage=graph_resolve_nodes_result"
                        " node_ids_count=%d fallback_triggered=%s",
                        correlation_id,
                        len(node_ids),
                        not bool(node_ids),
                    )

                    if node_ids:
                        with stage_timer("graph_fetch_nodes", correlation_id, node_count=len(node_ids)):
                            fetched_nodes = fetch_nodes_by_ids(node_ids, g_schema, _graph_redis)
                        with stage_timer("graph_fetch_edges", correlation_id):
                            relevant_edges = fetch_relevant_edges(node_ids, g_schema, _graph_redis)
                        with stage_timer("graph_determine_exec_order", correlation_id):
                            exec_order = determine_execution_order(fetched_nodes, relevant_edges)
                        with stage_timer("graph_assemble_payload", correlation_id):
                            payload = assemble_llm_payload(
                                nodes=fetched_nodes,
                                edges=relevant_edges,
                                target_variable=g_search_term,
                                user_query=state["raw_query"],
                                execution_order=exec_order,
                            )
                        state["llm_payload"] = payload
                        state["graph_available"] = True
                        _w43_diag.info(
                            "[W43_DIAG] correlation_id=%s stage=graph_path_selected"
                            " fetched_nodes=%d edges=%d payload_chars=%d",
                            correlation_id,
                            len(fetched_nodes),
                            len(relevant_edges),
                            len(payload),
                        )
                        logger.info("Using graph pipeline for query: %s", state.get("raw_query"))
                    else:
                        _w43_diag.info(
                            "[W43_DIAG] correlation_id=%s stage=fallback_selected"
                            " reason=no_nodes_returned g_query_type=%r g_search_term=%r",
                            correlation_id,
                            g_query_type,
                            g_search_term[:120] if g_search_term else "",
                        )
                        logger.info("Graph returned no nodes, falling back to raw source for query: %s", state.get("raw_query"))
                except Exception as exc:
                    _w43_diag.warning(
                        "[W43_DIAG] correlation_id=%s stage=graph_pipeline_exception"
                        " exc=%r fallback_triggered=true",
                        correlation_id,
                        str(exc)[:200],
                    )
                    logger.warning("Graph pipeline failed (non-fatal), falling back to raw source: %s", exc)

            # Stage 4: Generate explanation
            yield f"event: stage\ndata: {json_mod.dumps({'stage': 'explain', 'message': 'Generating detailed explanation...'})}\n\n"

            full_markdown = ""

            # W45 pre-generation check: if the user asked about a business
            # identifier (e.g. CAP973) that is absent from every retrieved
            # function's source body, route to a structured "not the answer"
            # response instead of the normal explainer. Semantic search
            # still returns name-similar neighbors, but none of them compute
            # the asked identifier — the normal path would describe a
            # neighbor as if it were the answer.
            # Phase 4: pass the graph Redis client so the detector can
            # consult every discovered schema's source bodies before
            # flagging an identifier as ungrounded. Pre-Phase-4 the
            # check used only the (already retrieved) multi_source —
            # accurate when semantic search reaches every schema, but
            # vulnerable to false positives when an OFSERM function
            # owning the identifier wasn't in the top-K retrieval.
            ungrounded_ids = detect_ungrounded_identifiers(
                raw_query=request.query,
                multi_source=state.get("multi_source", {}) or {},
                redis_client=_graph_redis,
            )

            # W49 pre-generation check: the asked-about FUNCTION exists in
            # graph metadata but its source body was not returned by the
            # retrieval pipeline (partial-indexed schema, e.g. OFSERM). The
            # normal path would speculate using related functions; the W49
            # branch instead emits a structured "source not currently
            # indexed" response that tells the truth about the gap. W45
            # takes precedence — if the identifier is fully ungrounded that
            # framing is more accurate.
            partial_source_info: Optional[Dict[str, Any]] = None
            if not ungrounded_ids:
                partial_source_info = _detect_partial_source_for_query(
                    raw_query=request.query,
                    multi_source=state.get("multi_source", {}) or {},
                    correlation_id=correlation_id,
                )

            # Hierarchy header (W39): emitted once before branching so every
            # normal streaming path — variable tracer, graph-pipeline, and
            # the plain semantic explainer — receives the same context line.
            # SKIPPED for the ungrounded branch (W45): the top-ranked
            # retrieved function is not the answer, so its hierarchy is
            # misleading.
            # SKIPPED for the partial-source branch (W49): the body already
            # includes the hierarchy in its "What I know about it" section,
            # so emitting a header above it would be redundant.
            if not ungrounded_ids and not partial_source_info:
                with stage_timer("hierarchy_header", correlation_id):
                    hierarchy_prefix = _logic_explainer.hierarchy_header(state)
                if hierarchy_prefix:
                    full_markdown += hierarchy_prefix
                    mark_event("first_sse_token_emit", correlation_id, source="hierarchy_header")
                    yield f"event: token\ndata: {json_mod.dumps(hierarchy_prefix)}\n\n"

                # W35 Phase 7: Derivation banner. Rendered when BI routing
                # resolved the query to a function whose Phase 6
                # derivation summary is on its case_when_target literal
                # record. Order is hierarchy -> derivation -> body. The
                # banner is deterministic markdown — the LLM does not
                # write it.
                with stage_timer("derivation_header", correlation_id):
                    derivation_prefix = render_derivation_header(state)
                if derivation_prefix:
                    full_markdown += derivation_prefix
                    yield f"event: token\ndata: {json_mod.dumps(derivation_prefix)}\n\n"

            if ungrounded_ids:
                # W45 ungrounded branch: bypass resolve/alias/extract/build
                # (all produce empty results for an identifier that isn't in
                # any retrieved source), and stream a structured "not found"
                # response. The warnings array will still carry
                # UNGROUNDED_IDENTIFIERS via evaluate_grounding() below, so
                # W46 metadata rendering is unaffected.
                primary_identifier = ungrounded_ids[0]
                with stage_timer(
                    "llm_stream_ungrounded",
                    correlation_id,
                    identifier=primary_identifier,
                    candidate_count=len(state.get("multi_source", {}) or {}),
                ):
                    _first_token = True
                    async for token in _variable_tracer.stream_ungrounded(
                        identifier=primary_identifier,
                        candidates=state.get("multi_source", {}) or {},
                        raw_query=request.query,
                        provider=provider,
                        model=model,
                    ):
                        if _first_token:
                            mark_event("llm_first_token", correlation_id, branch="ungrounded")
                            _first_token = False
                        full_markdown += token
                        yield f"event: token\ndata: {json_mod.dumps(token)}\n\n"
            elif partial_source_info:
                # W49 partial-source branch: function name and metadata are
                # known, but its source body was not returned by retrieval.
                # Skip the normal generation path (which would speculate
                # using related functions) and stream a structured "source
                # not currently indexed" response.
                with stage_timer(
                    "llm_stream_partial_source",
                    correlation_id,
                    function_name=partial_source_info["function_name"],
                    schema=partial_source_info["schema"],
                ):
                    _first_token = True
                    async for token in _variable_tracer.stream_partial_source(
                        function_name=partial_source_info["function_name"],
                        schema=partial_source_info["schema"],
                        hierarchy=partial_source_info.get("hierarchy"),
                        manifest_description=partial_source_info.get(
                            "manifest_description"
                        ),
                        provider=provider,
                        model=model,
                    ):
                        if _first_token:
                            mark_event(
                                "llm_first_token",
                                correlation_id,
                                branch="partial_source",
                            )
                            _first_token = False
                        full_markdown += token
                        yield f"event: token\ndata: {json_mod.dumps(token)}\n\n"
            elif state.get("llm_payload"):
                # Graph pipeline produced a structured payload — use it
                with stage_timer("llm_stream_semantic_graph", correlation_id):
                    _first_token = True
                    async for token in _logic_explainer.stream_semantic(
                        state, provider, model
                    ):
                        if _first_token:
                            mark_event("llm_first_token", correlation_id, branch="graph_payload")
                            _first_token = False
                        full_markdown += token
                        yield f"event: token\ndata: {json_mod.dumps(token)}\n\n"
            elif state.get("query_type") == "VARIABLE_TRACE":
                # Run variable resolver + extraction first (fast, non-streaming)
                target_var = state.get("target_variable", "").strip()
                functions_source = {}
                for fn_name, fn_data in state.get("multi_source", {}).items():
                    src = fn_data.get("source_code", [])
                    if src:
                        functions_source[fn_name] = src

                if target_var and functions_source:
                    with stage_timer("variable_resolve_llm", correlation_id):
                        seeds = await _variable_tracer.resolve_variable_names(
                            target_var, functions_source, provider, model
                        )
                    with stage_timer("variable_alias_map_build", correlation_id):
                        alias_map = _variable_tracer.build_alias_map(seeds, functions_source)
                    with stage_timer("variable_relevant_lines_extract", correlation_id):
                        tagged = _variable_tracer.extract_relevant_lines(
                            target_var, functions_source, alias_map, seeds
                        )
                    with stage_timer("variable_transformation_chain_build", correlation_id):
                        chain_text = _variable_tracer.build_transformation_chain(
                            target_var, tagged, seeds
                        )
                    with stage_timer("llm_stream_variable_trace", correlation_id):
                        _first_token = True
                        async for token in _variable_tracer.stream_chain(
                            target_var, chain_text, request.query, provider, model
                        ):
                            if _first_token:
                                mark_event("llm_first_token", correlation_id, branch="variable_trace")
                                _first_token = False
                            full_markdown += token
                            yield f"event: token\ndata: {json_mod.dumps(token)}\n\n"
                else:
                    # Fallback to semantic stream
                    with stage_timer("llm_stream_semantic_fallback_vt", correlation_id):
                        _first_token = True
                        async for token in _logic_explainer.stream_semantic(
                            state, provider, model
                        ):
                            if _first_token:
                                mark_event("llm_first_token", correlation_id, branch="semantic_fallback_vt")
                                _first_token = False
                            full_markdown += token
                            yield f"event: token\ndata: {json_mod.dumps(token)}\n\n"
            else:
                with stage_timer("llm_stream_semantic_fallback", correlation_id):
                    _first_token = True
                    async for token in _logic_explainer.stream_semantic(
                        state, provider, model
                    ):
                        if _first_token:
                            mark_event("llm_first_token", correlation_id, branch="semantic_fallback")
                            _first_token = False
                        full_markdown += token
                        yield f"event: token\ndata: {json_mod.dumps(token)}\n\n"

            # --- Grounding evaluation (W37): decide VERIFIED vs UNVERIFIED
            # based on citations, identifier presence, and contradiction
            # phrases. Replaces a previously-hardcoded VERIFIED payload that
            # ignored what the LLM actually produced.
            multi_source = state.get("multi_source", {}) or {}
            functions_analyzed = list(multi_source.keys())
            with stage_timer("grounding_evaluate", correlation_id):
                grounding = evaluate_grounding(
                    raw_query=request.query,
                    markdown=full_markdown,
                    multi_source=multi_source,
                    functions_analyzed=functions_analyzed,
                    query_type=state.get("query_type", ""),
                    redis_client=_graph_redis,
                )

            # W49: when the partial-source branch ran, surface the
            # PARTIAL_SOURCE_INDEXED warning so W46's ValidationHeader
            # renders the same "this is partial" badge users see for W45.
            # Override the badge/confidence to UNVERIFIED at low confidence
            # because the body intentionally avoids analysis.
            if partial_source_info:
                grounding["warnings"].append(
                    "PARTIAL_SOURCE_INDEXED: "
                    f"{partial_source_info['function_name']} has graph "
                    f"metadata in {partial_source_info['schema']} but its "
                    f"source body is not currently indexed for analysis"
                )
                grounding["badge"] = "UNVERIFIED"
                grounding["confidence"] = 0.2

            # Stream caveat tokens before closing so the user sees them inline.
            # W45/W49: suppress the Caveats block when either structured
            # branch was taken — the body already explains the situation, so
            # an appended Caveats block would be redundant and contradict
            # the clean structure. The warnings array (including
            # UNGROUNDED_IDENTIFIERS / PARTIAL_SOURCE_INDEXED) is still
            # emitted in the done payload for W46's ValidationHeader to
            # render.
            final_markdown = full_markdown
            if (
                grounding["sanity_messages"]
                and not ungrounded_ids
                and not partial_source_info
            ):
                caveat_block = (
                    "\n\n---\n\n"
                    "**Caveats:**\n"
                    + "\n".join(f"- {msg}" for msg in grounding["sanity_messages"])
                )
                with stage_timer("caveat_stream", correlation_id, chunks=len(caveat_block) // 4 + 1):
                    for chunk in _chunk_text(caveat_block):
                        yield f"event: token\ndata: {json_mod.dumps(chunk)}\n\n"
                final_markdown = full_markdown + caveat_block

            done_payload = {
                "confidence": grounding["confidence"],
                "validated": grounding["badge"] == "VERIFIED",
                "badge": grounding["badge"],
                "source_citations": grounding["source_citations"],
                "warnings": grounding["warnings"],
                "functions_analyzed": functions_analyzed,
                "correlation_id": correlation_id,
                "explanation": {
                    "markdown": final_markdown,
                    "summary": final_markdown[:200],
                },
            }
            with stage_timer("done_emit", correlation_id):
                yield f"event: done\ndata: {json_mod.dumps(done_payload)}\n\n"

        except LLMSanitizedError as exc:
            logger.warning(
                "Stream sanitized LLM failure | category=%s context=%s correlation_id=%s",
                exc.category, exc.context, exc.correlation_id or correlation_id,
            )
            declined = build_declined_response(
                exc.category, exc.user_message,
                correlation_id=exc.correlation_id or correlation_id,
                context=exc.context,
            )
            yield f"event: done\ndata: {json_mod.dumps(declined)}\n\n"
        except Exception as exc:
            # Sanitize the unexpected-error path so str(exc) cannot leak Python
            # internals (e.g. CompletionUsage(...) repr) to the frontend. The
            # raw exception is captured in the server logs only.
            logger.error(f"Stream failed: {exc}\n{traceback.format_exc()}")
            yield f"event: error\ndata: {json_mod.dumps({'error': GENERIC_LLM_ERROR_MESSAGE, 'correlation_id': correlation_id})}\n\n"

    async def _timed_event_stream():
        with stage_timer("total_request", correlation_id):
            async for event in event_stream():
                yield event

    return StreamingResponse(
        _timed_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Correlation-ID": correlation_id,
        },
    )


async def _phase2_stream(state, user_query, correlation_id, provider, model):
    """Stream a Phase 2 VALUE_TRACE / DIFFERENCE_EXPLANATION response as SSE.

    Runs the ValueTracerAgent, which resolves graph nodes, fetches actual
    Oracle values, builds a proof chain, identifies any delta, generates
    verification SQL, and finally streams an LLM narration.
    """
    query_type = state["query_type"]
    filters = dict(state.get("phase2_filters") or {})
    target = (state.get("target_variable") or "").strip()
    schema = state.get("schema") or fallback_to_default_schema(
        "main._phase2_stream", correlation_id,
    )

    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'classify', 'message': 'Classified as ' + query_type})}\n\n"

    if _value_tracer is None:
        yield f"event: error\ndata: {json_mod.dumps({'error': 'Phase 2 value tracer not available'})}\n\n"
        return

    # Enforce mis_date requirement configurably. Without it the trace
    # cannot be scoped to a specific run, so we fail fast with a clear
    # clarification event rather than producing a misleading answer.
    require_mis_date = (_settings.get("phase2") or {}).get("require_mis_date", True)
    if require_mis_date and not filters.get("mis_date"):
        payload = {
            "type": "clarification",
            "message": (
                "This looks like a data trace query but no MIS date was detected. "
                "Please include the date (e.g. 'on 2025-12-31')."
            ),
        }
        yield f"event: done\ndata: {json_mod.dumps(payload)}\n\n"
        return

    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'search', 'message': 'Resolving graph subgraph...'})}\n\n"
    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'fetch', 'message': 'Fetching actual Oracle values for each step...'})}\n\n"

    try:
        if query_type == "DIFFERENCE_EXPLANATION":
            with stage_timer("phase2_explain_difference", correlation_id):
                result = await _value_tracer.explain_difference(
                    target_variable=target,
                    filters=filters,
                    schema=schema,
                    bank_value=float(state.get("phase2_expected_value") or 0.0),
                    system_value=float(state.get("phase2_actual_value") or 0.0),
                    user_query=user_query,
                    provider=provider,
                    model=model,
                )
        else:
            # VALUE_TRACE (and anything else mis-routed here) -> single-row trace.
            with stage_timer("phase2_trace_value", correlation_id):
                result = await _value_tracer.trace_value(
                    target_variable=target,
                    filters=filters,
                    schema=schema,
                    expected_value=state.get("phase2_expected_value"),
                    user_query=user_query,
                    provider=provider,
                    model=model,
                )
    except LLMSanitizedError as exc:
        logger.warning(
            "Phase 2 trace sanitized LLM failure | category=%s context=%s "
            "correlation_id=%s",
            exc.category, exc.context, exc.correlation_id or correlation_id,
        )
        declined = build_declined_response(
            exc.category, exc.user_message,
            correlation_id=exc.correlation_id or correlation_id,
            context=exc.context,
        )
        yield f"event: done\ndata: {json_mod.dumps(declined)}\n\n"
        return
    except Exception as exc:
        logger.error(f"Phase 2 trace failed: {exc}\n{traceback.format_exc()}")
        yield f"event: error\ndata: {json_mod.dumps({'error': GENERIC_LLM_ERROR_MESSAGE, 'correlation_id': correlation_id})}\n\n"
        return

    # Identifier-ambiguity short-circuit — the trace never ran because the
    # target column is ambiguous across multiple tables. Surface the
    # explanatory message + suggestions instead of a trace response.
    if result.get("type") == "identifier_ambiguous":
        message = result.get("message") or ""
        for chunk in _chunk_text(message):
            yield f"event: token\ndata: {json_mod.dumps(chunk)}\n\n"
        done_payload = {
            **result,
            "explanation": {"markdown": message},
            "correlation_id": correlation_id,
        }
        yield f"event: done\ndata: {json_mod.dumps(done_payload, default=str)}\n\n"
        return

    # Row-first result shape (new): status, row, origin, route, evidence,
    # explanation, sanity_warnings, used_fallback, verification_sql
    origin = result.get("origin") or {}
    row = result.get("row") or {}
    meta = {
        "schema": schema,
        "query_type": query_type,
        "target_variable": target,
        "filters": filters,
        "status": result.get("status"),
        "route": result.get("route"),
        "origin_category": origin.get("origin_category"),
        "origin_value": origin.get("origin_value"),
        "traceable_via_graph": origin.get("traceable_via_graph"),
        "row_found": bool(row),
        "correlation_id": correlation_id,
    }
    yield f"event: meta\ndata: {json_mod.dumps(meta, default=str)}\n\n"

    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'explain', 'message': 'Generating explanation...'})}\n\n"

    # The explanation is already produced + sanity-checked. Stream it as
    # whitespace-preserving chunks so the frontend renders it progressively.
    full_markdown = result.get("explanation") or "(no explanation available)"
    mark_event("first_sse_token_emit", correlation_id, branch="phase2_rechunk")
    with stage_timer("phase2_token_stream", correlation_id, chars=len(full_markdown)):
        for chunk in _chunk_text(full_markdown):
            yield f"event: token\ndata: {json_mod.dumps(chunk)}\n\n"

    done_payload = {
        "type": query_type.lower(),
        "status": result.get("status"),
        "route": result.get("route"),
        "validated": not result.get("sanity_warnings"),
        "sanity_warnings": result.get("sanity_warnings") or [],
        "used_fallback": bool(result.get("used_fallback")),
        "badge": "VERIFIED" if not result.get("sanity_warnings") else "REVIEW",
        "correlation_id": correlation_id,
        "explanation": {"markdown": full_markdown},
        "origin": origin,
        "evidence": result.get("evidence"),
        "verification_sql": result.get("verification_sql"),
    }
    with stage_timer("done_emit", correlation_id, route="phase2"):
        yield f"event: done\ndata: {json_mod.dumps(done_payload, default=str)}\n\n"


def _chunk_text(text: str, chunk_size: int = 4):
    """Split text into small chunks for progressive SSE delivery."""
    if not text:
        return
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


async def _data_query_stream(state, user_query, correlation_id, provider, model):
    """Stream a DATA_QUERY response: LLM-generated SQL + safeguarded execution."""
    schema = state.get("schema") or fallback_to_default_schema(
        "main._data_query_stream", correlation_id,
    )
    filters = dict(state.get("phase2_filters") or {})

    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'classify', 'message': 'Classified as DATA_QUERY'})}\n\n"

    if _data_query is None:
        yield f"event: error\ndata: {json_mod.dumps({'error': 'DataQueryAgent not available'})}\n\n"
        return

    require_mis_date = (_settings.get("phase2") or {}).get("require_mis_date", True)
    has_date_range = bool(filters.get("start_date") and filters.get("end_date"))
    if require_mis_date and not filters.get("mis_date") and not has_date_range:
        payload = {
            "type": "clarification",
            "message": (
                "This looks like a data query but no MIS date was detected. "
                "Please include a date (e.g. 'on 2025-12-31') or a date range "
                "(e.g. 'between 2025-09-30 and 2025-12-31') so results are "
                "scoped to a specific run."
            ),
        }
        yield f"event: done\ndata: {json_mod.dumps(payload)}\n\n"
        return

    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'search', 'message': 'Building schema catalog + generating SQL...'})}\n\n"
    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'fetch', 'message': 'Executing read-only query against Oracle...'})}\n\n"

    try:
        with stage_timer("data_query_answer", correlation_id):
            result = await _data_query.answer(
                user_query=user_query,
                schema=schema,
                filters=filters,
                provider=provider,
                model=model,
                target_variable=(state.get("target_variable") or None),
            )
    except LLMSanitizedError as exc:
        logger.warning(
            "DATA_QUERY sanitized LLM failure | category=%s context=%s "
            "correlation_id=%s",
            exc.category, exc.context, exc.correlation_id or correlation_id,
        )
        declined = build_declined_response(
            exc.category, exc.user_message,
            correlation_id=exc.correlation_id or correlation_id,
            context=exc.context,
        )
        yield f"event: done\ndata: {json_mod.dumps(declined)}\n\n"
        return
    except Exception as exc:
        logger.error(f"DATA_QUERY failed: {exc}\n{traceback.format_exc()}")
        yield f"event: error\ndata: {json_mod.dumps({'error': GENERIC_LLM_ERROR_MESSAGE, 'correlation_id': correlation_id})}\n\n"
        return

    # Identifier-ambiguity short-circuit — no SQL was generated because
    # the target column is ambiguous across multiple tables. Surface the
    # explanatory message + suggestions instead of a data_query response.
    # Phase 4 adds the parallel `table_ambiguous` short-circuit for
    # multi-schema collisions (a named table exists in OFSMDM and OFSERM).
    if result.get("type") in ("identifier_ambiguous", "table_ambiguous"):
        message = result.get("message") or ""
        for chunk in _chunk_text(message):
            yield f"event: token\ndata: {json_mod.dumps(chunk)}\n\n"
        done_payload = {
            **result,
            "explanation": {"markdown": message},
            "correlation_id": correlation_id,
        }
        yield f"event: done\ndata: {json_mod.dumps(done_payload, default=str)}\n\n"
        return

    meta = {
        # Phase 4: prefer the schema DataQueryAgent actually routed to —
        # may differ from the orchestrator-classified `schema` when the
        # user named an OFSERM table on a default-OFSMDM request.
        "schema": result.get("schema") or schema,
        "query_type": "DATA_QUERY",
        "status": result.get("status"),
        "query_kind": result.get("query_kind"),
        "row_count": result.get("row_count"),
        "correlation_id": correlation_id,
    }
    yield f"event: meta\ndata: {json_mod.dumps(meta, default=str)}\n\n"

    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'explain', 'message': 'Formatting results...'})}\n\n"

    explanation = result.get("explanation") or "(no explanation available)"
    mark_event("first_sse_token_emit", correlation_id, branch="data_query_rechunk")
    with stage_timer("data_query_token_stream", correlation_id, chars=len(explanation)):
        for chunk in _chunk_text(explanation):
            yield f"event: token\ndata: {json_mod.dumps(chunk)}\n\n"

    status = result.get("status")
    suspicious = bool(result.get("suspicious"))
    validated = status == "answered" and not suspicious
    if suspicious:
        badge = "UNVERIFIED"
    elif status == "answered":
        badge = "VERIFIED"
    elif status == "confirmation_required":
        badge = "REVIEW"
    else:
        badge = "REJECTED"
    done_payload = {
        "type": "data_query",
        "status": status,
        "query_kind": result.get("query_kind"),
        "validated": validated,
        "badge": badge,
        "sanity_warnings": result.get("sanity_warnings") or [],
        "suspicious": suspicious,
        "suspicion_reason": result.get("suspicion_reason"),
        "summary": result.get("summary"),
        "correlation_id": correlation_id,
        "explanation": {"markdown": explanation},
        "sql": result.get("sql"),
        "count_sql": result.get("count_sql"),
        "params": result.get("params"),
        "columns": result.get("columns"),
        "rows": result.get("rows"),
        "row_count": result.get("row_count"),
        "requested_dates": result.get("requested_dates") or [],
        "verification_sql": result.get("verification_sql"),
    }
    with stage_timer("done_emit", correlation_id, route="data_query"):
        yield f"event: done\ndata: {json_mod.dumps(done_payload, default=str)}\n\n"


def _run_function_precheck(query: str, correlation_id: str) -> Optional[Dict[str, Any]]:
    """Return a DECLINED payload if *query* names a function we don't have.

    Extracts PL/SQL-looking identifiers from the raw query. If any extracted
    token looks like a function name (per the stopword-filtered heuristic in
    orchestrator.extract_function_candidates) but has no graph stored in any
    known schema, returns a pre-built DECLINED response. Returns None when
    no named function is referenced, or when every referenced function was
    found in the graph.
    """
    if _graph_redis is None:
        return None
    candidates = extract_function_candidates(query)
    if not candidates:
        return None
    missing = [
        cand for cand in candidates
        if not function_exists_in_graph(cand, _graph_redis)
    ]
    if not missing:
        return None
    # Decline on the first missing candidate — it's almost always the one
    # the user actually asked about. Similar-function suggestions help the
    # user recover quickly from a typo or wrong spelling.
    requested = missing[0]
    similar = find_similar_function_names(requested, _graph_redis, top_n=3)
    logger.info(
        "Function-name pre-check declined query: requested=%s, missing=%s, "
        "similar=%s | correlation_id=%s",
        requested, missing, similar, correlation_id,
    )
    return build_function_not_found_response(
        requested_function=requested,
        similar_functions=similar,
        correlation_id=correlation_id,
    )


def _detect_partial_source_for_query(
    raw_query: str,
    multi_source: Dict[str, Any],
    correlation_id: str,
) -> Optional[Dict[str, Any]]:
    """W49: detect the partial-source state for the asked-about function.

    Extracts the primary function-name candidate from *raw_query*. If that
    function exists in any known schema's graph metadata but its source
    body is not present in *multi_source* (or is below the minimum
    threshold), returns a dict carrying everything the W49 streaming
    branch needs:

      - function_name: case-preserved name from the query
      - schema: schema where parse metadata was found
      - hierarchy: the function graph's hierarchy block (may be empty)
      - manifest_description: optional declared description (currently
        always None — manifest descriptions aren't propagated onto the
        graph hierarchy block today)

    Returns None when the partial-source state does not apply: no graph
    Redis client, no function candidates, every candidate has source
    available in multi_source, or no schema has metadata for the
    candidate. In those cases the normal generation path is correct.
    """
    if _graph_redis is None:
        return None

    candidates = extract_function_candidates(raw_query)
    if not candidates:
        return None

    # Build a case-insensitive lookup from multi_source keys → entries so
    # we can detect the asked-about function whether or not the casing
    # matches what semantic search returned.
    ms_by_upper = {k.upper(): v for k, v in (multi_source or {}).items()}

    from src.parsing.store import get_function_graph
    from src.parsing.schema_discovery import discovered_schemas

    schemas_to_check = discovered_schemas(_graph_redis)
    for candidate in candidates:
        retrieved = ms_by_upper.get(candidate.upper())
        retrieved_source = (
            (retrieved or {}).get("source_code") if retrieved else None
        )
        for schema in schemas_to_check:
            if not detect_partial_source_function(
                function_name=candidate,
                schema=schema,
                retrieved_source=retrieved_source,
                redis_client=_graph_redis,
            ):
                continue

            # Found the partial-source case. Pull the function graph for
            # hierarchy details (best-effort — absence is non-fatal).
            hierarchy: Dict[str, Any] = {}
            try:
                graph = get_function_graph(
                    _graph_redis, schema, candidate.upper()
                )
                if graph:
                    hierarchy = graph.get("hierarchy") or {}
            except Exception as exc:
                logger.debug(
                    "W49 hierarchy fetch failed for %s.%s: %s | correlation_id=%s",
                    schema, candidate, exc, correlation_id,
                )

            logger.info(
                "W49 partial-source branch: function=%s schema=%s | "
                "correlation_id=%s",
                candidate, schema, correlation_id,
            )
            return {
                "function_name": candidate,
                "schema": schema,
                "hierarchy": hierarchy,
                "manifest_description": None,
            }
    return None


async def _stream_declined_response(payload: Dict[str, Any]):
    """Yield a DECLINED response as SSE tokens + meta + done events."""
    meta = {
        "type": payload.get("type", "function_not_found"),
        "status": "declined",
        "requested_function": payload.get("requested_function"),
        "similar_functions": payload.get("similar_functions") or [],
        "correlation_id": payload.get("correlation_id"),
    }
    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'classify', 'message': 'Named function not found in graph'})}\n\n"
    yield f"event: meta\ndata: {json_mod.dumps(meta)}\n\n"
    message = payload.get("message") or ""
    for chunk in _chunk_text(message):
        yield f"event: token\ndata: {json_mod.dumps(chunk)}\n\n"
    yield f"event: done\ndata: {json_mod.dumps(payload)}\n\n"


async def _unsupported_stream(state, correlation_id):
    """Stream an explicit capability-limitation response for UNSUPPORTED queries."""
    reason = state.get("unsupported_reason") or "capability not available in this system"
    markdown = (
        "### Not supported\n\n"
        f"This question cannot be answered by RTIE: **{reason}**.\n\n"
        "RTIE is a read-only introspection system scoped to the parsed "
        "PL/SQL graph and the current staging schema. It does not:\n"
        "- Reconcile against downstream result tables (FCT_*) that are not "
        "in the graph.\n"
        "- Forecast or predict future state.\n"
        "- Query tables outside the configured schema.\n\n"
        "**What you can do:** rephrase as a question about a specific "
        "value, account, or aggregate within the staging schema, or escalate "
        "to a team with access to the missing data source."
    )
    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'classify', 'message': 'Classified as UNSUPPORTED'})}\n\n"
    meta = {
        "query_type": "UNSUPPORTED",
        "status": "declined",
        "reason": reason,
        "correlation_id": correlation_id,
    }
    yield f"event: meta\ndata: {json_mod.dumps(meta)}\n\n"
    for chunk in _chunk_text(markdown):
        yield f"event: token\ndata: {json_mod.dumps(chunk)}\n\n"
    done_payload = {
        "type": "unsupported",
        "status": "declined",
        "validated": True,
        "badge": "DECLINED",
        "reason": reason,
        "correlation_id": correlation_id,
        "explanation": {"markdown": markdown},
    }
    yield f"event: done\ndata: {json_mod.dumps(done_payload)}\n\n"


async def _handle_command(
    command: str, args: list, session_id: str
) -> Dict[str, Any]:
    """Route a slash command to the appropriate handler.

    Args:
        command: The command name.
        args: List of command arguments.
        session_id: The current session ID.

    Returns:
        Command result dictionary.
    """
    settings = _load_settings()
    schema = settings["oracle"]["schema"]

    logger.info(f"Handling command: /{command} args={args}")

    if command == "refresh-cache" and args:
        return await _cache_manager.refresh_logic_cache(args[0], schema)
    elif command == "refresh-cache-all":
        return await _cache_manager.refresh_all_logic_cache(schema)
    elif command == "cache-status" and args:
        return await _cache_manager.get_cache_status(args[0], schema)
    elif command == "cache-list":
        return await _cache_manager.list_cached_objects(schema)
    elif command == "cache-clear" and args:
        return await _cache_manager.clear_cache_entry(args[0], schema)
    elif command == "refresh-schema":
        # Phase 2: refresh every discovered schema unless the user names
        # one explicitly via `/refresh-schema OFSERM`. Single-arg form
        # remains for parity with the per-schema admin workflow.
        target_schemas = (
            [args[0]] if args else discovered_schemas(_graph_redis)
        )
        results = {}
        for sch in target_schemas:
            try:
                results[sch] = await _cache_manager.refresh_schema_snapshot(sch)
            except Exception as exc:
                results[sch] = {
                    "status": "error",
                    "schema": sch,
                    "message": str(exc),
                }
        if len(results) == 1:
            # Preserve the historical single-schema response shape so
            # existing tooling that pipes /refresh-schema's output keeps
            # working when only one schema is targeted.
            return next(iter(results.values()))
        return {
            "status": "completed",
            "schemas": list(results.keys()),
            "results": results,
        }
    elif command == "index-module" and args:
        force = "--force" in args
        module_name = [a for a in args if a != "--force"][0]
        return await _indexer.index_module(module_name, force=force)
    elif command == "index-all":
        force = "--force" in args
        return await _indexer.index_all_modules(force=force)
    elif command == "index-status":
        return await _vector_store.get_index_stats()
    else:
        return {
            "status": "error",
            "message": f"Unknown command: /{command}",
            "supported_commands": [
                "/refresh-cache <name>",
                "/refresh-cache-all",
                "/cache-status <name>",
                "/cache-list",
                "/cache-clear <name>",
                "/refresh-schema",
                "/index-module <name> [--force]",
                "/index-all [--force]",
                "/index-status",
            ],
        }


@app.get("/v1/models")
async def models_endpoint() -> Dict[str, Any]:
    """List available LLM providers and their models.

    Returns:
        Dict with provider details, available models, and current defaults.
    """
    models = list_available_models()
    return {
        "default_provider": get_default_provider(),
        "default_model": get_default_model(get_default_provider()),
        "providers": models,
    }


@app.get("/health")
async def health_endpoint() -> Dict[str, Any]:
    """Check health of all external dependencies.

    Returns:
        Health status dict with Oracle, Redis, PostgreSQL statuses
        and overall system health.
    """
    return await _health_checker.check_all()
