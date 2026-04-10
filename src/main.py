"""
RTIE FastAPI Application.

Provides the HTTP API layer for the Regulatory Trace & Intelligence Engine.
Endpoints include POST /v1/query for logic explanation, GET /health for
dependency status checks, and GET /v1/models for listing available LLM
providers. All requests receive a correlation ID for end-to-end tracing,
and LangSmith tracing is enabled on every query.
"""

import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from src.agents.orchestrator import Orchestrator
from src.agents.metadata_interpreter import MetadataInterpreter
from src.agents.logic_explainer import LogicExplainer
from src.agents.validator import Validator
from src.agents.cache_manager import CacheManager
from src.agents.renderer import Renderer
from src.graph.logic_graph import compile_graph
from src.graph.state import LogicState
from src.tools.schema_tools import SchemaTools
from src.tools.cache_tools import CacheClient
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

    Loads the base settings.yaml and overlays the environment-specific
    settings file (e.g. settings.dev.yaml).

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
_orchestrator: Orchestrator = None
_metadata_interpreter: MetadataInterpreter = None
_logic_explainer: LogicExplainer = None
_validator: Validator = None
_cache_manager: CacheManager = None
_renderer: Renderer = None
_compiled_graph = None
_health_checker: HealthChecker = None
_settings: Dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler for startup and shutdown.

    Initializes all agents, connection pools, and the LangGraph pipeline
    on startup. Cleans up connections on shutdown.

    Args:
        app: The FastAPI application instance.
    """
    global _schema_tools, _cache_client, _orchestrator, _metadata_interpreter
    global _logic_explainer, _validator, _cache_manager, _renderer
    global _compiled_graph, _health_checker, _settings

    _settings = _load_settings()
    oracle_cfg = _settings["oracle"]
    redis_cfg = _settings["redis"]
    llm_cfg = _settings["llm"]

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

    # Initialize agents (no longer need Azure-specific credentials)
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

    _validator = Validator(
        schema_tools=_schema_tools,
        cache_client=_cache_client,
    )

    _cache_manager = CacheManager(
        schema_tools=_schema_tools,
        cache_client=_cache_client,
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
        validator=_validator,
        renderer=_renderer,
        postgres_dsn=postgres_dsn,
    )

    # Initialize health checker
    _health_checker = HealthChecker(
        schema_tools=_schema_tools,
        cache_client=_cache_client,
        postgres_dsn=postgres_dsn,
    )

    logger.info("RTIE application started successfully")
    yield

    # Shutdown
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

# Add CORS middleware for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add correlation ID middleware
app.add_middleware(CorrelationIdMiddleware)


class QueryRequest(BaseModel):
    """Request body for the /v1/query endpoint.

    Attributes:
        query: The user's natural language query or slash command.
        session_id: Unique session identifier for conversation continuity.
        engineer_id: Identifier for the requesting engineer.
        provider: LLM provider to use ('openai' or 'anthropic'). Optional.
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
    """Process a logic explanation query or slash command.

    For slash commands, routes directly to the CacheManager.
    For logic queries, runs the full LangGraph pipeline with the
    selected LLM provider and model.

    Args:
        request: The query request body.
        req: The raw Starlette request for correlation ID.

    Returns:
        Full output dict from the pipeline state, or command result.
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

    # Check for slash commands
    cmd = _orchestrator.check_command(request.query)
    if cmd.is_command:
        result = await _handle_command(
            cmd.command, cmd.args, request.session_id
        )
        return {"type": "command", "result": result, "correlation_id": correlation_id}

    # Run the LangGraph pipeline
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
        f"Query completed: object={final_state.get('object_name', 'N/A')} "
        f"confidence={final_state.get('confidence', 0)} | "
        f"correlation_id={correlation_id}"
    )

    return final_state.get("output", {})


async def _handle_command(
    command: str, args: list, session_id: str
) -> Dict[str, Any]:
    """Route a slash command to the appropriate CacheManager method.

    Args:
        command: The command name (e.g. 'refresh-cache').
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
