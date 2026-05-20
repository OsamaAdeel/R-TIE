"""W92 — schema-label consistency in multi-schema responses.

Before W92, ``data.schema`` in the SSE done payload (and the
non-streaming /v1/query Renderer output) was always the orchestrator's
primary-anchor schema — a single string. When retrieval pulled
functions from multiple schemas (Phase 3 multi-source), the heading
``schema`` field could disagree with the body content because the body
cited functions from a different schema.

W92 adopts Architecture B: keep ``schema`` as the primary anchor
(backward compatible), and add a new ``cited_schemas`` field carrying
the sorted, distinct list of schemas whose source bodies were actually
fetched into ``multi_source``. Both meta and done events carry
``cited_schemas``; the /v1/query Renderer carries it too. The W91
heading source also switches from ``state["schema"]`` to the
``w70_anchor``-promoted position-0 entry's schema, aligning with
W97's promote-to-front contract so the LLM-rendered heading and the
data payload tell the same story.

These tests are pure-logic and exercise:

* the ``_compute_cited_schemas`` helper in :mod:`src.main`
* the FUNCTION_LOGIC done-payload shape (regression: ``schema`` now
  emitted; new: ``cited_schemas``)
* the Phase 2 done-payload shape (new: ``cited_schemas``)
* the DATA_QUERY done-payload shape (new: ``cited_schemas``)
* the Renderer output shape (new: ``cited_schemas``)
* the W91 heading-source switch (now driven by w70_anchor, falls back
  to ``state["schema"]`` when absent)
* the W91 invariant tests in test_w91_schema_placeholder.py still pass
  (re-exported subset run here for completeness; the standalone file
  remains the source of truth and is exercised separately in CI).

No LLM, Oracle, or Redis dependency.
"""

from __future__ import annotations

import asyncio
from typing import Any, List

import pytest

from src.agents.renderer import Renderer
from src.agents import variable_tracer as vt_module
from src.agents.variable_tracer import VariableTracer
from src.main import _compute_cited_schemas


# ---------------------------------------------------------------------------
# _compute_cited_schemas helper — pure derivation
# ---------------------------------------------------------------------------


def test_compute_cited_schemas_empty_multi_source_returns_empty_list():
    """No multi_source → empty list. Empty dict and None both collapse."""
    assert _compute_cited_schemas({}) == []
    assert _compute_cited_schemas(None) == []


def test_compute_cited_schemas_single_schema_returns_single_element():
    """One function from OFSERM → single-element list."""
    multi_source = {
        "FN_FOO": {"schema": "OFSERM", "source_code": []},
    }
    assert _compute_cited_schemas(multi_source) == ["OFSERM"]


def test_compute_cited_schemas_multi_schema_returns_sorted_distinct():
    """Two functions across OFSERM + OFSMDM → both, sorted alphabetically."""
    multi_source = {
        "FN_A": {"schema": "OFSMDM", "source_code": []},
        "FN_B": {"schema": "OFSERM", "source_code": []},
        "FN_C": {"schema": "OFSERM", "source_code": []},
    }
    assert _compute_cited_schemas(multi_source) == ["OFSERM", "OFSMDM"]


def test_compute_cited_schemas_skips_entries_with_missing_or_blank_schema():
    """Defensive: entries with no schema key, blank schema, or non-dict
    values are silently skipped."""
    multi_source = {
        "FN_A": {"schema": "OFSERM", "source_code": []},
        "FN_B": {"source_code": []},          # missing schema
        "FN_C": {"schema": "", "source_code": []},  # blank schema
        "FN_D": "not_a_dict",                  # non-dict garbage
        "FN_E": None,                          # None entry
    }
    assert _compute_cited_schemas(multi_source) == ["OFSERM"]


def test_compute_cited_schemas_is_deterministic_across_dict_orders():
    """The sort guarantees identical output regardless of insertion order
    — required for snapshot-style canary equality."""
    ms_a = {
        "FN_A": {"schema": "OFSMDM"},
        "FN_B": {"schema": "OFSERM"},
    }
    ms_b = {
        "FN_B": {"schema": "OFSERM"},
        "FN_A": {"schema": "OFSMDM"},
    }
    assert _compute_cited_schemas(ms_a) == _compute_cited_schemas(ms_b)


# ---------------------------------------------------------------------------
# FUNCTION_LOGIC meta + done_payload — assembled inline like the existing
# test_stream_diagnostic_exposure regression test does.
# ---------------------------------------------------------------------------


