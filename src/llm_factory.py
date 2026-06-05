"""
RTIE LLM Factory.

Provides a factory for creating LLM instances from either OpenAI or
Anthropic (Claude), selected dynamically at runtime. Supports per-request
model switching via the provider and model parameters.

W34c Phase 1: per-call-site static model dispatch via SITE_MODEL_DEFAULTS,
overridable in aggregate by the RTIE_MODEL_OVERRIDES env var (JSON dict).
"""

import asyncio
import concurrent.futures
import json
import os
import ssl
from typing import Any, List, Optional

import httpx
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)

from src.logger import get_logger

logger = get_logger(__name__, concern="app")

# Supported providers:
#   * openai     — langchain_openai.ChatOpenAI (OpenAI API or OpenAI-compat
#                  endpoints via OPENAI_BASE_URL: OpenRouter, Groq, Ollama).
#   * anthropic  — langchain_anthropic.ChatAnthropic (Anthropic API key).
#   * claude_cli — ClaudeCLIChatModel below, wrapping claude-agent-sdk to
#                  route through the local `claude` CLI's OAuth (Pro/Max
#                  subscription, no API key).
PROVIDERS = {"openai", "anthropic", "claude_cli"}


# ──────────────────────────────────────────────────────────────────────
# Claude Agent SDK adapter.
#
# Wraps `claude_agent_sdk.query()` as a LangChain BaseChatModel so the
# existing call sites (`llm.ainvoke(messages)`, `llm.astream(...)`)
# work unchanged.
#
# Windows event-loop isolation:
#   run.py forces WindowsSelectorEventLoopPolicy on startup (required
#   by psycopg async). But on Windows, SelectorEventLoop raises
#   NotImplementedError when subprocess transport is requested — and
#   the SDK spawns the `claude` CLI via subprocess.
#
#   _aquery_text below dispatches each SDK call to a worker thread
#   that owns its own ProactorEventLoop. asyncio.set_event_loop is
#   thread-local, so the main thread keeps SelectorEventLoop intact
#   for psycopg. The two loops coexist in one process.
# ──────────────────────────────────────────────────────────────────────
class ClaudeCLIChatModel(BaseChatModel):
    """LangChain ChatModel that routes to Claude via the local CLI."""

    model_name: str = "claude-haiku-4-5"
    temperature: float = 0.0
    max_tokens: int = 4000

    @property
    def _llm_type(self) -> str:
        return "claude_cli"

    @staticmethod
    def _split_messages(
        messages: List[BaseMessage],
    ) -> tuple[str, Optional[str]]:
        system_parts: List[str] = []
        prompt_parts: List[str] = []
        for msg in messages:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if isinstance(msg, SystemMessage):
                system_parts.append(content)
            elif isinstance(msg, HumanMessage):
                prompt_parts.append(content)
            elif isinstance(msg, AIMessage):
                prompt_parts.append(f"Assistant: {content}\n\nHuman:")
            else:
                prompt_parts.append(content)
        return (
            "\n\n".join(prompt_parts),
            "\n\n".join(system_parts) if system_parts else None,
        )

    async def _aquery_text(
        self, prompt: str, system: Optional[str]
    ) -> str:
        # Windows event-loop isolation — see class docstring.
        import sys

        async def _do_sdk_call() -> str:
            from claude_agent_sdk import (
                query,
                ClaudeAgentOptions,
                AssistantMessage,
                TextBlock,
            )
            options_kwargs: dict[str, Any] = {
                "model": self.model_name,
                "allowed_tools": [],
                "max_turns": 1,
            }
            if system:
                options_kwargs["system_prompt"] = system
            chunks: List[str] = []
            async for message in query(
                prompt=prompt,
                options=ClaudeAgentOptions(**options_kwargs),
            ):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            chunks.append(block.text)
            return "".join(chunks)

        def _worker_thread() -> str:
            if sys.platform == "win32":
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_do_sdk_call())
            finally:
                try:
                    loop.close()
                finally:
                    asyncio.set_event_loop(None)

        main_loop = asyncio.get_running_loop()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return await main_loop.run_in_executor(pool, _worker_thread)

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        prompt, system = self._split_messages(messages)
        text = await self._aquery_text(prompt, system)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=text))]
        )

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._agenerate(messages, stop, None, **kwargs))
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                asyncio.run,
                self._agenerate(messages, stop, None, **kwargs),
            ).result()


