"""Unit tests for src/llm_errors.py — W42 LLM exception sanitization.

Covers:
  - categorize_llm_exception() correctly maps each openai exception type to
    its category and a user-safe message
  - build_declined_response() produces the W42 DECLINED shape
  - LLMSanitizedError carries the right fields
  - sanitize_llm_exception() logs + categorizes correctly
  - No Python internal reprs (CompletionUsage, class-with-parens, dotted
    qualnames) leak into user-facing fields under any path
  - Selected call sites (orchestrator.classify_query, logic_explainer
    .stream_semantic) raise LLMSanitizedError when their LLM raises and
    do not leak the raw exception repr
"""

from __future__ import annotations

import asyncio
import re
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import openai
import pytest

from src.llm_errors import (
    GENERIC_LLM_ERROR_MESSAGE,
    LLMSanitizedError,
    build_declined_response,
    categorize_llm_exception,
    sanitize_llm_exception,
)


# ---------------------------------------------------------------------
# Fixtures — synthetic openai exceptions need a real httpx Request/Response
# ---------------------------------------------------------------------

def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _response(status: int = 500) -> httpx.Response:
    return httpx.Response(status, request=_request())


# ---------------------------------------------------------------------
# categorize_llm_exception — per-type mapping
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "exc_factory,expected_category,expected_phrase",
    [
        (
            lambda: openai.RateLimitError(message="x", response=_response(429), body={}),
            "RateLimitError",
            "rate-limited",
        ),
        (
            lambda: openai.AuthenticationError(message="x", response=_response(401), body={}),
            "AuthenticationError",
            "authentication failed",
        ),
        (
            lambda: openai.PermissionDeniedError(message="x", response=_response(403), body={}),
            "PermissionDeniedError",
            "denied",
        ),
        (
            lambda: openai.NotFoundError(message="x", response=_response(404), body={}),
            "NotFoundError",
            "not found",
        ),
        (
            lambda: openai.BadRequestError(message="x", response=_response(400), body={}),
            "BadRequestError",
            "malformed",
        ),
        (
            lambda: openai.UnprocessableEntityError(message="x", response=_response(422), body={}),
            "UnprocessableEntityError",
            "unprocessable",
        ),
        (
            lambda: openai.ConflictError(message="x", response=_response(409), body={}),
            "ConflictError",
            "conflict",
        ),
        (
            lambda: openai.InternalServerError(message="x", response=_response(500), body={}),
            "InternalServerError",
            "server error",
        ),
        (
            lambda: openai.APIConnectionError(request=_request()),
            "APIConnectionError",
            "Could not reach LLM service",
        ),
        (
            lambda: openai.APITimeoutError(request=_request()),
            "APITimeoutError",
            "timed out",
        ),
    ],
)
def test_categorize_known_openai_exceptions(exc_factory, expected_category, expected_phrase):
    exc = exc_factory()
    category, message = categorize_llm_exception(exc)
    assert category == expected_category
    assert expected_phrase.lower() in message.lower()


def _make_length_finish_reason_error() -> openai.LengthFinishReasonError:
    """Construct a LengthFinishReasonError without a real ChatCompletion.

    The SDK constructor reads ``completion.usage`` so passing ``None``
    raises AttributeError. We bypass __init__ to get a properly-typed
    instance — categorize_llm_exception only does isinstance checks.
    """
    exc = openai.LengthFinishReasonError.__new__(openai.LengthFinishReasonError)
    Exception.__init__(exc, "length")
    return exc


def test_categorize_length_finish_reason_error():
    exc = _make_length_finish_reason_error()
    category, message = categorize_llm_exception(exc)
    assert category == "LengthFinishReasonError"
    assert "cut off" in message.lower()


def test_categorize_unknown_exception_falls_through_to_generic():
    category, message = categorize_llm_exception(ValueError("boom"))
    assert category == "UnexpectedError"
    assert message == GENERIC_LLM_ERROR_MESSAGE