def _build_function_logic_done_payload(state, *, grounding, final_markdown,
                                       functions_analyzed, schema_scope,
                                       correlation_id):
    """Mirror of the FUNCTION_LOGIC done_payload assembly in src/main.py
    (after W92). Kept inline so the contract is locked in here even if
    the helper around it is refactored."""
    return {
        "confidence": grounding["confidence"],
        "validated": grounding["badge"] == "VERIFIED",
        "badge": grounding["badge"],
        "source_citations": grounding["source_citations"],
        "warnings": grounding["warnings"],
        "functions_analyzed": functions_analyzed,
        "schema_searched": list(state.get("schemas_searched", []) or []),
        "schema": state.get("schema", ""),
        "cited_schemas": state.get("cited_schemas") or [],
        "schema_scope": schema_scope,
        "correlation_id": correlation_id,
        "explanation": {"markdown": final_markdown, "summary": final_markdown[:200]},
    }


def _build_function_logic_meta(state, *, schema_scope, correlation_id,
                               cited_schemas):
    """Mirror of the FUNCTION_LOGIC meta event in src/main.py (after W92).
    The real code computes cited_schemas once and threads it into both
    state and the meta dict; we accept the same precomputed value here."""
    return {
        "schema": state.get("schema", ""),
        "object_name": state.get("object_name", "")[:100],
        "query_type": state.get("query_type", ""),
        "functions_analyzed": list(state.get("multi_source", {}).keys()),
        "schema_searched": list(state.get("schemas_searched", []) or []),
        "cited_schemas": cited_schemas,
        "schema_scope": schema_scope,
        "correlation_id": correlation_id,
        "graph_rerank": state.get("graph_rerank_stats") or {},
    }


def test_function_logic_meta_carries_cited_schemas_sorted_distinct():
    """The meta event publishes cited_schemas derived from multi_source.
    This is the W92 contract that closes the heading-vs-body gap."""
    state = {
        "schema": "OFSMDM",
        "object_name": "FN_FOO",
        "query_type": "FUNCTION_LOGIC",
        "schemas_searched": ["OFSMDM", "OFSERM"],
        "multi_source": {
            "FN_A": {"schema": "OFSERM"},
            "FN_B": {"schema": "OFSMDM"},
        },
    }
    cited = _compute_cited_schemas(state["multi_source"])
    state["cited_schemas"] = cited
    meta = _build_function_logic_meta(
        state, schema_scope="ALL", correlation_id="abc", cited_schemas=cited,
    )
    assert meta["schema"] == "OFSMDM"
    assert meta["cited_schemas"] == ["OFSERM", "OFSMDM"]


def test_function_logic_meta_cited_schemas_empty_when_multi_source_empty():
    """Defensive: a DECLINED-shaped meta still carries the cited_schemas
    key — empty list, never missing — so consumers don't need defensive
    .get() chains."""
    state = {
        "schema": "OFSMDM",
        "object_name": "FN_FOO",
        "query_type": "FUNCTION_LOGIC",
        "schemas_searched": [],
        "multi_source": {},
    }
    cited = _compute_cited_schemas(state["multi_source"])
    meta = _build_function_logic_meta(
        state, schema_scope="ALL", correlation_id="abc", cited_schemas=cited,
    )
    assert meta["cited_schemas"] == []


def test_function_logic_done_payload_now_carries_schema_regression():
    """W92 regression fix: the FUNCTION_LOGIC done payload now emits the
    primary anchor schema alongside the meta event. App.jsx merges meta
    into data after the done payload, so having both halves carry the
    same value makes the merge a no-op for that field — closing the
    observability gap where `data.schema` could disagree with the body."""
    state = {
        "schema": "OFSMDM",
        "cited_schemas": ["OFSERM", "OFSMDM"],
        "schemas_searched": ["OFSMDM", "OFSERM"],
    }
    grounding = {
        "confidence": 0.85,
        "badge": "VERIFIED",
        "source_citations": [],
        "warnings": [],
    }
    done = _build_function_logic_done_payload(
        state,
        grounding=grounding,
        final_markdown="# explanation",
        functions_analyzed=["FN_A", "FN_B"],
        schema_scope="ALL",
        correlation_id="abc",
    )
    assert done["schema"] == "OFSMDM"
    assert done["cited_schemas"] == ["OFSERM", "OFSMDM"]


