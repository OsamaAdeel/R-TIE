"""W85 — anchor-vs-asked-function mismatch detector.

Run 9 surfaced D1 (Trace N_NET_INTEREST_INCOME ...) where the W70
cascade anchored on an unrelated function and the response carried
fabricated SQL pointing at it. W83B's Canary A reproduced a cleaner
sibling case: ``How does CS_Goodwill_Calculation work?`` with
w70_anchor landing on ``CS_GOODWILL_NET_OF_DTL_CALCULATION``.

W85 catches the routing-correctness violation that's structurally
distinct from content-grounding (W83a/W83B) and shape-grounding
(W82): the user named function X, the explainer anchored on Y. Even
if Y's description is accurate, that's a trust violation.

Fires independently of every other W57 sub-check by design. The
signal is state-derived (w70_anchor / w76_anchor vs raw_query
extraction); no body inspection is involved.

`function_exists_in_graph` is monkey-patched per-test to avoid
requiring a live Redis. The actual production lookup is in
:func:`src.agents.orchestrator.function_exists_in_graph` — the W85
check imports it locally inside its body so monkeypatching the
orchestrator attribute redirects the call site cleanly.
"""

import pytest

from src.agents import orchestrator as _orch_mod
from src.agents.logic_explainer import (
    _w57_check_anchor_vs_asked_mismatch,
    w57_enforce_grounding,
)


@pytest.fixture
def fake_graph(monkeypatch):
    """Patch function_exists_in_graph to consult an in-memory set.

    Tests append/replace via ``fake_graph["names"]`` (case-insensitive
    storage; the check itself uppercases on its side).
    """
    state = {"names": set()}

    def fake_exists(function_name, redis_client, schemas=None):
        return function_name.upper() in {n.upper() for n in state["names"]}

    monkeypatch.setattr(_orch_mod, "function_exists_in_graph", fake_exists)
    return state


# ===========================================================================
# POSITIVE — W85 should fire
# ===========================================================================

def test_fires_on_sibling_function_mismatch(fake_graph):
    """W83B Canary A verbatim: user asked CS_Goodwill_Calculation,
    cascade landed on CS_GOODWILL_NET_OF_DTL_CALCULATION sibling."""
    fake_graph["names"] = {
        "CS_GOODWILL_CALCULATION",
        "CS_GOODWILL_NET_OF_DTL_CALCULATION",
    }
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does CS_Goodwill_Calculation work?",
        redis_client=object(),  # any non-None — the fake ignores it
        w70_anchor={
            "function": "CS_GOODWILL_NET_OF_DTL_CALCULATION",
            "source": "object_name",
            "confidence": "high",
        },
    )
    assert len(warnings) == 1
    assert warnings[0].startswith("GROUNDING-ANCHOR-MISMATCH-HIGH")
    assert "CS_GOODWILL_NET_OF_DTL_CALCULATION" in warnings[0]
    assert "CS_Goodwill_Calculation" in warnings[0]


def test_fires_on_completely_unrelated_function(fake_graph):
    fake_graph["names"] = {
        "FN_LOAD_OPS_RISK_DATA",
        "ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM",
    }
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FN_LOAD_OPS_RISK_DATA work?",
        redis_client=object(),
        w70_anchor={"function": "ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM"},
    )
    assert len(warnings) == 1
    assert "ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM" in warnings[0]


def test_fires_when_w70_unset_but_w76_set(fake_graph):
    """W76 fallback path: w70_anchor None, w76_anchor present."""
    fake_graph["names"] = {"FOO_FN", "BAR_FN"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FOO_FN work?",
        redis_client=object(),
        w70_anchor=None,
        w76_anchor={"function": "BAR_FN", "source": "prefix"},
    )
    assert len(warnings) == 1
    assert "BAR_FN" in warnings[0]
    assert "FOO_FN" in warnings[0]


