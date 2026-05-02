"""Unit tests for VariableTracer's W45 ungrounded-identifier branch.

Covers:
  - UNGROUNDED_IDENTIFIER_PROMPT template substitution ({IDENTIFIER} +
    {CANDIDATE_LIST} placeholders resolve correctly)
  - UNGROUNDED_NEXT_STEP_TEMPLATE substitution yields the deterministic
    boilerplate next-step line
  - _format_source_excerpt abbreviates long source bodies to ~40 lines
  - stream_ungrounded assembles the expected messages and emits the
    next-step boilerplate after the LLM stream finishes

The LLM is stubbed — these are pure-logic tests that run in milliseconds
and don't hit the OpenAI API.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, List

import pytest

from src.agents.variable_tracer import (
    PARTIAL_SOURCE_FUNCTION_PROMPT,
    PARTIAL_SOURCE_NEXT_STEP_TEMPLATE,
    UNGROUNDED_IDENTIFIER_PROMPT,
    UNGROUNDED_NEXT_STEP_TEMPLATE,
    VariableTracer,
    _format_schema_list,
)


# ---------------------------------------------------------------------
# UNGROUNDED_IDENTIFIER_PROMPT template substitution
# ---------------------------------------------------------------------

def test_prompt_substitutes_identifier_into_title_and_constraints():
    rendered = UNGROUNDED_IDENTIFIER_PROMPT.format(
        IDENTIFIER="CAP973",
        CANDIDATE_LIST="- FN_FOO (similarity score 0.50)",
    )
    # Title and "not found" header
    assert "## CAP973 — Not Found in Indexed Functions" in rendered
    # Every HARD CONSTRAINT keeps the identifier substituted
    assert "DO NOT write a header of the form \"## CAP973 in" in rendered
    assert "Never substitute the asked-about identifier for the" in rendered


def test_prompt_substitutes_candidate_list_block():
    rendered = UNGROUNDED_IDENTIFIER_PROMPT.format(
        IDENTIFIER="CAP973",
        CANDIDATE_LIST=(
            "- TLX_PROV_AMT_FOR_CAP013 (similarity score 0.78)\n"
            "- FN_UPDATE_RATING_CODE (similarity score 0.41)"
        ),
    )
    assert "TLX_PROV_AMT_FOR_CAP013" in rendered
    assert "FN_UPDATE_RATING_CODE" in rendered


def test_next_step_boilerplate_substitutes_identifier_and_schemas():
    text = UNGROUNDED_NEXT_STEP_TEMPLATE.format(
        IDENTIFIER="CAP973",
        DISCOVERED_SCHEMAS="OFSERM, OFSMDM",
    )
    assert "CAP973" in text
    # Schema-agnostic boilerplate (Phase 8): the schema list is filled in
    # at emission time from schema_discovery.discovered_schemas. The
    # response always names the schemas RTIE has actually indexed
    # rather than the historical OFSMDM-only / FCT_STANDARD_ACCT_HEAD
    # phrasing.
    assert "OFSERM, OFSMDM" in text
    assert "manifest gap report" in text
    # The code owns the full section now (heading + body) so there is no
    # chance for the LLM to render whitespace between them.
    assert "### Suggested next step" in text


def test_format_schema_list_is_deterministic_across_input_orders():
    """Sort + comma-join. ['OFSERM','OFSMDM'] and ['OFSMDM','OFSERM']
    must render identically — the user-visible Suggested next step
    cannot depend on Redis SCAN ordering."""
    a = _format_schema_list(["OFSERM", "OFSMDM"])
    b = _format_schema_list(["OFSMDM", "OFSERM"])
    assert a == b == "OFSERM, OFSMDM"


def test_format_schema_list_handles_empty_and_none():
    """Defensive: empty / None still renders without raising."""
    assert _format_schema_list(None) == "(none discovered)"
    assert _format_schema_list([]) == "(none discovered)"
    assert _format_schema_list(["SOLO"]) == "SOLO"


def test_format_schema_list_preserves_distinct_schema_names_sorted():
    """Three+ schemas: alphabetic order, single comma-space separator."""
    out = _format_schema_list(["ZEBRA", "ALPHA", "MIDDLE"])
    assert out == "ALPHA, MIDDLE, ZEBRA"


def test_prompt_template_does_not_emit_next_step_heading():
    # The LLM should STOP after the "Related functions I searched" list.
    # The "### Suggested next step" heading belongs to
    # UNGROUNDED_NEXT_STEP_TEMPLATE and must not appear in the system
    # prompt as a template section to be filled in.
    rendered = UNGROUNDED_IDENTIFIER_PROMPT.format(
        IDENTIFIER="CAP973",
        CANDIDATE_LIST="- X (similarity score 0.50)",
    )
    # The heading may still appear inside the constraint text ("Do not
    # write..."), but not as a literal template scaffold line.
    assert "(Leave this section blank" not in rendered


# ---------------------------------------------------------------------
# _format_source_excerpt
# ---------------------------------------------------------------------

def test_format_source_excerpt_keeps_short_sources_intact():
    tracer = VariableTracer()
    lines = [{"line": i, "text": f"stmt_{i};"} for i in range(1, 6)]
    out = tracer._format_source_excerpt(lines, max_lines=40)
    assert out.startswith("L1: stmt_1;")
    assert out.endswith("L5: stmt_5;")
    assert "omitted" not in out


def test_format_source_excerpt_truncates_long_sources():
    tracer = VariableTracer()
    lines = [{"line": i, "text": f"stmt_{i};"} for i in range(1, 101)]
    out = tracer._format_source_excerpt(lines, max_lines=40)
    # First 40 kept
    assert "L1: stmt_1;" in out
    assert "L40: stmt_40;" in out
    # Remaining skipped with a summary marker
    assert "L41: stmt_41;" not in out
    assert "(60 more lines omitted)" in out


# ---------------------------------------------------------------------
# stream_ungrounded message assembly + next-step emission
# ---------------------------------------------------------------------

class _StubChunk:
    def __init__(self, content: str) -> None:
        self.content = content


class _StubLLM:
    """Minimal stand-in for a LangChain chat model: records messages and
    streams back a fixed body of chunks."""

    def __init__(self, body: str = "BODY") -> None:
        self.body = body
        self.captured_messages: List[Any] = []

    def astream(self, messages: List[Any]):
        self.captured_messages = messages

        async def gen():
            yield _StubChunk(self.body)
        return gen()


def _drain(async_iter) -> List[str]:
    """Collect all chunks from an async iterator synchronously."""
    async def run() -> List[str]:
        out: List[str] = []
        async for chunk in async_iter:
            out.append(chunk)
        return out
    return asyncio.run(run())


def test_stream_ungrounded_builds_prompt_and_appends_next_step(monkeypatch):
    stub = _StubLLM(body="## CAP973 — Not Found in Indexed Functions\n\nbody")
    monkeypatch.setattr(
        "src.agents.variable_tracer.create_llm", lambda **kw: stub
    )

    tracer = VariableTracer()
    candidates: Dict[str, Dict[str, Any]] = {
        "TLX_PROV_AMT_FOR_CAP013": {
            "score": 0.78,
            "description": "Computes provision amount for CAP013",
            "tables_read": "STG_PRODUCT_PROCESSOR",
            "tables_written": "SETUP_BANK_CAPITAL_DTL",
            "source_code": [
                {"line": 1, "text": "CREATE OR REPLACE FUNCTION ..."},
                {"line": 2, "text": "BEGIN"},
            ],
        },
        "FN_UPDATE_RATING_CODE": {
            "score": 0.41,
            "description": "Merges rating codes",
            "source_code": [{"line": 1, "text": "MERGE INTO stg..."}],
        },
    }

    chunks = _drain(tracer.stream_ungrounded(
        identifier="CAP973",
        candidates=candidates,
        raw_query="How is CAP973 calculated?",
        discovered_schemas=["OFSERM", "OFSMDM"],
    ))

    # System message should carry the substituted prompt template.
    system_msg, human_msg = stub.captured_messages
    assert "## CAP973 — Not Found in Indexed Functions" in system_msg.content
    assert "TLX_PROV_AMT_FOR_CAP013 (similarity score 0.78)" in system_msg.content
    assert "FN_UPDATE_RATING_CODE (similarity score 0.41)" in system_msg.content

    # User message should include the raw query, the identifier, and the
    # abbreviated source excerpts for each candidate.
    assert "How is CAP973 calculated?" in human_msg.content
    assert "Unresolved identifier: CAP973" in human_msg.content
    assert "=== FUNCTION: TLX_PROV_AMT_FOR_CAP013" in human_msg.content
    assert "=== FUNCTION: FN_UPDATE_RATING_CODE" in human_msg.content

    # Yielded chunks: LLM body, then the deterministic next-step boilerplate
    # with {IDENTIFIER} and the discovered-schemas list substituted.
    assert chunks[0].startswith("## CAP973")
    assert "CAP973" in chunks[-1]
    # Schema-agnostic next-step boilerplate (Phase 8): the schemas
    # passed in are rendered sorted, comma-separated.
    assert "OFSERM, OFSMDM" in chunks[-1]
    assert "manifest gap report" in chunks[-1]


def test_stream_ungrounded_handles_no_discovered_schemas(monkeypatch):
    """When no graph_redis is available (or discovered_schemas is None),
    the next-step boilerplate must still emit without raising — falling
    back to the '(none discovered)' marker."""
    stub = _StubLLM(body="body")
    monkeypatch.setattr(
        "src.agents.variable_tracer.create_llm", lambda **kw: stub
    )
    tracer = VariableTracer()
    chunks = _drain(tracer.stream_ungrounded(
        identifier="CAP973",
        candidates={},
        raw_query="How is CAP973 calculated?",
        discovered_schemas=None,
    ))
    assert "(none discovered)" in chunks[-1]


# ---------------------------------------------------------------------
# W49 — PARTIAL_SOURCE_FUNCTION_PROMPT template substitution
# ---------------------------------------------------------------------

def test_partial_source_prompt_substitutes_function_name_and_schema():
    rendered = PARTIAL_SOURCE_FUNCTION_PROMPT.format(
        FUNCTION_NAME="ABL_Def_Pension_Fund_Asset_Net_DTL",
        SCHEMA="OFSERM",
        BATCH_NAME="OFSERM_RUN",
        HIERARCHY_PATH="Pension → Asset Net DTL",
        TASK_ORDER="task #7",
        DESCRIPTION="Computes deferred pension asset/liability split",
    )
    # Title and "What I know about it" header must carry the substituted name.
    assert "## ABL_Def_Pension_Fund_Asset_Net_DTL — Source Not Currently Indexed" in rendered
    assert "Schema: OFSERM" in rendered
    assert "Batch: OFSERM_RUN" in rendered
    assert "Process path: Pension → Asset Net DTL" in rendered
    assert "Task position: task #7" in rendered
    assert (
        "Declared description: Computes deferred pension asset/liability split"
        in rendered
    )


def test_partial_source_prompt_renders_not_specified_fallback():
    rendered = PARTIAL_SOURCE_FUNCTION_PROMPT.format(
        FUNCTION_NAME="ABL_Def_Pension_Fund_Asset_Net_DTL",
        SCHEMA="OFSERM",
        BATCH_NAME="Not specified",
        HIERARCHY_PATH="Not specified",
        TASK_ORDER="Not specified",
        DESCRIPTION="Not specified",
    )
    # "Not specified" must reach the rendered template unchanged.
    assert "Batch: Not specified" in rendered
    assert "Declared description: Not specified" in rendered


def test_partial_source_next_step_substitutes_function_name():
    text = PARTIAL_SOURCE_NEXT_STEP_TEMPLATE.format(
        FUNCTION_NAME="ABL_Def_Pension_Fund_Asset_Net_DTL"
    )
    assert "ABL_Def_Pension_Fund_Asset_Net_DTL" in text
    # Phase 8 audit: the boilerplate must point at the file location
    # pattern AND ask the user to file a parser-coverage report. It must
    # NOT reference W35 or any "in-progress multi-schema work" — those
    # framings became stale after Phase 8.
    assert "db/modules" in text
    assert "parser-coverage report" in text
    assert "W35" not in text
    # Heading is owned by the template so the LLM cannot inject whitespace
    # between it and the body.
    assert "### Suggested next step" in text


# ---------------------------------------------------------------------
# W49 — stream_partial_source assembly + next-step emission
# ---------------------------------------------------------------------

def test_stream_partial_source_builds_prompt_and_appends_next_step(monkeypatch):
    stub = _StubLLM(
        body=(
            "## ABL_Def_Pension_Fund_Asset_Net_DTL — Source Not Currently Indexed\n\n"
            "body"
        )
    )
    monkeypatch.setattr(
        "src.agents.variable_tracer.create_llm", lambda **kw: stub
    )
    tracer = VariableTracer()

    chunks = _drain(tracer.stream_partial_source(
        function_name="ABL_Def_Pension_Fund_Asset_Net_DTL",
        schema="OFSERM",
        hierarchy={
            "batch": "OFSERM_RUN",
            "process": "Pension",
            "sub_process": "Asset Net DTL",
            "task_order": 7,
        },
        manifest_description="Computes deferred pension asset/liability split",
    ))

    # System message must carry the substituted prompt template.
    system_msg, human_msg = stub.captured_messages
    assert (
        "## ABL_Def_Pension_Fund_Asset_Net_DTL — Source Not Currently Indexed"
        in system_msg.content
    )
    assert "Schema: OFSERM" in system_msg.content
    assert "Batch: OFSERM_RUN" in system_msg.content
    assert "Process path: Pension → Asset Net DTL" in system_msg.content
    assert "Task position: task #7" in system_msg.content

    # User message must repeat the metadata and tell the model not to speculate.
    assert "ABL_Def_Pension_Fund_Asset_Net_DTL" in human_msg.content
    assert "OFSERM" in human_msg.content
    assert "source body for this function is NOT available" in human_msg.content

    # Yielded chunks: LLM body, then the deterministic next-step boilerplate
    # with {FUNCTION_NAME} substituted. Boilerplate is the LAST chunk.
    assert chunks[0].startswith("## ABL_Def_Pension_Fund_Asset_Net_DTL")
    assert "ABL_Def_Pension_Fund_Asset_Net_DTL" in chunks[-1]
    # Phase 8: boilerplate points at the file location and the
    # parser-coverage feedback channel; W35 wording is gone.
    assert "db/modules" in chunks[-1]
    assert "parser-coverage report" in chunks[-1]
    assert "W35" not in chunks[-1]


# ---------------------------------------------------------------------
# Phase 8 stale-phrase regression — the W45/W49 prompt audit removed
# OFSMDM-only / "multi-schema work in progress" / W35 phase references
# from both prompts and both next-step templates. These regressions
# guard against accidental reintroduction.
# ---------------------------------------------------------------------

_STALE_PHRASES = (
    "W35",
    "multi-schema work",
    "multi-schema fix",
    "multi-schema support",
    "OFSMDM-only",
    "OFSERM is known to be half-open",
    "OFSERM is half-open",
    "currently partial",
    "partially indexed",
    "partial coverage of the",
)


def _assert_no_stale_phrases(text: str, label: str) -> None:
    for phrase in _STALE_PHRASES:
        assert phrase not in text, (
            f"{label} contains stale Phase-8-removed phrase {phrase!r}; "
            f"see test_phase8 prompt audit"
        )


def test_w45_prompt_has_no_stale_phase_8_phrases():
    rendered = UNGROUNDED_IDENTIFIER_PROMPT.format(
        IDENTIFIER="CAP973",
        CANDIDATE_LIST="- FN_FOO (similarity score 0.50)",
    )
    _assert_no_stale_phrases(rendered, "UNGROUNDED_IDENTIFIER_PROMPT")


def test_w45_next_step_has_no_stale_phase_8_phrases():
    rendered = UNGROUNDED_NEXT_STEP_TEMPLATE.format(
        IDENTIFIER="CAP973",
        DISCOVERED_SCHEMAS="OFSERM, OFSMDM",
    )
    _assert_no_stale_phrases(rendered, "UNGROUNDED_NEXT_STEP_TEMPLATE")


def test_w49_prompt_has_no_stale_phase_8_phrases():
    rendered = PARTIAL_SOURCE_FUNCTION_PROMPT.format(
        FUNCTION_NAME="SOME_FN",
        SCHEMA="OFSERM",
        BATCH_NAME="OFSERM_RUN",
        HIERARCHY_PATH="A → B",
        TASK_ORDER="task #1",
        DESCRIPTION="Not specified",
    )
    _assert_no_stale_phrases(rendered, "PARTIAL_SOURCE_FUNCTION_PROMPT")


def test_w49_next_step_has_no_stale_phase_8_phrases():
    rendered = PARTIAL_SOURCE_NEXT_STEP_TEMPLATE.format(
        FUNCTION_NAME="SOME_FN"
    )
    _assert_no_stale_phrases(rendered, "PARTIAL_SOURCE_NEXT_STEP_TEMPLATE")


def test_stream_partial_source_renders_not_specified_for_missing_metadata(monkeypatch):
    """When hierarchy and description are missing, the prompt must still
    render with 'Not specified' fallbacks rather than raw None values."""
    stub = _StubLLM(body="body")
    monkeypatch.setattr(
        "src.agents.variable_tracer.create_llm", lambda **kw: stub
    )
    tracer = VariableTracer()

    _drain(tracer.stream_partial_source(
        function_name="SOME_FN",
        schema="OFSERM",
        hierarchy=None,
        manifest_description=None,
    ))

    system_msg, _ = stub.captured_messages
    assert "Batch: Not specified" in system_msg.content
    assert "Process path: Not specified" in system_msg.content
    assert "Task position: Not specified" in system_msg.content
    assert "Declared description: Not specified" in system_msg.content
