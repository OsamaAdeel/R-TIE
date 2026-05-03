"""
LLM Error Sanitization (W42 OpenAI + Claude SDK extension).

Catches upstream OpenAI and Anthropic SDK exceptions raised through
langchain's ChatOpenAI / ChatAnthropic wrappers and translates them into
user-safe DECLINED responses. Mirrors the W21 Oracle sanitization pattern
in src/agents/data_query.py — the raw exception is always logged with
full context, but only sanitized text reaches the user-facing response.

Both providers' exception class names align (AuthenticationError,
RateLimitError, APITimeoutError, etc.), so the user-facing translation
table is shared. The classification ladder walks both provider hierarchies;
the user can't tell which provider failed by reading the message.

Use at a non-streaming call site:

    from src.llm_errors import sanitize_llm_exception, LLMSanitizedError

    try:
        response = await llm.ainvoke(messages)
    except Exception as exc:
        raise sanitize_llm_exception(exc, context="classify_query") from exc

Use at a streaming call site (async generator):

    try:
        async for chunk in llm.astream(messages):
            if chunk.content:
                yield chunk.content
    except Exception as exc:
        raise sanitize_llm_exception(exc, context="stream_chain") from exc

The SSE boundary in src/main.py catches LLMSanitizedError specifically
(before the generic Exception fallback) and emits a structured DECLINED
done event built via build_declined_response().
"""

from __future__ import annotations

from typing import Any, Optional

import anthropic
import openai
from tenacity import RetryError

from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="app")


# ---------------------------------------------------------------------------
# Translation table — exception class name -> user-safe message.
# Keys are bare class names (no module qualifier), so OpenAI and Anthropic
# exceptions with the same name (AuthenticationError, RateLimitError, ...)
# share an entry. Add provider-specific names (LengthFinishReasonError,
# AnthropicError) only when they actually differ.
# ---------------------------------------------------------------------------
_LLM_ERROR_MESSAGES: dict[str, str] = {
    "AuthenticationError": (
        "LLM service authentication failed. Please contact support."
    ),
    "PermissionDeniedError": (
        "LLM service denied the request. Please contact support."
    ),
    "NotFoundError": (
        "LLM model or endpoint was not found. Please contact support."
    ),
    "RateLimitError": (
        "LLM service is currently rate-limited. Try again shortly."
    ),
    "APITimeoutError": (
        "LLM request timed out. Try a simpler query or try again."
    ),
    "APIConnectionError": (
        "Could not reach LLM service. Check network connectivity and try again."
    ),
    "BadRequestError": (
        "LLM request was malformed. This is likely a bug — "
        "please report this query to support."
    ),
    "UnprocessableEntityError": (
        "LLM rejected the request as unprocessable. Try rephrasing the query."
    ),
    "ConflictError": (
        "LLM service reported a conflict. Try again shortly."
    ),
    "InternalServerError": (
        "LLM service returned a server error. Try again shortly."
    ),
    "LengthFinishReasonError": (
        "Response was cut off before completion. The query may be too "
        "complex — try rephrasing or asking about a smaller piece."
    ),
    "ContentFilterFinishReasonError": (
        "Response was blocked by content moderation. Try rephrasing the question."
    ),
    "APIResponseValidationError": (
        "LLM service returned a malformed response. Try again or rephrase the query."
    ),
    "APIStatusError": (
        "LLM service returned an HTTP error. Try again or rephrase the query."
    ),
    "APIError": (
        "LLM service returned an error. Try again or rephrase the query."
    ),
    "OpenAIError": (
        "LLM service returned an error. Try again or rephrase the query."
    ),
    "AnthropicError": (
        "LLM service returned an error. Try again or rephrase the query."
    ),
}

GENERIC_LLM_ERROR_MESSAGE = (
    "LLM service returned an unexpected error. Try again or rephrase the query."
)


# ---------------------------------------------------------------------------
# Exception unwrap (parallel to data_query._unwrap_retry_error).
# ChatOpenAI is configured with max_retries=5 (llm_factory.py:115); langchain's
# internal retries don't currently surface as RetryError, but if a tenacity
# wrapper is ever introduced upstream the unwrap defends against the same
# Future-repr leak that W21 fixed for Oracle.
# ---------------------------------------------------------------------------
def _unwrap_retry_error(exc: BaseException) -> BaseException:
    """Peel tenacity's RetryError so we see the underlying LLM exception."""
    current = exc
    while isinstance(current, RetryError):
        try:
            inner = current.last_attempt.exception()
        except Exception:
            inner = None
        if inner is None or inner is current:
            break
        current = inner
    return current


# ---------------------------------------------------------------------------
# Categorization ladders. Order matters: most specific first.
# LengthFinishReasonError and ContentFilterFinishReasonError are NOT subclasses
# of APIError, so they need their own checks. Within the APIError hierarchy,
# status-specific subclasses must precede APIStatusError, and APIStatusError
# must precede APIError.
#
# OpenAI and Anthropic ship parallel hierarchies: APIError ← APIStatusError ←
# AuthenticationError/RateLimitError/etc. We walk both ladders so a query
# using either provider produces an identical user-facing message for the
# same logical failure (e.g. 401 → "authentication failed" message regardless
# of which SDK raised).
# ---------------------------------------------------------------------------
_OPENAI_EXCEPTION_LADDER: tuple[type[BaseException], ...] = (
    openai.LengthFinishReasonError,
    openai.ContentFilterFinishReasonError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.AuthenticationError,
    openai.PermissionDeniedError,
    openai.NotFoundError,
    openai.ConflictError,
    openai.UnprocessableEntityError,
    openai.RateLimitError,
    openai.BadRequestError,
    openai.InternalServerError,
    openai.APIResponseValidationError,
    openai.APIStatusError,
    openai.APIError,
    openai.OpenAIError,
)