def test_fires_with_case_insensitive_normalization(fake_graph):
    """User's lowercase query, uppercase anchor — still a mismatch
    (different function names, not a casing artifact)."""
    fake_graph["names"] = {
        "CS_GOODWILL_CALCULATION",
        "CS_GOODWILL_NET_OF_DTL_CALCULATION",
    }
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="how does cs_goodwill_calculation work",
        redis_client=object(),
        w70_anchor={"function": "CS_GOODWILL_NET_OF_DTL_CALCULATION"},
    )
    assert len(warnings) == 1


def test_fires_w70_takes_precedence_over_w76(fake_graph):
    """When both anchors are set and disagree with each other, W85
    reports the W70 cascade result (the one actually used by the
    explainer prompt). Confirms the preference order."""
    fake_graph["names"] = {"FOO_FN", "BAR_FN", "BAZ_FN"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FOO_FN work?",
        redis_client=object(),
        w70_anchor={"function": "BAZ_FN"},
        w76_anchor={"function": "BAR_FN"},  # ignored
    )
    assert len(warnings) == 1
    assert "BAZ_FN" in warnings[0]
    assert "BAR_FN" not in warnings[0]


# ===========================================================================
# NEGATIVE — W85 must NOT fire
# ===========================================================================

def test_no_fire_on_exact_match(fake_graph):
    fake_graph["names"] = {"FN_LOAD_OPS_RISK_DATA"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FN_LOAD_OPS_RISK_DATA work?",
        redis_client=object(),
        w70_anchor={"function": "FN_LOAD_OPS_RISK_DATA"},
    )
    assert warnings == []


def test_no_fire_on_match_after_case_normalization(fake_graph):
    """User lowercases the function name; anchor uppercases. Match."""
    fake_graph["names"] = {"FN_LOAD_OPS_RISK_DATA"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="how does fn_load_ops_risk_data work?",
        redis_client=object(),
        w70_anchor={"function": "FN_LOAD_OPS_RISK_DATA"},
    )
    assert warnings == []


def test_no_fire_on_cap_code_query(fake_graph):
    """BI routing: user asked CAP973, cascade lands on a real function
    (intentional anchor redirection). CAP973 has no underscore, fails
    the W58 candidate filter, so the asked-list is empty and W85
    no-ops. This is the critical false-positive guard."""
    fake_graph["names"] = {"REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How is CAP973 calculated?",
        redis_client=object(),
        w70_anchor={
            "function": "REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP"
        },
    )
    assert warnings == []


def test_no_fire_on_column_query(fake_graph):
    """Column-style asked entity (N_*, V_*, F_*, D_* prefix) — dropped
    by the W58 candidate filter. Variable-trace queries don't fire
    W85."""
    fake_graph["names"] = {"OPS_RISK_DATA_POPULATION_CSTM"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query=(
            "Trace `N_SHAREHOLDING_PERCENT` from STG_OPS_RISK_DATA "
            "to FCT_STANDARD_ACCT_HEAD."
        ),
        redis_client=object(),
        w70_anchor={"function": "OPS_RISK_DATA_POPULATION_CSTM"},
    )
    assert warnings == []


def test_no_fire_on_table_query(fake_graph):
    """Table-prefix asked entity (FCT_*, DIM_*, STG_*, FSI_*) — dropped
    by W58. No fire."""
    fake_graph["names"] = {"POPULATE_STDACC_FROMGL"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="What populates FCT_STANDARD_ACCT_HEAD?",
        redis_client=object(),
        w70_anchor={"function": "POPULATE_STDACC_FROMGL"},
    )
    assert warnings == []


def test_no_fire_when_no_anchor_resolved(fake_graph):
    """No w70 and no w76 → cannot compare; no-op."""
    fake_graph["names"] = {"FOO_FN"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FOO_FN work?",
        redis_client=object(),
        w70_anchor=None,
        w76_anchor=None,
    )
    assert warnings == []


def test_no_fire_on_empty_query(fake_graph):
    """Empty raw_query → extractor returns empty list → no-op."""
    fake_graph["names"] = {"FOO_FN"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="",
        redis_client=object(),
        w70_anchor={"function": "FOO_FN"},
    )
    assert warnings == []


