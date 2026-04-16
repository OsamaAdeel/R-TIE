"""
RTIE FastAPI Application.

Provides the HTTP API layer for the Regulatory Trace & Intelligence Engine.
Endpoints include POST /v1/query for logic explanation, GET /health for
dependency status checks, and GET /v1/models for listing available LLM
providers. All queries flow through semantic vector search.
"""

import asyncio
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
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.agents.orchestrator import Orchestrator
from src.agents.metadata_interpreter import MetadataInterpreter
from src.agents.logic_explainer import LogicExplainer
from src.agents.variable_tracer import VariableTracer
from src.agents.validator import Validator
from src.agents.cache_manager import CacheManager
from src.agents.indexer import IndexerAgent
from src.agents.renderer import Renderer
from src.pipeline.logic_graph import compile_graph
from src.pipeline.state import LogicState
from src.tools.schema_tools import SchemaTools
from src.tools.cache_tools import CacheClient
from src.tools.vector_store import VectorStore
from src.monitoring.health import HealthChecker
from src.middleware.correlation_id import CorrelationIdMiddleware, get_correlation_id
from src.llm_factory import list_available_models, get_default_provider, get_default_model
from src.logger import get_logger
import yaml

logger = get_logger(__name__, concern="app")

# Load environment based on ENVIRONMENT variable
env = os.getenv("ENVIRONMENT", "dev")
load_dotenv(f".env.{env}")


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
_validator: Validator = None
_cache_manager: CacheManager = None
_indexer: IndexerAgent = None
_renderer: Renderer = None
_compiled_graph = None
_graph_available: bool = False
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
    global _variable_tracer, _validator, _cache_manager, _indexer, _renderer
    global _compiled_graph, _health_checker, _settings, _graph_available

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
        from src.parsing.loader import load_all_functions
        _graph_redis = _redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", "6379")),
        )
        for fn_dir in graph_cfg.get("functions_dirs", []):
            result = load_all_functions(
                functions_dir=fn_dir,
                schema=oracle_cfg["schema"],
                redis_client=_graph_redis,
                force_reparse=graph_cfg.get("force_reparse_on_startup", False),
            )
            logger.info(
                f"Graph pipeline: {result['status']} — "
                f"{result['functions_parsed']} parsed, "
                f"{result['functions_skipped']} skipped, "
                f"{result['functions_failed']} failed"
            )
            if result["status"] in ("success", "partial"):
                _graph_available = True
    except Exception as exc:
        logger.warning(f"Graph pipeline failed (non-fatal): {exc}")

    # Auto-index configured modules on startup
    auto_index_modules = embedding_cfg.get("auto_index_modules", [])
    for module_name in auto_index_modules:
        try:
            logger.info(f"Auto-indexing module: {module_name}")
            result = await _indexer.index_module(module_name, force=False)
            logger.info(
                f"Auto-index {module_name}: "
                f"{result.get('indexed', 0)} indexed, "
                f"{result.get('skipped', 0)} skipped, "
                f"{result.get('errors', 0)} errors"
            )
        except Exception as exc:
            logger.warning(f"Auto-indexing failed for {module_name} (non-fatal): {exc}")

    logger.info("RTIE application started successfully")
    yield

    # Shutdown
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

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error(f"Query failed: {exc}\n{tb} | correlation_id={correlation_id}")
        return JSONResponse(
            status_code=500,
            content={
                "error": str(exc),
                "correlation_id": correlation_id,
            },
        )


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
        return await _cache_manager.refresh_schema_snapshot(schema)
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