def test_function_logic_done_payload_cited_schemas_defaults_empty_list():
    """When state has no cited_schemas key (legacy / DECLINED), the done
    payload emits an empty list rather than None."""
    state = {
        "schema": "OFSMDM",
        "schemas_searched": [],
    }
    grounding = {
        "confidence": 0.0,
        "badge": "UNVERIFIED",
        "source_citations": [],
        "warnings": [],
    }
    done = _build_function_logic_done_payload(
        state,
        grounding=grounding,
        final_markdown="",
        functions_analyzed=[],
        schema_scope="ALL",
        correlation_id="abc",
    )
    assert done["cited_schemas"] == []
    assert done["schema"] == "OFSMDM"


# ---------------------------------------------------------------------------
# Phase 2 done_payload — single-element cited_schemas
# ---------------------------------------------------------------------------


def _build_phase2_done_payload(*, query_type, result, schemas_searched,
                               cited_schemas, state, full_markdown,
                               correlation_id):
    """Mirror of the _phase2_stream done_payload in src/main.py (after
    W92). Phase 2 routes to a single schema, so cited_schemas mirrors
    schemas_searched."""
    return {
        "type": query_type.lower(),
        "status": result.get("status"),
        "route": result.get("route"),
        "validated": not result.get("sanity_warnings"),
        "sanity_warnings": result.get("sanity_warnings") or [],
        "used_fallback": bool(result.get("used_fallback")),
        "badge": "VERIFIED" if not result.get("sanity_warnings") else "REVIEW",
        "schema_searched": schemas_searched,
        "cited_schemas": cited_schemas,
        "schema_scope": state.get("schema_scope") or "ALL",
        "correlation_id": correlation_id,
        "explanation": {"markdown": full_markdown},
        "origin": result.get("origin") or {},
        "evidence": result.get("evidence"),
        "verification_sql": result.get("verification_sql"),
    }


def test_phase2_done_payload_carries_single_element_cited_schemas():
    """Phase 2 (VALUE_TRACE / DIFFERENCE_EXPLANATION) routes to one
    schema. cited_schemas is the same single-element list — present for
    shape symmetry with FUNCTION_LOGIC + DATA_QUERY."""
    schemas_searched = ["OFSERM"]
    cited_schemas = ["OFSERM"]
    done = _build_phase2_done_payload(
        query_type="VALUE_TRACE",
        result={"status": "answered", "route": "row_first", "sanity_warnings": []},
        schemas_searched=schemas_searched,
        cited_schemas=cited_schemas,
        state={"schema_scope": "OFSERM"},
        full_markdown="row found",
        correlation_id="abc",
    )
    assert done["schema_searched"] == ["OFSERM"]
    assert done["cited_schemas"] == ["OFSERM"]


def test_phase2_done_payload_cited_schemas_empty_when_schema_unresolved():
    """Defensive: when schema could not be resolved, both lists are
    empty — the contract is "always present, possibly empty"."""
    done = _build_phase2_done_payload(
        query_type="VALUE_TRACE",
        result={"status": "no_row", "route": "row_first", "sanity_warnings": []},
        schemas_searched=[],
        cited_schemas=[],
        state={"schema_scope": "ALL"},
        full_markdown="",
        correlation_id="abc",
    )
    assert done["cited_schemas"] == []


# ---------------------------------------------------------------------------
# DATA_QUERY done_payload — single-element cited_schemas reflecting pivot
# ---------------------------------------------------------------------------


def _build_data_query_done_payload(*, routed_schema, status, suspicious,
                                   result, state, explanation, correlation_id):
    """Mirror of the _data_query_stream done_payload in src/main.py
    (after W92). routed_schema is the post-pivot schema; cited_schemas
    is its single-element list."""
    schemas_searched = [routed_schema] if routed_schema else []
    cited_schemas = [routed_schema] if routed_schema else []
    validated = status == "answered" and not suspicious
    if suspicious:
        badge = "UNVERIFIED"
    elif status == "answered":
        badge = "VERIFIED"
    elif status == "confirmation_required":
        badge = "REVIEW"
    else:
        badge = "REJECTED"
    return {
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
        "cited_schemas": cited_schemas,
        "schema_scope": state.get("schema_scope") or "ALL",
        "correlation_id": correlation_id,
        "explanation": {"markdown": explanation},
    }


def test_data_query_done_payload_carries_routed_schema_in_cited_schemas():
    """When DataQueryAgent pivots from OFSMDM to OFSERM (Phase 4 named-
    table pivot), cited_schemas reflects the post-pivot schema, not the
    orchestrator default."""
    done = _build_data_query_done_payload(
        routed_schema="OFSERM",
        status="answered",
        suspicious=False,
        result={"query_kind": "select", "sanity_warnings": []},
        state={"schema_scope": "ALL"},
        explanation="row found",
        correlation_id="abc",
    )
    assert done["schema_searched"] == ["OFSERM"]
    assert done["cited_schemas"] == ["OFSERM"]


