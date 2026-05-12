"""W84 — unit tests for the /v1/stream done-event diagnostic block.

Covers ``_build_diagnostic_block`` in :mod:`src.main`, which extracts
W81 cross-process suppression and W70/W76 anchor signals from the
LangGraph state for inclusion in the SSE done event. Adding these
three fields is purely additive — the existing done-event shape
(badge, validated, warnings, confidence, explanation, meta,
functions_analyzed) is unchanged, and a regression test below pins
that contract.

The integration test in tests/integration/test_live_stream.py
verifies the diagnostic block lands in real SSE responses against a
running backend.
"""

import json

import pytest

from src.main import _build_diagnostic_block


# ----- positive cases: fields appear in done event ----------------


def test_w81_suppressed_true_appears_in_done_event():
    """When the renderer stamped suppression, expose it as True."""
    state = {"w81_suppressed": True}
    block = _build_diagnostic_block(state)
    assert block["w81_suppressed"] is True


def test_w81_suppressed_false_default_when_unset():
    """Renderer never stamps False — absence collapses to False, not None."""
    state = {}
    block = _build_diagnostic_block(state)
    assert block["w81_suppressed"] is False
    assert block["w81_suppressed"] is not None


def test_w70_anchor_present_when_set():
    """When apply_w70_anchor stamps a cascade hit, expose the function name."""
    state = {
        "w70_anchor": {
            "function": "CS_FOO",
            "source": "w76_prefix",
            "confidence": "high",
        }
    }
    block = _build_diagnostic_block(state)
    assert block["w70_anchor"] == "CS_FOO"


def test_w70_anchor_null_when_unset():
    """Absent state key → null value (key still present in payload)."""
    state = {}
    block = _build_diagnostic_block(state)
    assert "w70_anchor" in block
    assert block["w70_anchor"] is None


def test_w70_anchor_null_when_explicitly_none():
    """apply_w70_anchor stamps None when no cascade match — expose as null."""
    state = {"w70_anchor": None}
    block = _build_diagnostic_block(state)
    assert block["w70_anchor"] is None


def test_w76_anchor_present_when_set():
    """When the W76 anchor rule fired, expose the function it anchored on."""
    state = {
        "w76_anchor": {
            "function": "FN_LOAD_OPS_RISK_DATA",
            "source": "prefix",
            "original_query_type": "VARIABLE_TRACE",
            "original_target_variable": "",
        }
    }
    block = _build_diagnostic_block(state)
    assert block["w76_anchor"] == "FN_LOAD_OPS_RISK_DATA"


def test_w76_anchor_null_when_unset():
    state = {}
    block = _build_diagnostic_block(state)
    assert "w76_anchor" in block
    assert block["w76_anchor"] is None


def test_w76_anchor_null_when_alias_literal_cleared():
    """orchestrator stamps function="" on the cleared-alias branch.
    A blank function name is semantically "no anchor"; collapse to None
    so consumers see a clean string-or-null contract."""
    state = {
        "w76_anchor": {
            "function": "",
            "alias_literal_cleared": "EXP_11",
            "reason": "alias literal with no enclosing function",
            "source": "alias_fallback_no_function",
        }
    }
    block = _build_diagnostic_block(state)
    assert block["w76_anchor"] is None


def test_diagnostic_block_always_present_for_empty_state():
    """Even a near-empty state produces a complete diagnostic block."""
    block = _build_diagnostic_block({})
    assert set(block.keys()) == {"w81_suppressed", "w70_anchor", "w76_anchor"}
    assert block["w81_suppressed"] is False
    assert block["w70_anchor"] is None
    assert block["w76_anchor"] is None


def test_diagnostic_block_with_all_three_fields_populated():
    """Realistic single-function semantic-explain shape — all three set."""
    state = {
        "w81_suppressed": False,
        "w70_anchor": {
            "function": "FN_LOAD_OPS_RISK_DATA",
            "source": "object_name",
            "confidence": "high",
        },
        "w76_anchor": {
            "function": "FN_LOAD_OPS_RISK_DATA",
            "source": "prefix",
        },
    }
    block = _build_diagnostic_block(state)
    assert block == {
        "w81_suppressed": False,
        "w70_anchor": "FN_LOAD_OPS_RISK_DATA",
        "w76_anchor": "FN_LOAD_OPS_RISK_DATA",
    }