def test_categorize_user_message_never_contains_python_repr():
    # The W42 leak symptom: a Python repr (CompletionUsage(...), a class
    # name followed by parens-args) bleeding into user-facing text.
    # No matter what exception we throw at the categorizer, the returned
    # user message must be free of those patterns.
    cases = [
        openai.RateLimitError(message="<inner: CompletionUsage(prompt_tokens=10)>",
                              response=_response(429), body={}),
        openai.BadRequestError(message="invalid <Foo(arg=1)>",
                               response=_response(400), body={}),
        ValueError("CompletionUsage(completion_tokens=0, prompt_tokens=400)"),
    ]
    for exc in cases:
        _category, message = categorize_llm_exception(exc)
        # No "ClassName(..." parens-args pattern in the user-facing text.
        assert not re.search(r"[A-Z]\w+\([^)]*=", message), (
            f"User-facing message leaked a Python repr: {message!r}"
        )


# ---------------------------------------------------------------------
# build_declined_response — shape
# ---------------------------------------------------------------------

def test_declined_response_has_all_w42_required_fields():
    d = build_declined_response(
        "RateLimitError",
        "rate limited message",
        correlation_id="corr-1",
        context="unit_test",
    )
    # W42 spec — explicit required fields
    assert d["badge"] == "DECLINED"
    assert d["validated"] is False
    assert d["confidence"] == 0.0
    assert d["status"] == "declined"
    assert d["type"] == "llm_api_error"
    # Warnings array, single sanitized entry
    assert isinstance(d["warnings"], list)
    assert len(d["warnings"]) == 1
    assert d["warnings"][0].startswith("LLM_API_ERROR: RateLimitError")
    assert "rate limited message" in d["warnings"][0]
    # Bookkeeping
    assert d["correlation_id"] == "corr-1"
    assert d["context"] == "unit_test"
    # Renderable explanation block (so frontend ValidationHeader has content)
    assert d["explanation"]["markdown"]
    assert d["explanation"]["summary"]
    # No source citations under a declined-LLM path
    assert d["source_citations"] == []


def test_declined_response_payload_has_no_python_repr():
    # Even pathological inputs containing parens/braces shouldn't produce
    # parenthesized class-repr patterns in the rendered payload.
    d = build_declined_response("X", "user-safe text", correlation_id="c", context="x")
    rendered = str(d)
    # The pattern we're guarding against: ClassName(field=value, ...)
    assert not re.search(r"\b[A-Z]\w+\([a-zA-Z_]+=", rendered), (
        f"Payload contains a class-repr-like pattern: {rendered}"
    )


# ---------------------------------------------------------------------
# LLMSanitizedError
# ---------------------------------------------------------------------

def test_llm_sanitized_error_carries_fields():
    err = LLMSanitizedError(
        "RateLimitError", "rate limited", correlation_id="c-1", context="ctx"
    )
    assert err.category == "RateLimitError"
    assert err.user_message == "rate limited"
    assert err.correlation_id == "c-1"
    assert err.context == "ctx"
    # Exception's str() should be the user_message — never the class repr.
    assert str(err) == "rate limited"


def test_llm_sanitized_error_declined_payload_round_trip():
    err = LLMSanitizedError(
        "BadRequestError", "bad req", correlation_id="c-2", context="probe"
    )
    payload = err.declined_payload()
    assert payload["category"] == "BadRequestError"
    assert payload["correlation_id"] == "c-2"
    assert payload["context"] == "probe"
    assert payload["badge"] == "DECLINED"


# ---------------------------------------------------------------------
# sanitize_llm_exception — composes categorize + log + return
# ---------------------------------------------------------------------

def test_sanitize_returns_llm_sanitized_error_with_correct_fields():
    exc = openai.RateLimitError(message="x", response=_response(429), body={})
    sanitized = sanitize_llm_exception(exc, context="unit", correlation_id="abc")
    assert isinstance(sanitized, LLMSanitizedError)
    assert sanitized.category == "RateLimitError"
    assert sanitized.context == "unit"
    assert sanitized.correlation_id == "abc"