def test_data_query_done_payload_cited_schemas_empty_when_unrouted():
    """REJECTED responses without a routed_schema produce empty
    cited_schemas — the contract holds."""
    done = _build_data_query_done_payload(
        routed_schema="",
        status="rejected",
        suspicious=False,
        result={"sanity_warnings": ["no SQL"]},
        state={"schema_scope": "ALL"},
        explanation="",
        correlation_id="abc",
    )
    assert done["cited_schemas"] == []


# ---------------------------------------------------------------------------
# Renderer (/v1/query non-streaming path)
# ---------------------------------------------------------------------------


def test_renderer_output_carries_cited_schemas_from_state():
    """The /v1/query Renderer publishes cited_schemas symmetric with
    /v1/stream so consumers (canaries / dashboards) can read one field
    across endpoints."""
    state = {
        "object_name": "FN_FOO",
        "object_type": "FUNCTION",
        "schema": "OFSMDM",
        "cited_schemas": ["OFSERM", "OFSMDM"],
        "explanation": {"markdown": "x"},
        "confidence": 0.8,
        "validated": True,
        "warnings": [],
        "session_id": "s1",
        "correlation_id": "c1",
        "search_results": [],
        "multi_source": {},
    }
    renderer = Renderer()
    result = asyncio.run(renderer.render_response(state))
    output = result["output"]
    assert output["schema"] == "OFSMDM"
    assert output["cited_schemas"] == ["OFSERM", "OFSMDM"]


def test_renderer_output_cited_schemas_defaults_empty_list():
    """Legacy callers / DECLINED states without cited_schemas in state
    still get the key in output — value is an empty list."""
    state = {
        "object_name": "FN_FOO",
        "object_type": "FUNCTION",
        "schema": "OFSMDM",
        "explanation": {"markdown": "x"},
        "confidence": 0.0,
        "validated": False,
        "warnings": [],
        "session_id": "s1",
        "correlation_id": "c1",
        "search_results": [],
        "multi_source": {},
    }
    renderer = Renderer()
    result = asyncio.run(renderer.render_response(state))
    output = result["output"]
    assert "cited_schemas" in output
    assert output["cited_schemas"] == []


# ---------------------------------------------------------------------------
# W91 heading source — switch from state["schema"] to w70_anchor-promoted
# entry's schema
# ---------------------------------------------------------------------------


def _w92_select_heading_schema(state: dict) -> str:
    """Replica of the W92 selection logic in src/main.py:
    use w70_anchor's function to find its multi_source entry's schema,
    fall back to state["schema"] when absent / unmatched.

    Kept here as a small inline copy so the test is decoupled from the
    surrounding `stream_chain` call and runs without an LLM."""
    anchor = state.get("w70_anchor") or None
    anchor_fn = (anchor or {}).get("function") or ""
    ms = state.get("multi_source") or {}
    heading_schema = ""
    if anchor_fn:
        anchor_fn_upper = anchor_fn.upper()
        for ms_fn, ms_entry in ms.items():
            if ms_fn.upper() == anchor_fn_upper:
                heading_schema = (ms_entry or {}).get("schema") or ""
                break
    if not heading_schema:
        heading_schema = state.get("schema", "")
    return heading_schema


def test_w92_heading_uses_w70_anchor_promoted_entry_schema_when_present():
    """When state has a w70_anchor whose function lives in multi_source,
    the heading uses that entry's schema — not state["schema"]. This is
    the W92 fix: the heading and body cite the same schema."""
    state = {
        "schema": "OFSMDM",                          # orchestrator default
        "w70_anchor": {"function": "FN_ERM_FOO", "source": "object_name"},
        "multi_source": {
            "FN_ERM_FOO": {"schema": "OFSERM", "source_code": []},
            "FN_MDM_BAR": {"schema": "OFSMDM", "source_code": []},
        },
    }
    assert _w92_select_heading_schema(state) == "OFSERM"


def test_w92_heading_falls_back_to_state_schema_when_no_w70_anchor():
    """Variable-trace-only paths often bypass apply_w70_anchor —
    fall back to state["schema"] when w70_anchor is missing."""
    state = {
        "schema": "OFSMDM",
        "w70_anchor": None,
        "multi_source": {
            "FN_FOO": {"schema": "OFSERM", "source_code": []},
        },
    }
    assert _w92_select_heading_schema(state) == "OFSMDM"