# Known embedding-model dimensions. resolve_embedding_config() auto-
# derives RTIE_EMBEDDING_DIM from this when not explicitly set, so
# users don't have to remember that nomic-embed-text is 768.
EMBEDDING_DIM_DEFAULTS: dict[str, int] = {
    # OpenAI
    "text-embedding-3-small":  1536,
    "text-embedding-3-large":  3072,
    "text-embedding-ada-002":  1536,
    # Ollama (local)
    "nomic-embed-text":        768,
    "mxbai-embed-large":       1024,
    "all-minilm":              384,
    # BGE family
    "bge-base-en-v1.5":        768,
    "bge-large-en-v1.5":       1024,
    "bge-small-en-v1.5":       384,
    # Cohere
    "embed-english-v3.0":      1024,
    "embed-multilingual-v3.0": 1024,
    # Mistral
    "mistral-embed":           1024,
    # Voyage
    "voyage-3":                1024,
    "voyage-3-large":          1024,
    "voyage-code-3":           1024,
    # Google
    "text-embedding-004":      768,
}


def resolve_embedding_config() -> dict:
    """Resolve embedding-backend configuration dynamically from env.

    Returns dict with {base_url, api_key, model, dim}. See .env.dev for
    the resolution order. When all env vars are unset, defaults match
    upstream OpenAI text-embedding-3-small @ 1536.
    """
    model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    base_url = os.getenv("EMBEDDING_BASE_URL") or None
    api_key = (
        os.getenv("EMBEDDING_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or "unused"
    )

    explicit_dim = os.getenv("RTIE_EMBEDDING_DIM")
    if explicit_dim:
        dim = int(explicit_dim)
    elif model in EMBEDDING_DIM_DEFAULTS:
        dim = EMBEDDING_DIM_DEFAULTS[model]
    else:
        dim = 1536
        for prefix, d in EMBEDDING_DIM_DEFAULTS.items():
            if model.startswith(prefix):
                dim = d
                break

    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "dim": dim,
    }


# Heuristic placeholder detection — catches .env.example templates like
# "your_anthropic_key_here" / "sk-your-openai-key" so an unfilled config
# isn't auto-selected as the active provider.
_PROVIDER_PLACEHOLDER_PREFIXES = ("your_", "sk-your")


def _is_placeholder_key(value: Optional[str]) -> bool:
    if not value:
        return True
    v = value.strip().lower()
    return any(v.startswith(p) for p in _PROVIDER_PLACEHOLDER_PREFIXES)


# ──────────────────────────────────────────────────────────────────────
# W34c Phase 1: per-call-site model defaults.
#
# Phase 1 formalizes what's already shipped — sites #6-#10 were
# already running gpt-4o-mini; site #5 (variable_tracer.resolve_variables)
# is normalized here from gpt-4o → gpt-4o-mini.
#
# W34c Phase 2: promote phase2.explainer.invoke from gpt-5-mini →
# gpt-4o-mini. (orchestrator.classify_query was originally also part of
# Phase 2 but reverted after canary C14 caught a deterministic
# ClassificationResult ValidationError — gpt-4o-mini elides required
# fields on reconciliation-style UNSUPPORTED queries. Will return as a
# separate PR once the classifier schema is hardened.)
#
# W34c Phase 3: promote data_query._generate_sql to gpt-4o-mini. Wired
# the call site to pass site="data_query._generate_sql" at the same
# time, since the dispatch was not previously the active path for that
# call. Tier 2 canary (5/5 PASS, hand-verified row counts and
# aggregates) is the load-bearing gate.
#
# W77 / W34c Phase 4: promote logic_explainer.{stream,explain}_semantic
# to gpt-4o-mini. Diagnosed in docs/w77_diagnostic.md as the cause of
# v2 benchmark Run 7 truncation — gpt-5-mini reasoning-token
# consumption against a 4096 max_tokens cap. gpt-4o-mini is not a
# reasoning model, so the full max_tokens budget is available for
# visible output. Wired both call sites to pass site= at the same time
# (same shape as Phase 3); max_tokens=4096 retained so the new model
# is measured at the same budget that previously truncated gpt-5-mini.
#
# Remaining sites (orchestrator.classify_query) are intentionally
# absent — see W55. classify_query promotion is gated on structured-
# output schema work for ClassificationResult on UNSUPPORTED routes.
# ──────────────────────────────────────────────────────────────────────
SITE_MODEL_DEFAULTS: dict[str, str] = {
    "variable_tracer.resolve_variables":  "gpt-4o-mini",
    "variable_tracer.explain_chain":      "gpt-4o-mini",
    "variable_tracer.stream_chain":       "gpt-4o-mini",
    "variable_tracer.stream_ungrounded":  "gpt-4o-mini",
    "variable_tracer.stream_partial":     "gpt-4o-mini",
    "indexer.generate_description":       "gpt-4o-mini",
    # W34c Phase 2 addition
    "phase2.explainer.invoke":            "gpt-4o-mini",
    # W34c Phase 3 addition
    "data_query._generate_sql":           "gpt-4o-mini",
    # W77 / W34c Phase 4 additions
    "logic_explainer.stream_semantic":    "gpt-4o-mini",
    "logic_explainer.explain_semantic":   "gpt-4o-mini",
}


