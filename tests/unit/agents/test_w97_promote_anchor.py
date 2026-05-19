"""W97 — unit tests for ``promote_anchor_to_front``.

The helper extends the W95 architectural principle (anchor resolution
must be reflected in downstream retrieval) one stage further: when the
anchored function is present in ``multi_source`` but at a low rank,
promote it to position 0 so the LLM reads its source first in the user
message. The system-prompt anchor block (W70) is reinforced by primacy-
of-appearance in the source pile.

Companion to ``test_w70_anchor_injection.py`` (cascade + anchor block
rendering); this file only covers the promote step. The integration
canary in ``tests/integration/test_live_stream.py`` covers the end-to-
end FN_LOAD_OPS_RISK_DATA VERIFIED → anchor at functions_analyzed[0]
shape against a live backend.
"""

from __future__ import annotations

from typing import Any, Dict

from src.agents.anchor_resolution import promote_anchor_to_front


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(score: float, description: str = "") -> Dict[str, Any]:
    return {
        "source_code": [],
        "schema": "OFSERM",
        "description": description,
        "tables_read": "",
        "tables_written": "",
        "score": score,
    }


def _anchor(fn: str, *, confidence: str = "high",
            source: str = "w76_prefix") -> Dict[str, Any]:
    return {"function": fn, "source": source, "confidence": confidence}


# =====================================================================
# Happy path: anchor present at rank N>0 — promoted to position 0,
# other entries preserve their relative order.
# =====================================================================


def test_promotes_anchor_from_back_to_front():
    multi_source = {
        "PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP": _entry(0.10),
        "CS_GOODWILL_INTANGIBLES_APRA_PHASE_IN_DEDUCTION_AMT": _entry(0.20),
        "CAP_CONSL_EFFECTIVE_SHAREHOLDING_PERCENT": _entry(0.30),
        "FN_LOAD_OPS_RISK_DATA": _entry(0.45),
    }
    anchor = _anchor("FN_LOAD_OPS_RISK_DATA")

    out = promote_anchor_to_front(multi_source, anchor)

    assert list(out.keys()) == [
        "FN_LOAD_OPS_RISK_DATA",
        "PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP",
        "CS_GOODWILL_INTANGIBLES_APRA_PHASE_IN_DEDUCTION_AMT",
        "CAP_CONSL_EFFECTIVE_SHAREHOLDING_PERCENT",
    ]
    # Entry payloads preserved (no shallow-copy of values).
    assert out["FN_LOAD_OPS_RISK_DATA"] is multi_source["FN_LOAD_OPS_RISK_DATA"]
    assert out["PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP"] is \
        multi_source["PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP"]


def test_promotes_anchor_from_middle():
    """Anchor at any non-zero position lands at index 0; the entries
    before AND after the anchor preserve their relative order."""
    multi_source = {
        "FN_A": _entry(0.10),
        "FN_B": _entry(0.20),
        "TARGET": _entry(0.30),
        "FN_C": _entry(0.40),
        "FN_D": _entry(0.50),
    }
    anchor = _anchor("TARGET")

    out = promote_anchor_to_front(multi_source, anchor)

    assert list(out.keys()) == ["TARGET", "FN_A", "FN_B", "FN_C", "FN_D"]


# =====================================================================
# Case-insensitive match (anchor cascade and search_results may differ).
# =====================================================================


def test_case_insensitive_anchor_match():
    """The anchor cascade may produce a canonical-cased function name
    while ``multi_source`` keys come from ``search_results`` with
    whatever casing the vector store stamped. Match must be case-
    insensitive — and the original key casing must be preserved in
    the promoted dict so downstream consumers (the meta event,
    ``functions_analyzed``, the LLM prompt) see the same string they
    would have seen pre-promotion."""
    multi_source = {
        "fn_other": _entry(0.10),
        "Fn_Load_Ops_Risk_Data": _entry(0.30),
    }
    anchor = _anchor("FN_LOAD_OPS_RISK_DATA")

    out = promote_anchor_to_front(multi_source, anchor)

    assert list(out.keys()) == ["Fn_Load_Ops_Risk_Data", "fn_other"]


# =====================================================================
# No-op: anchor already at position 0 (W95 injection path).
# =====================================================================