def test_w92_heading_falls_back_when_anchor_fn_not_in_multi_source():
    """Defensive: if the anchor function name doesn't match any
    multi_source entry (shouldn't happen post-W97 promote-to-front,
    but harden against state drift), fall back to state["schema"]."""
    state = {
        "schema": "OFSMDM",
        "w70_anchor": {"function": "FN_PHANTOM"},
        "multi_source": {
            "FN_FOO": {"schema": "OFSERM", "source_code": []},
        },
    }
    assert _w92_select_heading_schema(state) == "OFSMDM"


def test_w92_heading_matches_case_insensitively():
    """Function names in multi_source are typically uppercase, but the
    anchor function may have mixed case. The match must be case-
    insensitive so a Pascal-case anchor still resolves."""
    state = {
        "schema": "OFSMDM",
        "w70_anchor": {"function": "fn_erm_foo"},
        "multi_source": {
            "FN_ERM_FOO": {"schema": "OFSERM"},
        },
    }
    assert _w92_select_heading_schema(state) == "OFSERM"


def test_w92_heading_falls_back_when_multi_source_entry_has_no_schema():
    """Defensive: a malformed multi_source entry (missing schema) falls
    through to state["schema"] rather than emitting an empty string."""
    state = {
        "schema": "OFSMDM",
        "w70_anchor": {"function": "FN_FOO"},
        "multi_source": {
            "FN_FOO": {"source_code": []},  # no schema key
        },
    }
    assert _w92_select_heading_schema(state) == "OFSMDM"


# ---------------------------------------------------------------------------
# W91 regression — the substitution still works after the call-site change
#
# The W91 unit tests in test_w91_schema_placeholder.py pass `schema=...`
# directly to stream_chain / explain_chain (not through the W92 call site
# wrapper), so they are unaffected by W92. We re-exercise the substitution
# here against the new W92-selected value to confirm the end-to-end path
# (state → _w92_select_heading_schema → stream_chain → SystemMessage) still
# produces a substituted prompt with no `(SCHEMA)` literal.
# ---------------------------------------------------------------------------


class _FakeChunk:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeStreamingLLM:
    def __init__(self) -> None:
        self.captured_messages: List[Any] | None = None

    async def astream(self, messages):
        self.captured_messages = messages
        yield _FakeChunk("ok")


@pytest.mark.asyncio
async def test_w91_substitution_still_works_via_w92_selected_schema(monkeypatch):
    """End-to-end: a state with a multi-schema multi_source and a
    w70_anchor pointing at the OFSERM function feeds an "OFSERM" schema
    label into stream_chain. The rendered SystemMessage carries
    "(OFSERM)" and has no residual "(SCHEMA)" / "{SCHEMA}" tokens.

    This is the W92 + W91 happy path: the heading the LLM is told to
    produce names the same schema as the body content."""
    fake_llm = _FakeStreamingLLM()

    def fake_create_llm(**kwargs):
        return fake_llm

    monkeypatch.setattr(vt_module, "create_llm", fake_create_llm)

    state = {
        "schema": "OFSMDM",  # orchestrator default — would be the bug
        "w70_anchor": {"function": "FN_ERM_FOO"},
        "multi_source": {
            "FN_ERM_FOO": {"schema": "OFSERM"},
            "FN_MDM_BAR": {"schema": "OFSMDM"},
        },
    }
    heading_schema = _w92_select_heading_schema(state)
    assert heading_schema == "OFSERM"  # the W92 fix

    tracer = VariableTracer()
    chunks: List[str] = []
    async for token in tracer.stream_chain(
        target_variable="EAD_AMOUNT",
        chain_text="(fake compact chain)",
        user_query="Trace EAD_AMOUNT through FN_ERM_FOO.",
        provider="openai",
        model="gpt-4o-mini",
        schema=heading_schema,
    ):
        chunks.append(token)

    assert chunks == ["ok"]
    assert fake_llm.captured_messages is not None
    system_content = fake_llm.captured_messages[0].content
    assert "OFSERM" in system_content
    assert "(OFSERM)" in system_content
    assert "(SCHEMA)" not in system_content
    assert "{SCHEMA}" not in system_content
    # Critically, the body's schema (OFSERM) must dominate the heading,
    # not the orchestrator default (OFSMDM).
    assert "(OFSMDM)" not in system_content