def _load_site_model_overrides() -> dict[str, str]:
    """Parse RTIE_MODEL_OVERRIDES env var, layer on top of SITE_MODEL_DEFAULTS.

    The env var is a JSON object mapping site keys → model names. Unknown
    site keys are kept (logged at WARNING) so future-Phase rollouts can
    use the same mechanism without code change. Malformed JSON is
    ignored with a warning; the function never raises.
    """
    raw = os.getenv("RTIE_MODEL_OVERRIDES")
    if not raw:
        return dict(SITE_MODEL_DEFAULTS)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning(
            f"RTIE_MODEL_OVERRIDES is not valid JSON, ignoring: {exc}. "
            f"Falling back to SITE_MODEL_DEFAULTS."
        )
        return dict(SITE_MODEL_DEFAULTS)

    if not isinstance(parsed, dict):
        logger.warning(
            f"RTIE_MODEL_OVERRIDES must be a JSON object, got "
            f"{type(parsed).__name__}. Ignoring."
        )
        return dict(SITE_MODEL_DEFAULTS)

    resolved = dict(SITE_MODEL_DEFAULTS)
    for key, value in parsed.items():
        if not isinstance(value, str):
            logger.warning(
                f"RTIE_MODEL_OVERRIDES['{key}'] must be a string, "
                f"got {type(value).__name__}. Skipping."
            )
            continue
        if key not in SITE_MODEL_DEFAULTS:
            logger.warning(
                f"RTIE_MODEL_OVERRIDES key '{key}' is not a known site "
                f"(known: {sorted(SITE_MODEL_DEFAULTS.keys())}). "
                f"Applying anyway."
            )
        resolved[key] = value
    return resolved


# Resolved at import time so the mapping is stable for the process
# lifetime and grep-able in startup logs.
_RESOLVED_SITE_MODELS: dict[str, str] = _load_site_model_overrides()
logger.info(
    f"W34c Phase 1: resolved per-site model mapping = "
    f"{json.dumps(_RESOLVED_SITE_MODELS, sort_keys=True)}"
)


def get_default_provider() -> str:
    """Resolve LLM provider dynamically from environment.

    Priority order:
      1. DEFAULT_LLM_PROVIDER env var if explicitly set — wins absolutely.
      2. OPENAI_API_KEY non-placeholder → "openai".
      3. ANTHROPIC_API_KEY non-placeholder → "anthropic".
      4. Fallback → "claude_cli" (local `claude` CLI's OAuth — no key
         needed; raises clear error at use time if CLI is missing).

    Returns:
        One of: "openai", "anthropic", "claude_cli".
    """
    explicit = os.getenv("DEFAULT_LLM_PROVIDER")
    if explicit:
        return explicit.strip().lower()
    if not _is_placeholder_key(os.getenv("OPENAI_API_KEY")):
        return "openai"
    if not _is_placeholder_key(os.getenv("ANTHROPIC_API_KEY")):
        return "anthropic"
    return "claude_cli"


def get_default_model(provider: str) -> str:
    """Get the default model name for a given provider.

    Args:
        provider: The LLM provider ('openai' or 'anthropic').

    Returns:
        Default model name string.
    """
    if provider == "anthropic":
        return os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    if provider == "claude_cli":
        # Shares ANTHROPIC_MODEL so the same env var works for both
        # direct-API Anthropic and CLI-wrapped Anthropic.
        return os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    return os.getenv("OPENAI_MODEL", "gpt-5-mini")


def get_site_model(site: str) -> Optional[str]:
    """Look up the resolved model for a call-site key.

    Returns None if the site is not registered in SITE_MODEL_DEFAULTS
    (and not added via RTIE_MODEL_OVERRIDES). Callers should fall back
    to the global default in that case.
    """
    return _RESOLVED_SITE_MODELS.get(site)