def test_no_fire_on_unknown_asked_function(fake_graph):
    """User names a function-shaped string that isn't in the graph
    (typo, hallucinated, deleted). W45-style ungrounded-identifier
    territory; W85 stays out."""
    fake_graph["names"] = {"REAL_FUNCTION_NAME"}  # NONEXISTENT_FN not in graph
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does NONEXISTENT_FUNCTION_XYZ work?",
        redis_client=object(),
        w70_anchor={"function": "REAL_FUNCTION_NAME"},
    )
    assert warnings == []


def test_no_fire_on_multi_function_query_anchor_matches_one(fake_graph):
    """User names two functions ('Compare FN_A and FN_B'); anchor
    lands on FN_A. FN_B is still mentioned, but the anchor matches
    one named candidate → no mismatch."""
    fake_graph["names"] = {"FN_LOAD_OPS_RISK_DATA", "OPS_RISK_DATA_POPULATION_CSTM"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query=(
            "Compare FN_LOAD_OPS_RISK_DATA and "
            "OPS_RISK_DATA_POPULATION_CSTM."
        ),
        redis_client=object(),
        w70_anchor={"function": "FN_LOAD_OPS_RISK_DATA"},
    )
    assert warnings == []


# ===========================================================================
# EDGE CASES — anchor shape, redis errors, defensive coding
# ===========================================================================

def test_w70_anchor_full_dict_shape(fake_graph):
    """Production w70_anchor shape: dict with function, source,
    confidence. Extract .function field correctly."""
    fake_graph["names"] = {"FOO_FN", "BAR_FN"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FOO_FN work?",
        redis_client=object(),
        w70_anchor={
            "function": "BAR_FN",
            "source": "w76_prefix",
            "confidence": "high",
        },
    )
    assert len(warnings) == 1


def test_w70_anchor_unexpected_string_shape_safe_noop(fake_graph):
    """If w70_anchor is somehow a bare string (not a dict), W85's
    isinstance check rejects it and falls back to w76 / no-op.
    Defensive: prefer no-op over crashing."""
    fake_graph["names"] = {"FOO_FN"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FOO_FN work?",
        redis_client=object(),
        w70_anchor="BAR_FN",  # wrong shape
        w76_anchor=None,
    )
    assert warnings == []


def test_w70_anchor_empty_dict_falls_back(fake_graph):
    """w70_anchor={} → falls back to w76; w76 also empty → no-op."""
    fake_graph["names"] = {"FOO_FN"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FOO_FN work?",
        redis_client=object(),
        w70_anchor={},
        w76_anchor={},
    )
    assert warnings == []


def test_w70_anchor_function_empty_string_falls_back(fake_graph):
    """w70_anchor={"function": ""} → empty function → fall back. If
    w76 also empty → no-op."""
    fake_graph["names"] = {"FOO_FN", "BAR_FN"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FOO_FN work?",
        redis_client=object(),
        w70_anchor={"function": "", "source": "x"},
        w76_anchor={"function": "BAR_FN"},
    )
    # Falls back to w76 → BAR_FN → mismatch with FOO_FN → fire.
    assert len(warnings) == 1
    assert "BAR_FN" in warnings[0]


def test_redis_exception_during_lookup_safe(monkeypatch):
    """If function_exists_in_graph raises (e.g., redis down), the
    check catches and treats as 'not known'. Whole check no-ops
    if no asked candidate succeeds — fail open, not closed."""
    def raising(function_name, redis_client, schemas=None):
        raise RuntimeError("redis down")
    monkeypatch.setattr(_orch_mod, "function_exists_in_graph", raising)
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FOO_FN work?",
        redis_client=object(),
        w70_anchor={"function": "BAR_FN"},
    )
    assert warnings == []


# ===========================================================================
# Dedup posture — W85 fires INDEPENDENTLY of other W57 checks
# ===========================================================================

