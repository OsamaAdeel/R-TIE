"""
RTIE LLM Factory.

Provides a factory for creating LLM instances from either OpenAI or
Anthropic (Claude), selected dynamically at runtime. Supports per-request
model switching via the provider and model parameters.
"""

import os
from typing import Optional

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_core.language_models.chat_models import BaseChatModel

from src.logger import get_logger

logger = get_logger(__name__, concern="app")

# Supported providers
PROVIDERS = {"openai", "anthropic"}


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
    return os.getenv("OPENAI_MODEL", "gpt-4o")


def create_llm(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0,
    max_tokens: int = 2000,
    json_mode: bool = False,
) -> BaseChatModel:
    """Create an LLM instance for the specified provider and model.

    Args:
        provider: 'openai' or 'anthropic'. Defaults to DEFAULT_LLM_PROVIDER env var.
        model: Model name (e.g. 'gpt-4o', 'claude-sonnet-4-20250514'). Defaults to
            provider-specific env var.
        temperature: Sampling temperature. Defaults to 0.
        max_tokens: Maximum output tokens. Defaults to 2000.
        json_mode: Whether to force JSON output. Defaults to False.

    Returns:
        A LangChain chat model instance.

    Raises:
        ValueError: If the provider is not supported or API key is missing.
    """
    provider = (provider or get_default_provider()).lower()
    model = model or get_default_model(provider)

    if provider not in PROVIDERS:
        raise ValueError(
            f"Unsupported LLM provider: '{provider}'. "
            f"Supported: {', '.join(sorted(PROVIDERS))}"
        )

    logger.info(f"Creating LLM: provider={provider}, model={model}")

    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY not set in environment")

        kwargs = {}
        if json_mode:
            kwargs["model_kwargs"] = {"response_format": {"type": "json_object"}}

        return ChatOpenAI(
            api_key=api_key,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
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
            "default_model": os.getenv("OPENAI_MODEL", "gpt-4o"),
            "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "o1", "o3-mini"],
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