def test_sanitize_uses_get_correlation_id_when_not_provided():
    exc = openai.RateLimitError(message="x", response=_response(429), body={})
    # Don't pass correlation_id; helper should fall back to context var.
    sanitized = sanitize_llm_exception(exc, context="unit")
    # Default value of the context var is "N/A" outside a request.
    assert sanitized.correlation_id == "N/A"


def test_sanitize_logs_exception_at_error_level():
    """The src.logger.get_logger() factory disables propagation, so caplog
    can't see records via the root logger — instead we mock logger.exception
    directly and verify it was called once with the expected context."""
    exc = openai.RateLimitError(message="x", response=_response(429), body={})
    with patch("src.llm_errors.logger") as mock_logger:
        sanitize_llm_exception(exc, context="logged")
    assert mock_logger.exception.call_count == 1
    msg, *_ = mock_logger.exception.call_args.args
    assert "LLM call failed" in msg


# ---------------------------------------------------------------------
# Call-site integration: orchestrator.classify_query
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_classify_query_raises_sanitized_on_rate_limit():
    """If the LLM raises RateLimitError, classify_query must raise
    LLMSanitizedError carrying the sanitized message. The original repr
    must not appear in the raised exception's user_message."""
    from src.agents.orchestrator import Orchestrator
    from src.pipeline.state import LogicState

    orch = Orchestrator(temperature=0, max_tokens=100)
    state: LogicState = {
        "raw_query": "test", "object_name": "", "object_type": "",
        "schema": "", "query_type": "", "target_variable": "",
        "warnings": [], "partial_flag": False,
    }  # type: ignore[typeddict-item]

    fake_llm = MagicMock()
    fake_llm.ainvoke = AsyncMock(
        side_effect=openai.RateLimitError(
            message="rate", response=_response(429), body={}
        )
    )

    with patch.object(orch, "_get_llm", return_value=fake_llm):
        with pytest.raises(LLMSanitizedError) as exc_info:
            await orch.classify_query("anything", state)

    err = exc_info.value
    assert err.category == "RateLimitError"
    assert err.context == "classify_query"
    # No Python class-repr leak.
    assert "RateLimitError(" not in err.user_message


# ---------------------------------------------------------------------
# Call-site integration: logic_explainer.stream_semantic (streaming)
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_logic_explainer_stream_semantic_raises_sanitized_on_failure():
    """If astream raises mid-stream, the generator should propagate
    LLMSanitizedError — NOT the raw OpenAI exception. Partial tokens
    yielded before the failure are fine; the SSE boundary builds the
    final DECLINED done event from the LLMSanitizedError."""
    from src.agents.logic_explainer import LogicExplainer
    from src.pipeline.state import LogicState

    explainer = LogicExplainer(temperature=0, max_tokens=100)
    state: LogicState = {
        "raw_query": "test", "object_name": "FOO", "object_type": "",
        "schema": "S", "query_type": "GENERAL", "target_variable": "",
        "warnings": [], "partial_flag": False,
        "multi_source": {}, "llm_payload": "", "graph_available": False,
    }  # type: ignore[typeddict-item]

    fake_llm = MagicMock()

    async def failing_astream(messages):
        # Yield one chunk so we exercise the partial-stream case, then
        # raise a length-finish-reason error like the W41 trigger.
        chunk = MagicMock()
        chunk.content = "first token "
        yield chunk
        raise _make_length_finish_reason_error()

    fake_llm.astream = failing_astream

    with patch("src.agents.logic_explainer.create_llm", return_value=fake_llm):
        gen = explainer.stream_semantic(state)
        tokens: list[str] = []
        with pytest.raises(LLMSanitizedError) as exc_info:
            async for token in gen:
                tokens.append(token)

    # Partial output preserved before the failure
    assert tokens == ["first token "]
    err = exc_info.value
    assert err.category == "LengthFinishReasonError"
    assert err.context == "stream_semantic"
    # Sanitized text — no Python class repr leak
    assert "LengthFinishReasonError(" not in err.user_message
    assert "completion=" not in err.user_message
    assert "CompletionUsage" not in err.user_message