def test_noop_when_anchor_already_at_front():
    """W95 force-injects the anchor at search_results[0] when missing,
    so ``fetch_multi_logic`` lands it at ``multi_source[0]``. W97 must
    no-op in that case — same input dict returned (identity, not just
    equality, so callers don't pay for a needless rebuild)."""
    multi_source = {
        "FN_LOAD_OPS_RISK_DATA": _entry(0.0),
        "FN_OTHER": _entry(0.20),
    }
    anchor = _anchor("FN_LOAD_OPS_RISK_DATA")

    out = promote_anchor_to_front(multi_source, anchor)

    assert out is multi_source


def test_noop_when_anchor_already_at_front_case_insensitive():
    """Same no-op contract under case-mismatched keys."""
    multi_source = {
        "fn_load_ops_risk_data": _entry(0.0),
        "FN_OTHER": _entry(0.20),
    }
    anchor = _anchor("FN_LOAD_OPS_RISK_DATA")

    out = promote_anchor_to_front(multi_source, anchor)

    assert out is multi_source


# =====================================================================
# No-op: anchor not in multi_source. W95 should have force-injected it
# upstream, but in case retrieval and anchor genuinely disagree, the
# safe behavior is to leave multi_source alone (don't fabricate a
# source-less entry — that's W95's job, not W97's).
# =====================================================================


def test_noop_when_anchor_not_in_multi_source():
    multi_source = {
        "FN_A": _entry(0.10),
        "FN_B": _entry(0.20),
    }
    anchor = _anchor("ANCHOR_THAT_DOESNT_EXIST")

    out = promote_anchor_to_front(multi_source, anchor)

    assert out is multi_source


# =====================================================================
# Defensive no-ops: anchor None / empty function / non-dict shapes.
# =====================================================================


def test_noop_when_anchor_none():
    multi_source = {"FN_A": _entry(0.10), "FN_B": _entry(0.20)}
    out = promote_anchor_to_front(multi_source, None)
    assert out is multi_source


def test_noop_when_anchor_has_empty_function():
    """``determine_primary_anchor`` never emits an anchor with empty
    function (the cascade short-circuits on whitespace-only candidates),
    but the helper must tolerate it — defensive against future cascade
    refactors."""
    multi_source = {"FN_A": _entry(0.10), "FN_B": _entry(0.20)}
    out = promote_anchor_to_front(multi_source, {"function": "", "source": "x"})
    assert out is multi_source

    out = promote_anchor_to_front(multi_source, {"function": "   ",
                                                  "source": "x"})
    assert out is multi_source


def test_noop_when_anchor_missing_function_key():
    multi_source = {"FN_A": _entry(0.10)}
    out = promote_anchor_to_front(multi_source, {"source": "x"})
    assert out is multi_source


# =====================================================================
# Defensive no-ops: empty / falsy multi_source.
# =====================================================================


def test_noop_when_multi_source_empty():
    out = promote_anchor_to_front({}, _anchor("FN_X"))
    assert out == {}


def test_noop_when_multi_source_single_entry_and_matches_anchor():
    """One-entry dict where the single entry IS the anchor — already at
    position 0, no-op."""
    multi_source = {"FN_LOAD_OPS_RISK_DATA": _entry(0.0)}
    out = promote_anchor_to_front(multi_source,
                                  _anchor("FN_LOAD_OPS_RISK_DATA"))
    assert out is multi_source


def test_noop_when_multi_source_single_entry_does_not_match_anchor():
    """One-entry dict where the single entry is NOT the anchor —
    promote-to-front would fabricate an entry, which is not the helper's
    contract. No-op."""
    multi_source = {"FN_OTHER": _entry(0.10)}
    out = promote_anchor_to_front(multi_source,
                                  _anchor("FN_LOAD_OPS_RISK_DATA"))
    assert out is multi_source


# =====================================================================
# Idempotency: applying the helper twice yields the same result as
# applying it once.
# =====================================================================


def test_idempotent_when_anchor_already_promoted():
    multi_source = {
        "FN_A": _entry(0.10),
        "FN_B": _entry(0.20),
        "TARGET": _entry(0.30),
    }
    anchor = _anchor("TARGET")

    once = promote_anchor_to_front(multi_source, anchor)
    twice = promote_anchor_to_front(once, anchor)

    # Second call sees TARGET at position 0 — no-op return.
    assert twice is once
    assert list(once.keys()) == ["TARGET", "FN_A", "FN_B"]
