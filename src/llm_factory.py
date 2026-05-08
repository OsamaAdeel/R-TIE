"""
RTIE LLM Factory.

Provides a factory for creating LLM instances from either OpenAI or
Anthropic (Claude), selected dynamically at runtime. Supports per-request
model switching via the provider and model parameters.

W34c Phase 1: per-call-site static model dispatch via SITE_MODEL_DEFAULTS,
overridable in aggregate by the RTIE_MODEL_OVERRIDES env var (JSON dict).
"""

import json
import os
import ssl
from typing import Optional

import httpx
from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel

from src.logger import get_logger

logger = get_logger(__name__, concern="app")

# Supported providers
PROVIDERS = {"openai", "anthropic"}


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
# Remaining Phase 4 sites (orchestrator.classify_query,
# logic_explainer.*) are intentionally absent — they will be added
# individually in later PRs.
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
    """Get the default LLM provider from environment.

    Returns:
        Provider string: 'openai' or 'anthropic'.
    """
    return os.getenv("DEFAULT_LLM_PROVIDER", "openai").lower()


def get_default_model(provider: str) -> str:
    """Get the default model name for a given provider.

    Args:
        provider: The LLM provider ('openai' or 'anthropic').

    Returns:
        Default model name string.
    """
    if provider == "anthropic":
        return os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
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
