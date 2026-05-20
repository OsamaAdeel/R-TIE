"""W91 — `(SCHEMA)` placeholder substitution in VARIABLE_TRACE_PROMPT.

Before W91 the FORMAT block of ``VARIABLE_TRACE_PROMPT`` instructed the
LLM to start its output with::

    ## {VARIABLE_NAME} in `FUNCTION_NAME` (SCHEMA)

The LLM correctly substituted ``{VARIABLE_NAME}`` and ``FUNCTION_NAME``
from the user prompt, but ``(SCHEMA)`` was a literal token never bound
to anything, so it leaked verbatim into the response heading.

W91 changes the token to ``{SCHEMA}`` and threads ``state["schema"]``
through ``explain_chain`` / ``stream_chain`` so the substitution happens
before the prompt ever reaches the LLM.

These tests are pure-logic: the LLM factory is monkeypatched with a
fake that captures the messages it would have sent, so the assertions
run against the rendered ``SystemMessage`` content without any network
or Oracle / Redis dependency.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from src.agents import variable_tracer as vt_module
from src.agents.variable_tracer import (
    UNGROUNDED_IDENTIFIER_PROMPT,
    VARIABLE_TRACE_PROMPT,
    VariableTracer,
)


# ---------------------------------------------------------------------------
# Template text invariants (no LLM needed)
# ---------------------------------------------------------------------------


def test_variable_trace_prompt_has_one_schema_token_no_literal():
    """Guard against half-reverts: if anyone changes ``{SCHEMA}`` back to
    ``(SCHEMA)`` in the template, the bug returns."""
    assert VARIABLE_TRACE_PROMPT.count("{SCHEMA}") == 1
    assert "(SCHEMA)" not in VARIABLE_TRACE_PROMPT


def test_ungrounded_prompt_example_block_untouched():
    """The intentional ``(OFSMDM)`` literal in UNGROUNDED_IDENTIFIER_PROMPT's
    'WRONG OUTPUT' example block must remain — it's the LLM-facing
    demonstration of a forbidden format and is unrelated to W91."""
    assert (
        "## {IDENTIFIER} in `TLX_PROV_AMT_FOR_CAP013` (OFSMDM)"
        in UNGROUNDED_IDENTIFIER_PROMPT
    )


# ---------------------------------------------------------------------------
# stream_chain — system-prompt substitution
# ---------------------------------------------------------------------------


class _FakeChunk:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeStreamingLLM:
    """Captures the messages that would be sent to an LLM and yields a
    single token so the async generator completes."""

    def __init__(self) -> None:
        self.captured_messages: List[Any] | None = None

    async def astream(self, messages):
        self.captured_messages = messages
        yield _FakeChunk("ok")


async def _run_stream_chain_and_capture(
    monkeypatch, schema_arg: str | None
) -> str:
    """Invoke stream_chain with the given schema, drive the async
    generator to completion, and return the rendered SystemMessage
    content the fake LLM saw."""
    fake_llm = _FakeStreamingLLM()

    def fake_create_llm(**kwargs):
        return fake_llm

    monkeypatch.setattr(vt_module, "create_llm", fake_create_llm)

    tracer = VariableTracer()
    chunks: List[str] = []
    async for token in tracer.stream_chain(
        target_variable="EAD_AMOUNT",
        chain_text="(fake compact chain)",
        user_query="Trace EAD_AMOUNT through FN_FOO.",
        provider="openai",
        model="gpt-4o-mini",
        schema=schema_arg,
    ):
        chunks.append(token)

    assert chunks == ["ok"], "fake LLM should have yielded exactly one token"
    assert fake_llm.captured_messages is not None
    system_msg = fake_llm.captured_messages[0]
    return system_msg.content


@pytest.mark.asyncio
async def test_stream_chain_substitutes_concrete_schema(monkeypatch):
    """Happy path: when state carries schema='OFSERM', the rendered system
    prompt names that schema and contains no leftover ``(SCHEMA)`` or
    ``{SCHEMA}`` tokens."""
    rendered = await _run_stream_chain_and_capture(monkeypatch, "OFSERM")

    assert "OFSERM" in rendered
    assert "(SCHEMA)" not in rendered
    assert "{SCHEMA}" not in rendered
    # The substituted token should appear in its rendered FORMAT context —
    # i.e. inside the heading template parentheses, not as bare prose.
    assert "(OFSERM)" in rendered


@pytest.mark.asyncio
async def test_stream_chain_defensive_fallback_when_schema_empty(monkeypatch):
    """Defensive fallback: when schema is the empty string (or None), the
    template still has neither ``(SCHEMA)`` nor ``{SCHEMA}`` left, and the
    fallback literal 'the schema' takes its place."""
    rendered = await _run_stream_chain_and_capture(monkeypatch, "")

    assert "(SCHEMA)" not in rendered
    assert "{SCHEMA}" not in rendered
    assert "the schema" in rendered


# ---------------------------------------------------------------------------
# explain_chain — same substitution path on the non-streaming branch
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """Captures the messages an ``ainvoke`` call would receive and returns
    a canned response."""

    def __init__(self) -> None:
        self.captured_messages: List[Any] | None = None

    async def ainvoke(self, messages):
        self.captured_messages = messages
        return _FakeResponse("## EAD_AMOUNT in `FN_FOO` (OFSERM)\n\n…")


@pytest.mark.asyncio
async def test_explain_chain_substitutes_concrete_schema(monkeypatch):
    """The non-streaming branch (``trace_variable`` → ``explain_chain``)
    must use the same substitution so /v1/query stays consistent with
    /v1/stream — even though only /v1/stream invokes grounding overlays."""
    fake_llm = _FakeLLM()

    def fake_create_llm(**kwargs):
        return fake_llm

    monkeypatch.setattr(vt_module, "create_llm", fake_create_llm)

    tracer = VariableTracer()
    result = await tracer.explain_chain(
        target_variable="EAD_AMOUNT",
        chain_text="(fake compact chain)",
        user_query="Trace EAD_AMOUNT through FN_FOO.",
        provider="openai",
        model="gpt-4o-mini",
        schema="OFSMDM",
    )

    assert isinstance(result, dict)
    assert fake_llm.captured_messages is not None
    system_content = fake_llm.captured_messages[0].content
    assert "OFSMDM" in system_content
    assert "(SCHEMA)" not in system_content
    assert "{SCHEMA}" not in system_content