def create_llm(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0,
    max_tokens: int = 2000,
    json_mode: bool = False,
    site: Optional[str] = None,
) -> BaseChatModel:
    """Create an LLM instance for the specified provider and model.

    Args:
        provider: 'openai' or 'anthropic'. Defaults to DEFAULT_LLM_PROVIDER env var.
        model: Model name (e.g. 'gpt-4o', 'claude-sonnet-4-20250514'). Explicit
            value always wins over `site=`. Defaults to provider-specific env var
            when neither model nor site is supplied.
        temperature: Sampling temperature. Defaults to 0.
        max_tokens: Maximum output tokens. Defaults to 2000.
        json_mode: Whether to force JSON output. Defaults to False.
        site: W34c dotted call-site key (e.g. "variable_tracer.stream_chain").
            When supplied and `model` is not, the model is resolved from the
            site → model mapping (SITE_MODEL_DEFAULTS layered with
            RTIE_MODEL_OVERRIDES). Unknown sites fall back to the global
            default with a warning.

    Returns:
        A LangChain chat model instance.

    Raises:
        ValueError: If the provider is not supported or API key is missing.
    """
    provider = (provider or get_default_provider()).lower()

    if model is None and site is not None:
        site_model = get_site_model(site)
        if site_model is None:
            logger.warning(
                f"create_llm: site='{site}' is not in SITE_MODEL_DEFAULTS; "
                f"falling back to global default model."
            )
        else:
            model = site_model

    model = model or get_default_model(provider)

    if provider not in PROVIDERS:
        raise ValueError(
            f"Unsupported LLM provider: '{provider}'. "
            f"Supported: {', '.join(sorted(PROVIDERS))}"
        )

    logger.info(
        f"Creating LLM: provider={provider}, model={model}"
        + (f", site={site}" if site else "")
    )

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set in environment")

        kwargs = {}
        if json_mode:
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}

        # GPT-5 and the o-series reasoning models only accept temperature=1;
        # any other value raises "Unsupported value: 'temperature'".
        # Must set explicitly — langchain's ChatOpenAI default is 0.7.
        model_lower = (model or "").lower()
        if model_lower.startswith("gpt-5") or model_lower.startswith("o1") or model_lower.startswith("o3"):
            kwargs["temperature"] = 1
        else:
            kwargs["temperature"] = temperature

        # Force TLS 1.2 — TLS 1.3 on Python 3.14 + corporate networks
        # causes SSL: SSLV3_ALERT_BAD_RECORD_MAC on large payloads
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ssl_ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ssl_ctx.load_default_certs()

        return ChatOpenAI(
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            http_client=httpx.Client(verify=ssl_ctx, timeout=120),
            http_async_client=httpx.AsyncClient(verify=ssl_ctx, timeout=120),
            # Retry transient TLS/proxy failures (SSLV3_ALERT_BAD_RECORD_MAC,
            # connection reset) before surfacing the error to the user.
            max_retries=5,
            **kwargs,
        )

    if provider == "anthropic":
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in environment")

        return ChatAnthropic(
            api_key=api_key,
            model=model,
            temperature=temperature,
            max_tokens_to_sample=max_tokens,
        )

    if provider == "claude_cli":
        # No API key — SDK uses local `claude` CLI's OAuth state
        # (Pro/Max subscription). json_mode is silently ignored; callers
        # use prompt-level JSON instructions + json.loads().
        return ClaudeCLIChatModel(
            model_name=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


def list_available_models() -> dict:
    """List available providers and their configured models.

    Returns:
        Dict with provider names as keys and model info as values.
    """
    models = {}

    if os.getenv("OPENAI_API_KEY"):
        models["openai"] = {
            "available": True,
            "default_model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            "models": [
                "gpt-5.4-mini",
                "gpt-5.4",
                "gpt-5.2",
                "gpt-5-mini",
                "gpt-5-nano",
                "gpt-4o",
                "gpt-4o-mini",
                "gpt-4-turbo",
                "o1",
                "o3-mini",
            ],
        }
    else:
        models["openai"] = {"available": False}

    if os.getenv("ANTHROPIC_API_KEY"):
        models["anthropic"] = {
            "available": True,
            "default_model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
            "models": [
                "claude-sonnet-4-20250514",
                "claude-opus-4-20250514",
                "claude-haiku-4-20250514",
            ],
        }
    else:
        models["anthropic"] = {"available": False}

    return models