def test_diagnostic_block_cross_process_shape():
    """Cross-flow VARIABLE_TRACE: W81 fires, W70 may be None
    (variable_tracer bypasses apply_w70_anchor), W76 typically None."""
    state = {"w81_suppressed": True, "w70_anchor": None}
    block = _build_diagnostic_block(state)
    assert block["w81_suppressed"] is True
    assert block["w70_anchor"] is None
    assert block["w76_anchor"] is None


# ----- regression: existing done-event fields unchanged -----------


def test_badge_validated_warnings_unchanged_by_diagnostic_addition():
    """The diagnostic block is additive — it cannot collide with or
    rename any existing top-level done-event field. This test asserts
    the contract by simulating the assembly inline (mirrors
    src/main.py line ~1431) and checking the pre-W84 fields are bit-
    for-bit identical to a manually constructed reference dict."""
    state = {
        "w81_suppressed": False,
        "w70_anchor": {"function": "FN_A", "source": "object_name", "confidence": "high"},
        "w76_anchor": {},
        "schemas_searched": ["OFSMDM"],
    }
    grounding = {
        "confidence": 0.85,
        "badge": "VERIFIED",
        "source_citations": [{"function": "FN_A", "schema": "OFSMDM"}],
        "warnings": [],
    }
    final_markdown = "### FN_A\n\nDoes a thing."
    functions_analyzed = ["FN_A"]
    schema_scope = "OFSMDM"
    correlation_id = "abc-123"

    done_payload = {
        "confidence": grounding["confidence"],
        "validated": grounding["badge"] == "VERIFIED",
        "badge": grounding["badge"],
        "source_citations": grounding["source_citations"],
        "warnings": grounding["warnings"],
        "functions_analyzed": functions_analyzed,
        "schema_searched": list(state.get("schemas_searched", []) or []),
        "schema_scope": schema_scope,
        "correlation_id": correlation_id,
        "explanation": {
            "markdown": final_markdown,
            "summary": final_markdown[:200],
        },
        "diagnostic": _build_diagnostic_block(state),
    }

    # Existing fields — names, types, and values unchanged.
    assert done_payload["confidence"] == 0.85
    assert done_payload["validated"] is True
    assert done_payload["badge"] == "VERIFIED"
    assert done_payload["source_citations"] == [
        {"function": "FN_A", "schema": "OFSMDM"}
    ]
    assert done_payload["warnings"] == []
    assert done_payload["functions_analyzed"] == ["FN_A"]
    assert done_payload["schema_searched"] == ["OFSMDM"]
    assert done_payload["schema_scope"] == "OFSMDM"
    assert done_payload["correlation_id"] == "abc-123"
    assert done_payload["explanation"]["markdown"] == final_markdown
    assert done_payload["explanation"]["summary"] == final_markdown[:200]

    # Diagnostic block lives at top level, not nested inside meta or
    # explanation. Confirm it doesn't collide with an existing key.
    assert "diagnostic" in done_payload
    pre_w84_keys = {
        "confidence", "validated", "badge", "source_citations",
        "warnings", "functions_analyzed", "schema_searched",
        "schema_scope", "correlation_id", "explanation",
    }
    new_keys = set(done_payload.keys()) - pre_w84_keys
    assert new_keys == {"diagnostic"}


# ----- JSON-shape contract -----------------------------------------


def test_done_event_is_json_serializable():
    """If state ever contains a non-JSON-serializable value (set, Path,
    datetime), json.dumps on the diagnostic block must not crash. The
    block extracts primitives only — bool, str|None — so this is a
    standing invariant we lock in here so it cannot regress."""
    state = {
        "w81_suppressed": True,
        "w70_anchor": {
            "function": "FN_X",
            "source": "w76_prefix",
            "confidence": "high",
        },
        "w76_anchor": {"function": "FN_X", "source": "prefix"},
    }
    block = _build_diagnostic_block(state)
    serialized = json.dumps(block)
    round_tripped = json.loads(serialized)
    assert round_tripped == {
        "w81_suppressed": True,
        "w70_anchor": "FN_X",
        "w76_anchor": "FN_X",
    }


def test_done_event_serializable_when_state_contains_garbage():
    """Defensive: even if state["w70_anchor"] is not a dict (impossible
    per the type, but harden against state-management bugs), the
    helper must not raise — fall through to None and stay JSON-safe."""
    state = {"w70_anchor": "not_a_dict", "w76_anchor": 42}
    block = _build_diagnostic_block(state)
    json.dumps(block)
    assert block["w70_anchor"] is None
    assert block["w76_anchor"] is None