def test_does_not_dedup_with_w83b(fake_graph):
    """A body that triggers W83B (hedged December framing) PLUS a
    mismatched anchor → both warnings present. Anchor mismatch and
    content fabrication are distinct trust violations; collapsing
    them would underreport."""
    fake_graph["names"] = {
        "CS_GOODWILL_CALCULATION",
        "CS_GOODWILL_NET_OF_DTL_CALCULATION",
    }
    body = (
        "## CS_GOODWILL_NET_OF_DTL_CALCULATION\n"
        "The function is executed, particularly when the reporting "
        "month is December, for year-end processing."
    )
    multi_source = {
        "CS_GOODWILL_NET_OF_DTL_CALCULATION": {
            "source_code": [{
                "line": 1,
                "text": "WHERE D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD')",
            }],
        }
    }
    warnings = w57_enforce_grounding(
        raw_query="How does CS_Goodwill_Calculation work?",
        markdown=body,
        multi_source=multi_source,
        functions_analyzed=["CS_GOODWILL_NET_OF_DTL_CALCULATION"],
        redis_client=object(),
        w70_anchor={
            "function": "CS_GOODWILL_NET_OF_DTL_CALCULATION",
            "source": "object_name",
            "confidence": "high",
        },
    )
    w85_warnings = [w for w in warnings if "GROUNDING-ANCHOR-MISMATCH-HIGH" in w]
    w83b_warnings = [w for w in warnings if "GROUNDING-CALENDAR-HIGH" in w]
    assert len(w85_warnings) == 1
    assert len(w83b_warnings) == 1


def test_no_w85_when_anchor_matches_asked(fake_graph):
    """End-to-end: clean happy path. Asked == anchor → no W85 fire,
    even if other checks fire on the body."""
    fake_graph["names"] = {"FN_LOAD_OPS_RISK_DATA"}
    body = "## FN_LOAD_OPS_RISK_DATA\nThe function loads operational risk data."
    multi_source = {
        "FN_LOAD_OPS_RISK_DATA": {
            "source_code": [{"line": 1, "text": "BEGIN COMMIT; END;"}],
        }
    }
    warnings = w57_enforce_grounding(
        raw_query="How does FN_LOAD_OPS_RISK_DATA work?",
        markdown=body,
        multi_source=multi_source,
        functions_analyzed=["FN_LOAD_OPS_RISK_DATA"],
        redis_client=object(),
        w70_anchor={"function": "FN_LOAD_OPS_RISK_DATA"},
    )
    w85 = [w for w in warnings if "GROUNDING-ANCHOR-MISMATCH-HIGH" in w]
    assert w85 == []


def test_w85_runs_when_threaded_through_enforce_grounding(fake_graph):
    """The new kwargs reach the new check end-to-end via
    w57_enforce_grounding. Same body, no content fabrications;
    mismatch on routing only.

    Uses six-plus-char function names because the W58 candidate
    filter drops anything shorter — that's an upstream rule, not
    a W85 invariant. A real-world bench query would never name a
    sub-6-char function.
    """
    fake_graph["names"] = {"FN_FIRST", "FN_SECOND"}
    body = "## FN_SECOND\nThe function FN_SECOND does its work."
    multi_source = {"FN_SECOND": {"source_code": [{"line": 1, "text": "x"}]}}
    warnings = w57_enforce_grounding(
        raw_query="How does FN_FIRST work?",
        markdown=body,
        multi_source=multi_source,
        functions_analyzed=["FN_SECOND"],
        redis_client=object(),
        w70_anchor={"function": "FN_SECOND"},
    )
    w85 = [w for w in warnings if "GROUNDING-ANCHOR-MISMATCH-HIGH" in w]
    assert len(w85) == 1
    assert "FN_SECOND" in w85[0]
    assert "FN_FIRST" in w85[0]


def test_emits_single_warning_per_response(fake_graph):
    """W85 emits at most one warning per response. The canonical-text
    set dedup at the bottom of w57_enforce_grounding collapses any
    duplicate fires (none expected — the check returns at most one
    item — but locking the invariant)."""
    fake_graph["names"] = {"FN_FIRST", "FN_SECOND"}
    warnings = _w57_check_anchor_vs_asked_mismatch(
        raw_query="How does FN_FIRST work?",
        redis_client=object(),
        w70_anchor={"function": "FN_SECOND"},
    )
    assert len(warnings) == 1
