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
    detect_business_identifiers,
    extract_function_candidates,
    function_exists_in_graph,
    find_similar_function_names,
    build_function_not_found_response,
    build_unrecognized_term_response,
    resolve_bi_to_function,
    _detect_unrecognized_term_query,
)
from src.agents.anchor_resolution import (
    apply_w70_anchor,
    ensure_anchor_in_search_results,
    promote_anchor_to_front,
    resolve_search_query,
)
from src.agents.graph_rerank import rerank_with_rrf
from src.agents.retrieval_config import resolve_top_k
from src.agents.metadata_interpreter import MetadataInterpreter
from src.agents.logic_explainer import (
    LogicExplainer,
    detect_ungrounded_identifiers,
    detect_partial_source_function,
    evaluate_grounding,
    render_derivation_header,
)
from src.agents.variable_tracer import VariableTracer
from src.agents.chain_ordering import reorder_multi_source
from src.agents.value_tracer import ValueTracerAgent
from src.agents.data_query import DataQueryAgent
from src.agents.computation_router import detect_named_computation
from src.agents.structural_question_router import detect_structural_question
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

    # W93: regression check for indexer state-lies. The indexer's
    # description-generation LLM failure handler used to write the
    # sentinel string "(indexing failed: ...)" as the description and
    # then mark the doc status="approved" — four OFSERM docs landed in
    # this state, unfindable via KNN but invisible to count-based
    # health probes. The W93 gate in src/agents/indexer.py prevents
    # new sentinels, but legacy docs need to be re-indexed and any
    # future regression in a different code path should fail loudly
    # here rather than silently degrade retrieval. Logs CRITICAL with
    # the affected doc keys; does not abort startup so the operator
    # can remediate without an outage.
    from src.agents.indexer import (
        INDEXING_FAILED_SENTINEL_PREFIX,
        DESCRIPTION_MIN_LENGTH,
    )
    w93_invalid_docs = await _vector_store.scan_for_invalid_approved_docs(
        sentinel_prefix=INDEXING_FAILED_SENTINEL_PREFIX,
        min_description_length=DESCRIPTION_MIN_LENGTH,
    )
    if w93_invalid_docs:
        logger.critical(
            "W93: %d approved doc(s) in the vector store fail the "
            "indexer validation gate. These docs claim status=approved "
            "but their description is either the indexing-failed "
            "sentinel or shorter than %d chars — they are unfindable "
            "via KNN despite looking healthy. Re-run the indexer to "
            "remediate. Affected: %s",
            len(w93_invalid_docs),
            DESCRIPTION_MIN_LENGTH,
            [
                f"{d['schema']}:{d['function_name']} ({d['reason']}, "
                f"len={d['description_length']})"
                for d in w93_invalid_docs
            ],
        )

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

        # Phase 8: legacy ``rtie:logic:*`` key cleanup. The previous source
        # cache was retired in favour of the loader-managed
        # ``graph:source:*`` namespace; any ``rtie:logic:*`` keys left over
        # from earlier deployments are now dead weight and would clutter
        # KEYS / SCAN output. SCAN + UNLINK in one pass; surviving on a
        # single Redis hiccup is fine because the keys are dead anyway.
        try:
            legacy_keys = list(
                _graph_redis.scan_iter(match="rtie:logic:*")
            )
            if legacy_keys:
                _graph_redis.unlink(*legacy_keys)
            logger.info(
                "Phase 8 legacy cleanup: deleted %d rtie:logic:* key(s)",
                len(legacy_keys),
            )
        except Exception as exc:
            logger.warning(
                "Phase 8 legacy cleanup failed (non-fatal): %s", exc
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
        # canonical source cache) before falling through to Oracle /
        # disk. Without this wiring the Oracle/disk chain still runs
        # but every source fetch pays the Oracle round-trip.
        if _metadata_interpreter is not None:
            _metadata_interpreter.set_graph_redis_client(_graph_redis)

        # Phase 8: same client into CacheManager so the repointed
        # ``/cache-list`` and ``/cache-status`` slash commands can SCAN
        # ``graph:source:*`` and report on the loader cache without
        # going through the async cache_client.
        if _cache_manager is not None:
            _cache_manager.set_graph_redis_client(_graph_redis)

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

            # W58.d: prime the orchestrator's manifest-name exclusion set
            # so the function-precheck doesn't decline queries that mention
            # a process or sub_process name (e.g. "OPS_RISK_PROCESSING").
            from src.agents.orchestrator import refresh_process_subprocess_names
            try:
                proc_names = refresh_process_subprocess_names(_graph_redis)
                logger.info(
                    "W58.d exclusion set primed: %d process/sub_process names",
                    len(proc_names),
                )
            except Exception as exc:
                logger.warning(
                    "W58.d exclusion set refresh failed (non-fatal): %s", exc
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
    """Request body for the /v1/query and /v1/stream endpoints.

    Attributes:
        query: The user's natural language query or slash command.
        session_id: Unique session identifier for conversation continuity.
        engineer_id: Identifier for the requesting engineer.
        provider: LLM provider to use. Optional.
        model: Specific model name to use. Optional.
        schema_scope: W79 — user-driven schema selection from the UI
            dropdown. ``"ALL"`` (default) fans retrieval out across
            every discovered schema with per-schema top-K aggregation.
            A specific schema name (``"OFSMDM"`` / ``"OFSERM"`` /
            ``"FSDM"`` / ``"FSAPPS"``) restricts retrieval to that
            schema and overrides the LLM-inferred schema_name.
    """

    model_config = {"strict": True}

    query: str
    session_id: str
    engineer_id: str
    provider: Optional[str] = None
    model: Optional[str] = None
    schema_scope: str = "ALL"


def _build_diagnostic_block(state: Dict[str, Any]) -> Dict[str, Any]:
    """Extract W81/W70/W76 anchor state for the /v1/stream done event.

    Surfaces three internal anchor signals that are otherwise invisible
    to SSE consumers (benchmark harnesses, diagnostic scripts):

      * ``w81_suppressed`` — True iff the hierarchy-header renderer hit
        the W81 cross-process suppression branch
        (``logic_explainer._build_hierarchy_header``). Default False;
        the renderer never stamps False, so absence means no
        suppression. Always present as a bool.

      * ``w70_anchor`` — the function name from
        ``state["w70_anchor"]["function"]`` (cascade-resolved primary
        anchor passed to the explainer prompt by
        ``anchor_resolution.apply_w70_anchor``). ``None`` when the
        cascade didn't produce an anchor or when the response flow
        bypassed the explainer (e.g. variable-trace).

      * ``w76_anchor`` — the function name from
        ``state["w76_anchor"]["function"]`` (stamped by
        ``orchestrator.apply_named_function_anchor`` only when the
        ``"In <FunctionName>, …"`` prefix rule or alias-literal
        fallback fires). ``None`` for queries that don't match either
        mechanism. NOT the ``_w57_resolve_primary_function`` result —
        that helper picks a target per content check and doesn't
        stamp state.

    Empty strings (e.g. ``w76_anchor.function == ""`` after the
    cleared-alias branch in orchestrator) collapse to ``None`` for a
    clean string-or-null contract on the wire.
    """
    w70_raw = state.get("w70_anchor")
    w70_fn: Optional[str] = None
    if isinstance(w70_raw, dict):
        candidate = (w70_raw.get("function") or "").strip()
        w70_fn = candidate or None

    w76_raw = state.get("w76_anchor")
    w76_fn: Optional[str] = None
    if isinstance(w76_raw, dict):
        candidate = (w76_raw.get("function") or "").strip()
        w76_fn = candidate or None

    return {
        "w81_suppressed": bool(state.get("w81_suppressed", False)),
        "w70_anchor": w70_fn,
        "w76_anchor": w76_fn,
    }


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
            "schema_scope": _normalize_schema_scope(request.schema_scope),
            "schemas_searched": [],
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
            schema_scope = _normalize_schema_scope(request.schema_scope)
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
                "schema_scope": schema_scope,
                "schemas_searched": [],
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

            # W130 — pre-classifier W88 detect. The W88 named-computation
            # registry's decline arm (LCR, NSFR, Leverage Ratio) was being
            # shadowed at baseline: W87 caught LCR / NSFR as unrecognized
            # terms first, and the DATA_QUERY MIS-date gate caught Leverage
            # Ratio before W88 could fire. Running detect_named_computation
            # as the first orchestration step lets the static registry win
            # the routing decision when its patterns match — saving the
            # LLM classify call and surfacing W88's authoritative reason
            # text + alternative-metric suggestions (CAP214 for Leverage
            # Ratio). detect_named_computation is intentionally called
            # again downstream at data_query.py:328 — the function is
            # reused, not refactored. The downstream call covers the
            # normal DATA_QUERY-with-MIS-date path that never goes through
            # this hook (classifier emits DATA_QUERY, pipeline routes to
            # DataQueryAgent, W88 fires there). Do not "optimize" the
            # downstream call away. Baseline: F1 / F2 / F3 of
            # scratch/quality_harness_report_baseline.md. Closes the
            # documented W88b backlog (Weakness Log 2026-05-18).
            w88_pre = detect_named_computation(
                raw_query=request.query, query_type="DATA_QUERY",
            )
            if w88_pre is not None:
                logger.info(
                    "W130: W88 matched pre-classifier | computation=%s "
                    "arm=%s pattern=%r | correlation_id=%s",
                    w88_pre.definition.name,
                    w88_pre.definition.arm,
                    w88_pre.matched_pattern,
                    correlation_id,
                )
                state["query_type"] = "DATA_QUERY"
                state["w88_pre_detected"] = True
                yield (
                    "event: stage\ndata: "
                    + json_mod.dumps({
                        "stage": "classify",
                        "message": "Recognized named computation...",
                    })
                    + "\n\n"
                )
            else:
                # Stage 1: Classify (existing path, unchanged)
                yield f"event: stage\ndata: {json_mod.dumps({'stage': 'classify', 'message': 'Understanding your question...'})}\n\n"
                with stage_timer("orchestrator_classify", correlation_id):
                    state = await _orchestrator.classify_query(
                        request.query, state, provider=provider, model=model
                    )

                if state.get("partial_flag"):
                    yield f"event: done\ndata: {json_mod.dumps({'type': 'clarification', 'message': state.get('output', {}).get('message', 'Could you clarify?')})}\n\n"
                    return

                # W129 — post-classifier structural-question override. The
                # classifier over-fires DATA_QUERY on structural questions
                # ("what runs in December", "what functions update FCT_*"):
                # date-shape tokens and bare FCT_* table references both
                # trigger the DATA_QUERY heuristic, which then hits the
                # MIS-date gate and returns the wrong-shape "include a
                # date" clarification. W129 detects a narrow set of
                # structural shapes (P1 = "what/which <code-noun>
                # <data-op-verb>"; P2 = "what <run-verb> in/on/during
                # <time>") and overrides query_type to COLUMN_LOGIC so
                # the logic_explainer pipeline runs instead (same path
                # validated by C3's W127 post-fix routing).
                #
                # Override is POST-classifier (not pre-) so the classifier-
                # populated state (target_variable, schema, search_terms)
                # is preserved and feeds the explainer prompt correctly.
                # An earlier pre-classifier draft bypassed classify_query
                # and left these fields empty, which broke the explainer
                # LLM call with BadRequestError on E1 (35-function
                # retrieval + empty prompt slots). Pattern divergence
                # from W130's pre-classifier hook is justified: W88 paths
                # short-circuit to canned payloads (no downstream LLM
                # call); structural-question paths flow through the full
                # logic pipeline (LLM call inevitable, classifier cost
                # worth paying for clean state).
                #
                # Gated on query_type == "DATA_QUERY" so legitimate
                # classifier routes (VARIABLE_TRACE / COLUMN_LOGIC /
                # UNSUPPORTED / FUNCTION_LOGIC) are never overridden.
                # The W88 pre-detect path (state["w88_pre_detected"])
                # does not reach this branch — W130's `if` arm above
                # bypasses classify_query and skips this else block.
                # Baseline: E1 / E2 of
                # scratch/quality_harness_report_baseline.md.
                if state.get("query_type") == "DATA_QUERY":
                    w129_structural = detect_structural_question(request.query)
                    if w129_structural is not None:
                        logger.info(
                            "W129: structural override | was=DATA_QUERY "
                            "to=%s pattern=%r | correlation_id=%s",
                            w129_structural.suggested_route,
                            w129_structural.pattern,
                            correlation_id,
                        )
                        state["query_type"] = w129_structural.suggested_route
                        state["w129_structural"] = True

            # W79: when the user scoped to a specific schema in the UI,
            # the dropdown wins over the classifier's schema_name. The
            # LLM-inferred field is left as a soft signal (still on the
            # classifier output object) but state["schema"] — the value
            # every downstream call site reads — is rewritten to the user
            # choice. ALL mode is left alone here; the vector-search and
            # graph-pipeline branches below decide per-call whether to fan
            # out across discovered_schemas or constrain.
            if schema_scope != _SCHEMA_SCOPE_ALL:
                if state.get("schema") and state["schema"] != schema_scope:
                    logger.info(
                        "W79: user scope %s overrides classifier schema %s "
                        "(query=%r) | correlation_id=%s",
                        schema_scope, state["schema"],
                        request.query[:80], correlation_id,
                    )
                state["schema"] = schema_scope

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

            # --- W76: named-function anchor pre-rule. When the raw
            # query starts with "In <FunctionName>, ..." (or Inside /
            # Within / possessive variants), anchor the asked-about
            # object on that function regardless of what the classifier
            # returned for target_variable / object_name. Defends
            # against the classifier mistaking CASE-branch alias
            # literals (EXP_11, COND_5, ...) for the asked-about
            # object. Mechanism 2 backstop also fires when the
            # classifier put an alias literal into target_variable
            # while the query mentions a real function elsewhere.
            #
            # Runs BEFORE the date-range / Phase 2 / Option A / W37 /
            # BI routing branches so its query_type override (->
            # COLUMN_LOGIC) takes effect at every downstream gate. The
            # W79 schema_scope override (above) already settled
            # state["schema"]; W76 leaves schema alone — function-name
            # cross-scope detection is the W79 precheck's job.
            with stage_timer("named_function_anchor", correlation_id):
                _orchestrator.apply_named_function_anchor(state)

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

            # --- W79 cross-scope precheck (D2). When the user scoped to a
            # specific schema but the named function lives in a different
            # one, decline with a structured "wrong scope" response that
            # tells the user exactly which schema to switch to. Runs
            # BEFORE the W37 function-precheck so an exists-elsewhere
            # function gets the more specific scope-mismatch framing
            # rather than the generic "not found" framing. Two passes:
            # the function-name pass catches "How does FN_X work?"
            # queries; the BI pass catches "How is CAP973 calculated?"
            # queries before BI routing rewrites state["schema"].
            if state.get("query_type") in (
                "COLUMN_LOGIC", "VARIABLE_TRACE", "FUNCTION_LOGIC"
            ):
                with stage_timer("scope_mismatch_precheck", correlation_id):
                    scope_mismatch = _run_scope_mismatch_precheck(
                        request.query, schema_scope, correlation_id
                    )
                if scope_mismatch is None:
                    with stage_timer(
                        "scope_mismatch_precheck_bi", correlation_id
                    ):
                        scope_mismatch = _run_bi_scope_mismatch_precheck(
                            state, schema_scope, correlation_id
                        )
                if scope_mismatch is not None:
                    async for event in _stream_declined_response(scope_mismatch):
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

            # --- W87 unrecognized-term gate. When the user asked an
            # entity-seeking question (FUNCTION_LOGIC / COLUMN_LOGIC /
            # VARIABLE_TRACE) and every orchestrator-stage resolver
            # failed — no function name extracted, no BI routing, no
            # W76 anchor, no target_variable column match — short-circuit
            # with a structured "I don't know what this means" response
            # instead of feeding the concatenated enriched_query blob
            # (orchestrator.py:669) into semantic search, where the LLM
            # would fabricate an anchor on a name-similar but unrelated
            # function (the stakeholder-test-1 Q11 "G Test" failure).
            #
            # Sibling of W37 (function_not_found): pre-search, deterministic
            # body. UNVERIFIED rather than DECLINED — the system is asking
            # for clarification, not refusing the query.
            with stage_timer("w87_unrecognized_term_gate", correlation_id):
                w87_term = _detect_unrecognized_term_query(
                    state, request.query, _graph_redis,
                )
            if w87_term is not None:
                schemas_loaded = (
                    discovered_schemas(_graph_redis)
                    if _graph_redis is not None
                    else []
                )
                similar = (
                    find_similar_function_names(
                        w87_term, _graph_redis, top_n=3,
                    )
                    if _graph_redis is not None
                    else []
                )
                logger.info(
                    "W87 unrecognized-term gate fired: term=%r, "
                    "schemas_loaded=%s, similar=%s | correlation_id=%s",
                    w87_term, schemas_loaded, similar, correlation_id,
                )
                w87_payload = build_unrecognized_term_response(
                    term=w87_term,
                    similar_functions=similar,
                    schemas_loaded=schemas_loaded,
                    correlation_id=correlation_id,
                )
                async for event in _stream_unrecognized_term_response(
                    w87_payload
                ):
                    yield event
                return

            # Stage 2: Semantic search
            search_stage_msg = (
                "Searching across all schemas..."
                if schema_scope == _SCHEMA_SCOPE_ALL
                else f"Searching {schema_scope}..."
            )
            yield f"event: stage\ndata: {json_mod.dumps({'stage': 'search', 'message': search_stage_msg})}\n\n"
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
            # W80: prefer the clean anchor (W76 / BI routing); fall back to
            # raw_query — never the classifier blob, which used to be stamped
            # into object_name by classify_query and poisoned the embedding.
            search_query = resolve_search_query(state)
            with stage_timer("embedding_create", correlation_id):
                query_embedding = await embeddings.aembed_query(search_query)
            # W79: ALL fans out across every discovered schema with
            # per-schema top-K so a mediocre OFSMDM hit cannot crowd out
            # a strong OFSERM hit (global top-K would let it). Scoped
            # mode passes the schema through to the vector store as a
            # TAG pre-filter on the KNN clause.
            # W80b: per-query-type top-K — FUNCTION_LOGIC stays at 5
            # (anchored upstream), VARIABLE_TRACE / COLUMN_LOGIC raise
            # to capture multi-stage chains and dense column-writer
            # sets. See src/agents/retrieval_config.py.
            top_k = resolve_top_k(state.get("query_type"))
            with stage_timer(
                "vector_search", correlation_id,
                schema_scope=schema_scope, top_k=top_k,
            ):
                results, schemas_searched = await _run_scoped_vector_search(
                    query_embedding=query_embedding,
                    schema_scope=schema_scope,
                    top_k=top_k,
                )
            state["search_results"] = results
            state["schemas_searched"] = schemas_searched
            # W79: set state["schema"] for downstream callers that still
            # treat it as the request's primary schema. Scoped mode
            # already stamped it; for ALL mode prefer the top-ranked
            # result's schema, falling back to the legacy default when
            # nothing matched.
            if not state.get("schema"):
                if results and results[0].get("schema"):
                    state["schema"] = results[0]["schema"]
                else:
                    state["schema"] = fallback_to_default_schema(
                        "main.semantic_search", correlation_id,
                    )

            # W80c: hybrid graph + vector rerank. 1-hop expansion from
            # the top-3 vector hits via the cross-function edges already
            # persisted at graph:full:<schema>, then RRF fuses cosine
            # rank with edge-derived signals so multi-stage chain
            # functions (the significant-investment canary's 3 missing
            # targets ranked 8/12/18 by pure cosine) climb into the
            # surfacing set. Gated on VARIABLE_TRACE / COLUMN_LOGIC and
            # on _graph_redis availability. Best-effort — any failure
            # leaves search_results untouched. Stamps
            # state["graph_rerank_stats"] so the meta event surfaces
            # seed/expanded/rank_change counts for canary measurement.
            # Must run BEFORE ensure_anchor_in_search_results so W95's
            # position-0 injection isn't displaced by the rerank.
            apply_w80c_rerank(
                state,
                redis_client=_graph_redis,
                correlation_id=correlation_id,
                schema_scope=schema_scope,
            )

            # W95: anchor resolution (W76 / BI routing) must be reflected
            # in downstream retrieval, not just embedding bias. When the
            # anchored function ranked outside the vector-search top-K
            # (CAP973 → CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT
            # is the canonical case), force-inject it at position 0 so
            # fetch_multi_logic loads its body. Refresh the local
            # `results` reference so the downstream stage-event preview
            # and fn_names match state["search_results"].
            ensure_anchor_in_search_results(state)
            results = state["search_results"]

            # Stage 3: Fetch source code
            fn_names = list(dict.fromkeys(r["function_name"] for r in results)) if results else []
            yield f"event: stage\ndata: {json_mod.dumps({'stage': 'fetch', 'message': f'Reading source code for {len(fn_names)} functions...', 'functions': fn_names})}\n\n"
            with stage_timer("metadata_fetch_multi", correlation_id, functions=len(fn_names)):
                state = await _metadata_interpreter.fetch_multi_logic(state)

            # W89: for VARIABLE_TRACE queries, reorder multi_source by
            # manifest task_order BEFORE emitting the meta event so the
            # user-visible functions_analyzed array and the chain the
            # narrative LLM receives are both in execution order. Other
            # query types (FUNCTION_LOGIC, COLUMN_LOGIC, ...) retain the
            # semantic-rank order they had pre-W89 — only the
            # variable-trace narrative is order-sensitive in a way the
            # alphabetical fallback misframed.
            if state.get("query_type") == "VARIABLE_TRACE" and _graph_redis is not None:
                with stage_timer("w89_chain_reorder", correlation_id):
                    state["multi_source"] = reorder_multi_source(
                        state.get("multi_source", {}) or {},
                        redis_client=_graph_redis,
                    )

            # W97: extend the W95 architectural principle one stage
            # downstream. W95 force-includes the anchored function in
            # search_results when it was missing; W97 promotes it to
            # multi_source position 0 when it was present but ranked low
            # (W80c-v2's wider retrieval window now surfaces anchors at
            # rank 30 only for the LLM to drift to position 0 instead).
            # Anchor resolution must dominate both retrieval coverage
            # (W95) and prompt prominence (W97) — the LLM anchors on
            # whatever sits first in the source pile regardless of the
            # system-prompt anchor block. Runs AFTER reorder_multi_source
            # so anchor-first beats manifest task_order when they
            # disagree. Also stamps state["w70_anchor"] for diagnostics
            # — the duplicate apply_w70_anchor call inside
            # stream_semantic is idempotent (determine_primary_anchor is
            # deterministic on a fixed state).
            with stage_timer("w97_promote_anchor", correlation_id):
                w70_anchor = apply_w70_anchor(state)
                state["multi_source"] = promote_anchor_to_front(
                    state.get("multi_source", {}) or {},
                    w70_anchor,
                )

            # W92: stamp the per-turn list of schemas whose bodies were
            # fetched into multi_source. This is what the LLM-rendered
            # body actually cites, regardless of which schema the
            # primary anchor (state["schema"]) belongs to. Computed once
            # here (post-W97 promote-to-front so order is settled);
            # downstream done_payload reads it from state.
            cited_schemas = _compute_cited_schemas(state.get("multi_source"))
            state["cited_schemas"] = cited_schemas

            # Send metadata event
            meta = {
                "schema": state.get("schema", ""),
                "object_name": state.get("object_name", "")[:100],
                "query_type": state.get("query_type", ""),
                "functions_analyzed": list(state.get("multi_source", {}).keys()),
                # W79: schemas that returned at least one candidate. The UI
                # renders this as a chip so users can confirm where the
                # answer came from. Single-element list for scoped queries,
                # multi-element for ALL fan-out hits.
                "schema_searched": list(state.get("schemas_searched", []) or []),
                # W92: schemas whose source bodies the response actually
                # cites. Distinct from schema_searched (retrieval
                # coverage) and schema (primary anchor) — closes the
                # heading-vs-body mismatch when multi_source spans
                # schemas.
                "cited_schemas": cited_schemas,
                "schema_scope": schema_scope,
                "correlation_id": correlation_id,
                # W80c telemetry: status + (when status=ok) seed_count,
                # expanded_count, kept_count, rank_change_count. Always
                # populated post-vector-search; status="skipped_*" when
                # the gate was closed (FUNCTION_LOGIC, no redis, etc.).
                "graph_rerank": state.get("graph_rerank_stats") or {},
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
                        # Phase 8: schema-agnostic next-step boilerplate.
                        # Snapshot the live discovered_schemas list so the
                        # response always names the schemas RTIE actually
                        # has indexed.
                        discovered_schemas=(
                            discovered_schemas(_graph_redis)
                            if _graph_redis is not None
                            else None
                        ),
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
                        # W89: pass functions_source's iteration order
                        # (already manifest-reordered above before the
                        # meta event) so the chain text walks functions
                        # in execution order rather than alphabetical.
                        chain_text = _variable_tracer.build_transformation_chain(
                            target_var, tagged, seeds,
                            function_order=list(functions_source.keys()),
                        )
                    # W92: align with W97's promote-to-front contract — schema of the position-0 multi_source entry, not state["schema"]
                    _w92_anchor = state.get("w70_anchor") or None
                    _w92_anchor_fn = (_w92_anchor or {}).get("function") or ""
                    _w92_ms = state.get("multi_source") or {}
                    _w92_heading_schema = ""
                    if _w92_anchor_fn:
                        _w92_anchor_fn_upper = _w92_anchor_fn.upper()
                        for _ms_fn, _ms_entry in _w92_ms.items():
                            if _ms_fn.upper() == _w92_anchor_fn_upper:
                                _w92_heading_schema = (_ms_entry or {}).get("schema") or ""
                                break
                    if not _w92_heading_schema:
                        _w92_heading_schema = state.get("schema", "")
                    with stage_timer("llm_stream_variable_trace", correlation_id):
                        _first_token = True
                        async for token in _variable_tracer.stream_chain(
                            target_var, chain_text, request.query, provider, model,
                            schema=_w92_heading_schema,
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
                    # W76b: forward the orchestrator's anchor so the
                    # NAMED_FUNCTION_NOT_RETRIEVED check + post-hoc
                    # Caveats appender consult it instead of re-
                    # extracting from raw_query (which would latch
                    # onto alias literals like EXP_11).
                    w76_anchor=state.get("w76_anchor") or {},
                    # W83B: forward W70's cascade-resolved anchor so
                    # the calendar-gating check consults the same
                    # function the explainer prompt anchored on. Set
                    # by apply_w70_anchor during stream_semantic;
                    # absent on variable-trace-only paths.
                    w70_anchor=state.get("w70_anchor") or None,
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

            # W108: when stream_semantic's source-concat cap fired, surface
            # a user-visible W108-TRUNCATED warning and force UNVERIFIED.
            # The response was built on a subset of retrieved candidates,
            # so it cannot be VERIFIED — the dropped (lower-ranked)
            # candidates might have contained the actual answer.
            #
            # W134: also cap confidence at 0.4. Pre-W134, the W108 block
            # left confidence untouched on the rationale "grounding's own
            # calculation accounts for evidence quality" — but that
            # calculation is a 5-bucket lookup on (badge, has_citations),
            # not a quality measure (see scratch/w134_audit_findings.md).
            # As a result a clean response that got truncated could ship
            # as UNVERIFIED + 0.95 (B3 in the P1 harness exhibited this).
            # The cap mirrors the bucket the formula would have produced
            # had W108-TRUNCATED been present in `warnings` BEFORE the
            # formula ran (blocking_warnings non-empty + citations → 0.4).
            w108_truncation = state.get("w108_truncation")
            if w108_truncation:
                kept = w108_truncation["kept"]
                total = w108_truncation["total"]
                dropped_count = total - kept
                grounding["warnings"].append(
                    f"W108-TRUNCATED: response based on {kept} of {total} "
                    f"retrieved functions; {dropped_count} lower-ranked "
                    "candidates were dropped to fit the model's context "
                    "budget. Narrow your query if you need full coverage."
                )
                grounding["badge"] = "UNVERIFIED"
                if grounding["confidence"] > 0.4:
                    grounding["confidence"] = 0.4

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
                # W79: schemas that contributed candidates this turn.
                "schema_searched": list(state.get("schemas_searched", []) or []),
                # W92: emit `schema` here too. App.jsx merges meta into
                # data AFTER the done payload (frontend/src/App.jsx:135-141),
                # so without this key the merged `data.schema` came
                # from the meta event only. Having both halves carry
                # the same value makes the merge a no-op and closes the
                # observability gap where `data.schema` could disagree
                # with the body content. Same anchor that meta uses.
                "schema": state.get("schema", ""),
                # W92: cited_schemas is the honest list of schemas whose
                # bodies the response cites — derived once at the meta
                # event from multi_source. UI / benchmark consumers can
                # detect a label-vs-body mismatch by checking that
                # `schema` is in `cited_schemas` (or that cited_schemas
                # has length 1).
                "cited_schemas": state.get("cited_schemas") or [],
                "schema_scope": schema_scope,
                "correlation_id": correlation_id,
                "explanation": {
                    "markdown": final_markdown,
                    "summary": final_markdown[:200],
                },
                # W84: expose W81/W70/W76 anchor state so benchmark
                # tooling can measure suppression firing rate and
                # anchor preservation without re-deriving them from
                # logs. Additive; consumers that only read the
                # existing fields are unaffected.
                "diagnostic": _build_diagnostic_block(state),
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
    schemas_searched = [schema] if schema else []
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
        # W79: phase2 routes to a single schema (state["schema"] which
        # the user-scope override or pivot already settled on), so the
        # UI chip shows that one schema.
        "schema_searched": schemas_searched,
        # W92: Phase 2 routes to a single schema, so cited_schemas is
        # the same single-element list. Emitted for shape symmetry
        # with FUNCTION_LOGIC / DATA_QUERY so consumers can read one
        # field across all three routes.
        "cited_schemas": schemas_searched,
        "schema_scope": state.get("schema_scope") or _SCHEMA_SCOPE_ALL,
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
        "schema_searched": schemas_searched,
        # W92: symmetric with the meta event — Phase 2 cites a single
        # schema, so the list has one element (or zero when schema
        # could not be resolved).
        "cited_schemas": schemas_searched,
        "schema_scope": state.get("schema_scope") or _SCHEMA_SCOPE_ALL,
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


def _compute_cited_schemas(multi_source: Optional[Dict[str, Any]]) -> list:
    """W92: derive the sorted set of distinct schemas in multi_source.

    ``multi_source`` is a ``{function_name: entry}`` dict where each
    entry carries the per-function source schema (stamped by
    ``MetadataInterpreter.fetch_logic_multi``). Two functions from
    different schemas in the same response produce a two-element list;
    one schema produces a single-element list; an empty / missing
    multi_source produces ``[]``.

    Sorted for deterministic SSE payloads (snapshot-style tests + the
    canary driver compare equality, not set membership).
    """
    if not multi_source:
        return []
    return sorted({
        entry["schema"]
        for entry in multi_source.values()
        if isinstance(entry, dict) and entry.get("schema")
    })


# W79: canonical scope tokens accepted from the request body. The frontend
# already sends one of these (the "all" UI option maps to "ALL"); this set
# is the single place where backend-side validation lives. Anything else
# falls back to "ALL" so a malformed request degrades to the safe default
# rather than 400ing out.
_SCHEMA_SCOPE_ALL = "ALL"
_SCHEMA_SCOPE_VALUES: frozenset[str] = frozenset(
    {_SCHEMA_SCOPE_ALL, "OFSMDM", "OFSERM", "FSDM", "FSAPPS"}
)


async def _run_scoped_vector_search(
    *,
    query_embedding: list,
    schema_scope: str,
    top_k: int = 5,
) -> tuple[list[Dict[str, Any]], list[str]]:
    """W79 retrieval dispatcher — ALL fan-out vs. single-schema scope.

    Returns a ``(results, schemas_contributed)`` tuple:

    * ``results`` is the list of search hits the rest of the pipeline
      consumes. Each hit already carries its source schema (the vector
      store stamps ``schema`` on every doc since Phase 3).
    * ``schemas_contributed`` is the deduped list of schemas that
      actually returned at least one hit, used for the meta + done SSE
      events so the UI can show "Schema: OFSERM" / "Schemas: OFSMDM,
      OFSERM" / etc.

    For ``schema_scope == "ALL"``: iterate every discovered schema and
    issue an independent top-K KNN per schema. Aggregating by global
    cosine score would let one schema's mediocre matches crowd out
    another schema's good ones — top-K-per-schema preserves the
    strongest candidates from each side.

    For a specific schema: pass ``schema_filter`` to the vector store so
    the KNN runs against the pre-filtered ``@schema:{<name>}`` slice
    only. Identical to the pre-W79 behaviour scoped to one schema.
    """
    if schema_scope == _SCHEMA_SCOPE_ALL:
        # Snapshot the live schema list once. Falls back to the
        # manifest set when graph-Redis is unavailable.
        all_schemas = (
            discovered_schemas(_graph_redis)
            if _graph_redis is not None
            else []
        )
        if not all_schemas:
            # Pre-loader / clean-Redis case: degrade to the unfiltered
            # search so the request still returns something.
            hits = await _vector_store.search(
                query_embedding=query_embedding, top_k=top_k
            )
            contributed = sorted({h.get("schema") for h in hits if h.get("schema")})
            return hits, contributed

        aggregated: list[Dict[str, Any]] = []
        contributed: list[str] = []
        for schema in all_schemas:
            try:
                hits = await _vector_store.search(
                    query_embedding=query_embedding,
                    top_k=top_k,
                    schema_filter=schema,
                )
            except Exception as exc:
                logger.warning(
                    "W79 ALL fan-out: vector search failed for %s: %s",
                    schema, exc,
                )
                continue
            if not hits:
                continue
            aggregated.extend(hits)
            contributed.append(schema)
        return aggregated, contributed

    # Specific-schema mode.
    hits = await _vector_store.search(
        query_embedding=query_embedding,
        top_k=top_k,
        schema_filter=schema_scope,
    )
    contributed = [schema_scope] if hits else []
    return hits, contributed


_W80C_RERANK_QUERY_TYPES: frozenset[str] = frozenset({"VARIABLE_TRACE", "COLUMN_LOGIC"})


def apply_w80c_rerank(
    state: Dict[str, Any],
    *,
    redis_client: Any,
    correlation_id: str,
    schema_scope: str,
) -> None:
    """W80c — best-effort hybrid graph + vector rerank of ``search_results``.

    Calls :func:`rerank_with_rrf` against the cross-function edges already
    persisted at ``graph:full:<schema>`` (built once at loader time). The
    rerank surfaces 1-hop neighbors of the top-3 vector hits and fuses
    cosine rank with edge-derived signals (matching_columns,
    seed_reach_count, same_sub_process_path) via Reciprocal Rank Fusion.

    Gating (mirrors the W80c Stage 1 diagnostic Section 7 Q2 decision):
      * ``query_type`` must be ``VARIABLE_TRACE`` or ``COLUMN_LOGIC`` —
        FUNCTION_LOGIC is anchored upstream and gains nothing.
      * ``redis_client`` must be non-None — no Redis means no edge index.
      * ``state["search_results"]`` must be non-empty — nothing to rerank.

    Best-effort augmentation: any exception is caught, logged at WARNING,
    and ``state["search_results"]`` is left unchanged. The stats dict is
    stamped on ``state["graph_rerank_stats"]`` for downstream telemetry
    (meta event, weakness-log calibration). On skip or failure the stats
    block records the reason so canary readers can distinguish "did not
    run" from "ran but coasted".

    The ``keep_top`` budget is ``resolve_top_k(query_type) + 10`` —
    diagnostic Q4 picked the +10 cap on top of W80b's per-query-type
    top_k.
    """
    query_type = state.get("query_type") or ""

    if query_type not in _W80C_RERANK_QUERY_TYPES:
        state["graph_rerank_stats"] = {"status": "skipped_query_type"}  # type: ignore[typeddict-item]
        return
    if redis_client is None:
        state["graph_rerank_stats"] = {"status": "skipped_no_redis"}  # type: ignore[typeddict-item]
        return
    hits = state.get("search_results") or []
    if not hits:
        state["graph_rerank_stats"] = {"status": "skipped_empty_results"}  # type: ignore[typeddict-item]
        return

    # PR 2 retune (2026-05-18): per-seed cap of 20 holds expansion
    # blast radius. First wire-in canary measured 137 expansion
    # candidates from 3 FCT_ENTITY_INFO-touching seeds and pushed a
    # strong-cosine top-1 hit (T1) out of the keep_top=25 window;
    # cap=20 trims the long tail of 0-col passthrough neighbours
    # while keeping every load-bearing edge.
    #
    # W80c-v2 (2026-05-18): keep_top offset lifted +10 → +20 to chase
    # T3 (SIGNIFICANT_INVESTMENT_IN_PARTY_FOR_REPORTING_BANK_IDENTIFICATION).
    # Diagnostic showed T3 expanded via T1 (cap-20 sort-rank 7 within
    # T1's 114-neighbour list) but was scored at the margin of the
    # PR 2 keep_top=25 window. Doesn't shift any RRF rank — just
    # keeps 10 more candidates. Fetch cost ~+40% per VARIABLE_TRACE /
    # COLUMN_LOGIC, bounded acceptable.
    keep_top = resolve_top_k(query_type) + 20
    per_seed_cap = 20
    try:
        with stage_timer(
            "graph_rerank", correlation_id,
            schema_scope=schema_scope, query_type=query_type,
        ):
            reranked, stats = rerank_with_rrf(
                hits,
                redis_client=redis_client,
                seed_count=3,
                keep_top=keep_top,
                per_seed_cap=per_seed_cap,
            )
        state["search_results"] = reranked
        state["graph_rerank_stats"] = {"status": "ok", **stats}  # type: ignore[typeddict-item]
        # Stats line as a second log entry — stage_timer can only carry
        # kwargs known at entry, but seed_count / expanded_count /
        # rank_change_count are only known after the call returns. The
        # canary parses this line out of logs/app.log if needed.
        try:
            logger.info(
                "[GRAPH_RERANK_STATS] correlation_id=%s schema_scope=%s "
                "query_type=%s seed_count=%d expanded_count=%d "
                "kept_count=%d rank_change_count=%d keep_top=%d",
                correlation_id, schema_scope, query_type,
                stats["seed_count"], stats["expanded_count"],
                stats["kept_count"], stats["rank_change_count"],
                keep_top,
            )
        except Exception:
            pass
    except Exception as exc:
        # Best-effort: leave search_results untouched, record failure so
        # the canary can spot a regression even when the user-visible
        # response still renders.
        logger.warning(
            "apply_w80c_rerank failed (best-effort; leaving "
            "search_results unchanged): %s", exc,
        )
        state["graph_rerank_stats"] = {  # type: ignore[typeddict-item]
            "status": "error",
            "error": type(exc).__name__,
        }


def _normalize_schema_scope(raw: Optional[str]) -> str:
    """Coerce an inbound schema_scope value to a canonical token.

    Empty / None / unrecognized values fall back to ``"ALL"``. The
    comparison is case-insensitive on input but always returns the
    upper-case canonical form, so downstream code can use ``==`` against
    the constants in :data:`_SCHEMA_SCOPE_VALUES` without worrying about
    casing drift between the UI ("all") and Redis-stored schema names
    ("OFSMDM", "OFSERM").
    """
    if not raw:
        return _SCHEMA_SCOPE_ALL
    upper = str(raw).strip().upper()
    if upper in _SCHEMA_SCOPE_VALUES:
        return upper
    return _SCHEMA_SCOPE_ALL


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
    # W130: W88 pre-detected paths don't need a user-supplied MIS date.
    # Anchor-arm SQL uses DENSE_RANK on the latest run; decline-arm payloads
    # are hand-built and emit no SQL. The MIS-date clarification is for
    # LLM-generated SQL paths that can't safely default a date scope.
    w88_pre_detected = bool(state.get("w88_pre_detected"))
    if (
        require_mis_date
        and not filters.get("mis_date")
        and not has_date_range
        and not w88_pre_detected
    ):
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

    # W34a: stage events are emitted progressively from inside
    # answer_stream() at the TRUE start of each sub-stage. The pre-W34a
    # upfront cluster ("Building schema catalog + generating SQL..." +
    # "Executing read-only query..." both firing 5+s before either work
    # actually started) lied about progress and has been removed — the
    # generator now yields each stage right as that sub-stage begins.

    result = None
    try:
        with stage_timer("data_query_answer", correlation_id):
            async for kind, *payload in _data_query.answer_stream(
                user_query=user_query,
                schema=schema,
                filters=filters,
                provider=provider,
                model=model,
                target_variable=(state.get("target_variable") or None),
            ):
                if kind == "stage":
                    stage_name, message = payload
                    yield (
                        "event: stage\ndata: "
                        f"{json_mod.dumps({'stage': stage_name, 'message': message})}\n\n"
                    )
                elif kind == "result":
                    result = payload[0]
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

    if result is None:
        # Generator exhausted without producing a terminal result. This
        # should not happen in practice — answer_stream always yields a
        # ("result", payload) before returning — but guard against it
        # rather than dereferencing None below.
        logger.error(
            "DATA_QUERY generator exhausted without a result | correlation_id=%s",
            correlation_id,
        )
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

    # W79: surface the schema DataQueryAgent actually executed against.
    # When ALL was selected, the agent may have pivoted via Phase 4
    # `schemas_for_table` — the resolved schema is the source of truth
    # here, not the orchestrator-classified default.
    routed_schema = result.get("schema") or schema
    schemas_searched = [routed_schema] if routed_schema else []
    # W92: DATA_QUERY routes to one schema (the agent already pivoted
    # via Phase 4 if it had to), so cited_schemas mirrors that single
    # routed schema. Kept symmetric with FUNCTION_LOGIC / Phase 2 so
    # consumers can rely on cited_schemas always being present.
    cited_schemas = [routed_schema] if routed_schema else []
    meta = {
        # Phase 4: prefer the schema DataQueryAgent actually routed to —
        # may differ from the orchestrator-classified `schema` when the
        # user named an OFSERM table on a default-OFSMDM request.
        "schema": routed_schema,
        "query_type": "DATA_QUERY",
        "status": result.get("status"),
        "query_kind": result.get("query_kind"),
        "row_count": result.get("row_count"),
        "schema_searched": schemas_searched,
        "cited_schemas": cited_schemas,
        "schema_scope": state.get("schema_scope") or _SCHEMA_SCOPE_ALL,
        "correlation_id": correlation_id,
    }
    yield f"event: meta\ndata: {json_mod.dumps(meta, default=str)}\n\n"

    # W34a: the "explain" stage event was emitted from inside
    # answer_stream() before _build_explanation ran. The previous redundant
    # post-result emission has been removed.

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
        "schema_searched": schemas_searched,
        # W92: symmetric with meta — DataQueryAgent has already pivoted
        # so the list reflects the schema we actually executed against.
        "cited_schemas": cited_schemas,
        "schema_scope": state.get("schema_scope") or _SCHEMA_SCOPE_ALL,
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
    # W88: propagate pre-router metadata when present. Both fields are
    # absent on non-W88 DATA_QUERY responses; canaries assert their
    # presence/absence as the W88 routing signal.
    if result.get("w88_anchor"):
        done_payload["w88_anchor"] = result["w88_anchor"]
    if result.get("w88_decline"):
        done_payload["w88_decline"] = result["w88_decline"]
    with stage_timer("done_emit", correlation_id, route="data_query"):
        yield f"event: done\ndata: {json_mod.dumps(done_payload, default=str)}\n\n"


def _build_scope_mismatch_response(
    *,
    requested_function: str,
    scoped_schema: str,
    other_schemas: List[str],
    correlation_id: str,
) -> Dict[str, Any]:
    """W79: assemble a structured "wrong scope" DECLINED response.

    Mirrors the W37/W45 ``function_not_found`` shape so the frontend's
    DECLINED/UNVERIFIED rendering paths don't need new branching. The
    body explains the mismatch in user-facing terms and points at the
    schema the named function actually lives in, plus the dropdown
    action the user needs to take.
    """
    schema_listing = ", ".join(other_schemas) if other_schemas else "another schema"
    parts = [
        f"`{requested_function}` is not in **{scoped_schema}**.",
        "",
        f"It exists in {schema_listing}. Switch the schema scope in the "
        "composer (or set it to **All schemas**) and re-run the query.",
    ]
    message = "\n".join(parts)
    return {
        "type": "scope_mismatch",
        "status": "declined",
        "requested_function": requested_function,
        "requested_schema": scoped_schema,
        "available_schemas": other_schemas,
        "validated": False,
        "badge": "DECLINED",
        "confidence": 0.0,
        "source_citations": [],
        "warnings": [
            f"SCOPE_MISMATCH: {requested_function} is not indexed in "
            f"{scoped_schema}; it lives in "
            f"{schema_listing}."
        ],
        "message": message,
        "explanation": {"markdown": message, "summary": message[:200]},
        # W79: schemas_searched is empty because nothing matched in
        # scope. The UI hides the chip when the list is empty.
        "schema_searched": [],
        "schema_scope": scoped_schema,
        "correlation_id": correlation_id,
    }


def _run_scope_mismatch_precheck(
    query: str, schema_scope: str, correlation_id: str
) -> Optional[Dict[str, Any]]:
    """W79 D2 cross-scope detection.

    Fires when:
      * the user scoped to a specific schema (not ALL)
      * the query names a PL/SQL function (extract_function_candidates)
      * the function is NOT indexed in the scoped schema, BUT
      * it IS indexed in at least one other discovered schema

    In that case the existing pipeline would otherwise either decline
    via the W37 function-precheck (when graph_redis is None) or
    silently retrieve nothing useful from the scoped schema's vector
    slice. Returning a structured response tells the user exactly how
    to recover (switch scope) instead of returning a confidently-empty
    answer.

    Returns None when the precondition fails (ALL mode, no graph
    Redis, no named function, function exists in scope, function
    exists nowhere — the W37 function-precheck handles the last case).
    """
    if schema_scope == _SCHEMA_SCOPE_ALL:
        return None
    if _graph_redis is None:
        return None
    candidates = extract_function_candidates(query)
    if not candidates:
        return None

    # W79: only fire when the named function genuinely lives in another
    # schema. Iterate candidates so a query naming both an OFSMDM and
    # an OFSERM function (rare) still fires for the first scope-violating
    # name.
    for candidate in candidates:
        if function_exists_in_graph(
            candidate, _graph_redis, schemas=[schema_scope]
        ):
            continue  # exists in the user's scope — no mismatch
        # Find every other schema where it does live.
        other_schemas: list[str] = []
        for sch in discovered_schemas(_graph_redis):
            if sch == schema_scope:
                continue
            if function_exists_in_graph(candidate, _graph_redis, schemas=[sch]):
                other_schemas.append(sch)
        if not other_schemas:
            # Function exists nowhere — let function_precheck (W37)
            # handle it with its own response shape so we don't
            # double-decline.
            continue
        logger.info(
            "W79 scope mismatch: function=%s scoped=%s other=%s | "
            "correlation_id=%s",
            candidate, schema_scope, other_schemas, correlation_id,
        )
        return _build_scope_mismatch_response(
            requested_function=candidate,
            scoped_schema=schema_scope,
            other_schemas=other_schemas,
            correlation_id=correlation_id,
        )
    return None


def _run_bi_scope_mismatch_precheck(
    state: LogicState, schema_scope: str, correlation_id: str
) -> Optional[Dict[str, Any]]:
    """W79 D2 cross-scope detection for business-identifier (BI) queries.

    Companion to :func:`_run_scope_mismatch_precheck` — the function-name
    detector handles "How does FN_X work?" queries; this one handles
    "How is CAP973 calculated?" queries where the user names a CAP-code
    (or other configured business identifier) instead of a function.

    BI routing (W35 Phase 7) normally rewrites ``state["schema"]`` to the
    schema whose literal index owns the identifier. Under W79 the user's
    dropdown choice must take priority: when scoped to a specific schema
    that doesn't own the identifier, return a structured scope-mismatch
    response pointing at the schema that does, instead of letting BI
    silently pivot.

    Mirrors the gating in
    :func:`src.agents.orchestrator.apply_bi_routing`:
      * fires only for COLUMN_LOGIC / FUNCTION_LOGIC / VARIABLE_TRACE
      * VARIABLE_TRACE checks the ``target_variable`` field, others
        scan ``raw_query``
      * an explicit function-name override in the query short-circuits
        to None (the user's named function wins, same as BI's own rule)

    Returns None when:
      * ALL mode (no scope set)
      * graph Redis unavailable
      * query type isn't BI-eligible
      * no BI identifier in the query
      * the identifier resolves under the scoped schema (BI runs as
        normal)
      * the identifier resolves nowhere (let downstream paths handle)
      * the user named a function that exists under the scoped schema
        (explicit-name override beats BI scope-mismatch, same as it
        beats BI itself)
    """
    if schema_scope == _SCHEMA_SCOPE_ALL:
        return None
    if _graph_redis is None:
        return None

    qt = state.get("query_type", "") or ""
    raw_query = state.get("raw_query", "") or ""

    if qt in ("COLUMN_LOGIC", "FUNCTION_LOGIC"):
        haystack = raw_query
    elif qt == "VARIABLE_TRACE":
        haystack = (state.get("target_variable") or "").strip()
        if not haystack:
            return None
    else:
        return None

    # Explicit-function-name override — when the user names a function
    # that exists under their chosen scope, that function wins over any
    # BI identifier also present in the query (same semantics BI itself
    # uses internally before resolving CAP-codes).
    candidates = extract_function_candidates(raw_query)
    for cand in candidates:
        if function_exists_in_graph(
            cand, _graph_redis, schemas=[schema_scope]
        ):
            return None

    bi_patterns = getattr(_orchestrator, "_bi_patterns", None)
    identifiers = detect_business_identifiers(haystack, bi_patterns)
    if not identifiers:
        return None

    primary = identifiers[0]
    # Does the identifier resolve under the user's chosen scope? If so,
    # BI routing is fine — let it run.
    in_scope = resolve_bi_to_function(
        primary, _graph_redis, schemas=[schema_scope]
    )
    if in_scope is not None:
        return None

    # Identifier doesn't live in the scoped schema. Find every other
    # schema where it DOES live so the response can point the user at
    # the right scope.
    other_schemas: list[str] = []
    for sch in discovered_schemas(_graph_redis):
        if sch == schema_scope:
            continue
        resolved = resolve_bi_to_function(
            primary, _graph_redis, schemas=[sch]
        )
        if resolved is not None:
            other_schemas.append(sch)
    if not other_schemas:
        # Identifier is configured (matched a BI pattern) but doesn't
        # live anywhere. Let the normal flow run — semantic search will
        # produce its usual W45 / empty-retrieval response.
        return None

    logger.info(
        "W79 BI scope mismatch: identifier=%s scoped=%s other=%s | "
        "correlation_id=%s",
        primary, schema_scope, other_schemas, correlation_id,
    )
    return _build_scope_mismatch_response(
        requested_function=primary,
        scoped_schema=schema_scope,
        other_schemas=other_schemas,
        correlation_id=correlation_id,
    )


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


async def _stream_unrecognized_term_response(payload: Dict[str, Any]):
    """Yield a W87 UNRECOGNIZED_TERM response as SSE tokens + meta + done events.

    Parallels :func:`_stream_declined_response` (W37 / scope_mismatch shape)
    but ships an UNVERIFIED badge — the system is asking the user to
    clarify, not refusing the query. The frontend renders ``warnings`` and
    ``badge`` the same way it does for any other UNVERIFIED response, plus
    the message markdown for the structured body.
    """
    term = payload.get("requested_term") or ""
    stage_message = f"Unrecognized term — clarification needed"
    meta = {
        "type": payload.get("type", "unrecognized_term"),
        "status": payload.get("status", "unverified"),
        "badge": payload.get("badge", "UNVERIFIED"),
        "validated": payload.get("validated", False),
        "confidence": payload.get("confidence", 0.2),
        "warnings": payload.get("warnings") or [],
        "requested_term": term,
        "similar_functions": payload.get("similar_functions") or [],
        "schemas_searched": payload.get("schemas_searched") or [],
        "correlation_id": payload.get("correlation_id"),
    }
    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'classify', 'message': stage_message})}\n\n"
    yield f"event: meta\ndata: {json_mod.dumps(meta)}\n\n"
    message = payload.get("message") or ""
    for chunk in _chunk_text(message):
        yield f"event: token\ndata: {json_mod.dumps(chunk)}\n\n"
    yield f"event: done\ndata: {json_mod.dumps(payload)}\n\n"


async def _stream_declined_response(payload: Dict[str, Any]):
    """Yield a DECLINED response as SSE tokens + meta + done events.

    Shared by the W37 ``function_not_found`` and the W79 ``scope_mismatch``
    branches — both produce the same SSE event sequence (stage → meta →
    tokens → done) but describe a different problem.
    """
    response_type = payload.get("type", "function_not_found")
    if response_type == "scope_mismatch":
        stage_message = "Named function lives in a different schema"
    else:
        stage_message = "Named function not found in graph"
    meta = {
        "type": response_type,
        "status": "declined",
        "requested_function": payload.get("requested_function"),
        "similar_functions": payload.get("similar_functions") or [],
        # W79: scope_mismatch carries a list of schemas where the named
        # function actually lives; surface those so the UI can render
        # them in the same "schema indicator" chip path the normal flow
        # uses for schemas_searched.
        "available_schemas": payload.get("available_schemas") or [],
        "schema_searched": payload.get("schema_searched") or [],
        "schema_scope": payload.get("schema_scope") or "",
        "correlation_id": payload.get("correlation_id"),
    }
    yield f"event: stage\ndata: {json_mod.dumps({'stage': 'classify', 'message': stage_message})}\n\n"
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
    elif command == "cache-status":
        target = args[0] if args else None
        return await _cache_manager.get_cache_status(target, schema)
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