# Mirrors the OpenAI ladder. Anthropic does not ship LengthFinishReasonError
# or ContentFilterFinishReasonError (stop_reason is exposed on the message
# object, not via exceptions), so those entries are absent here.
_ANTHROPIC_EXCEPTION_LADDER: tuple[type[BaseException], ...] = (
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.AuthenticationError,
    anthropic.PermissionDeniedError,
    anthropic.NotFoundError,
    anthropic.ConflictError,
    anthropic.UnprocessableEntityError,
    anthropic.RateLimitError,
    anthropic.BadRequestError,
    anthropic.InternalServerError,
    anthropic.APIResponseValidationError,
    anthropic.APIStatusError,
    anthropic.APIError,
    anthropic.AnthropicError,
)

_LLM_EXCEPTION_LADDERS: tuple[tuple[type[BaseException], ...], ...] = (
    _OPENAI_EXCEPTION_LADDER,
    _ANTHROPIC_EXCEPTION_LADDER,
)


def categorize_llm_exception(exc: BaseException) -> tuple[str, str]:
    """Return ``(category, user_safe_message)`` for an LLM exception.

    The category is the exception class name (used in warnings + logs).
    The user-safe message contains no Python internals or stack info.

    Recognizes both OpenAI and Anthropic SDK exception hierarchies; an
    exception not matching either falls through to ``UnexpectedError``
    with the generic message.

    Callers must still log the raw exception with full traceback at ERROR
    level — this function only produces the user-facing text. Use
    ``sanitize_llm_exception`` to do both in one call.
    """
    inner = _unwrap_retry_error(exc)
    for ladder in _LLM_EXCEPTION_LADDERS:
        for cls in ladder:
            if isinstance(inner, cls):
                category = cls.__name__
                return category, _LLM_ERROR_MESSAGES.get(
                    category, GENERIC_LLM_ERROR_MESSAGE
                )
    return ("UnexpectedError", GENERIC_LLM_ERROR_MESSAGE)


# ---------------------------------------------------------------------------
# DECLINED response builder.
# ---------------------------------------------------------------------------
def build_declined_response(
    category: str,
    user_message: str,
    *,
    correlation_id: Optional[str] = None,
    context: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble a DECLINED-shape dict for an LLM-error path.

    Shape conforms to the existing DECLINED contract used by
    ``build_function_not_found_response`` (orchestrator.py:513), with the
    W42 additions: ``warnings`` (one entry, ``LLM_API_ERROR: ...``) and
    ``category`` (machine-readable exception class name).
    """
    warning = f"LLM_API_ERROR: {category} — {user_message}"
    explanation_md = (
        "### Cannot complete this query\n\n"
        f"{user_message}"
    )
    return {
        "type": "llm_api_error",
        "status": "declined",
        "category": category,
        "validated": False,
        "badge": "DECLINED",
        "confidence": 0.0,
        "source_citations": [],
        "warnings": [warning],
        "user_message": user_message,
        "message": user_message,
        "explanation": {
            "markdown": explanation_md,
            "summary": user_message,
        },
        "correlation_id": correlation_id,
        "context": context,
    }


# ---------------------------------------------------------------------------
# LLMSanitizedError — raised by call sites, caught at SSE boundary.
# ---------------------------------------------------------------------------
class LLMSanitizedError(Exception):
    """Sanitized wrapper around an upstream LLM exception.

    Raised by agent call sites after categorizing + logging the raw
    exception. The SSE boundary in src/main.py catches this specifically
    (before the generic Exception catch) and emits a DECLINED done event.
    """

    def __init__(
        self,
        category: str,
        user_message: str,
        *,
        correlation_id: Optional[str] = None,
        context: Optional[str] = None,
    ):
        super().__init__(user_message)
        self.category = category
        self.user_message = user_message
        self.correlation_id = correlation_id
        self.context = context

    def declined_payload(self) -> dict[str, Any]:
        return build_declined_response(
            self.category,
            self.user_message,
            correlation_id=self.correlation_id,
            context=self.context,
        )


# ---------------------------------------------------------------------------
# sanitize_llm_exception — categorize + log + return ready-to-raise error.
# ---------------------------------------------------------------------------
def sanitize_llm_exception(
    exc: BaseException,
    *,
    context: str,
    correlation_id: Optional[str] = None,
) -> LLMSanitizedError:
    """Categorize + log + return an ``LLMSanitizedError`` ready to raise.

    The raw exception (with traceback) is logged at ERROR level via
    ``logger.exception()`` before the sanitized error is returned.
    Callers add only a small try/except — categorization and logging
    are centralized here.
    """
    if correlation_id is None:
        try:
            correlation_id = get_correlation_id()
        except Exception:
            correlation_id = None
    category, user_message = categorize_llm_exception(exc)
    logger.exception(
        "LLM call failed | context=%s category=%s correlation_id=%s",
        context, category, correlation_id,
    )
    return LLMSanitizedError(
        category,
        user_message,
        correlation_id=correlation_id,
        context=context,
    )
