"""W168 — attribution-consistency gate (routing anchor vs prose subject).

The adversarial run found the badge anti-correlated with correctness:
wrong-function answers shipped at VERIFIED·95% when the routing anchor (the
function the pipeline resolved) was absent from the prose the answer actually
wrote. W168 computes that disagreement and caps the badge at UNVERIFIED.

Predicate (all must hold to fire):
  1. query_type != VARIABLE_TRACE          (W159 fan-in must-not-regress)
  2. an anchor resolves (w70 fn, else w76 fn)
  3. anchor source is EXPLICIT             (w76_* / classifier_object /
                                            bi_routing / raw_query_scan;
                                            semantic_top1 + unknown suppressed)
  4. prose cites >= 1 function
  5. normalize(anchor) is neither in the prose fn SET nor a body substring
     (the substring belt)

W168 is DISJOINT from W166: W168 catches `anchor not-in prose` (prose drifted
AWAY from the routed fn); W166 is the opposite (anchor itself wrong but prose
faithful to it -> `anchor in prose`), which W168 never fires on. So suppressing
on semantic_top1 sacrifices zero W166 coverage.

SCOPE (validated live). W168 catches anchor-ABSENT / "silent-swap" drift only:
the routed function name appears NOWHERE in the prose (proven live on "How is
CAP973 calculated?"). It deliberately does NOT catch "framing-drift" — where
the prose names the anchor as framing context but substantively describes a
different function (e.g. "In the function FN_G_TEST_CSTM, ... updated in
ABL_INV_ASSET_CLASS_RECLASS"), so `anchor in prose` and the set-membership
predicate (lenient by design to keep Q39 safe) does not fire. The adversarial
Q12/Q16/Q48 are framing-drift and are NOT caught here — separate follow-up
(W169). See test_no_fire_anchor_is_prose_member /
test_no_fire_q39_multi_function_comparison for the set-membership behaviour
that (correctly) makes framing-drift out of scope.

NOTE on source strings: these tests use the REAL `source` values emitted by
src.agents.anchor_resolution ("bi_routing", "classifier_object",
"raw_query_scan", "w76_prefix", "semantic_top1") — NOT the illustrative
"object_name" the W85 test uses. W85 ignores source; W168 reads it, so the
exact strings matter.
"""

import pytest

from src.agents.logic_explainer import (
    _w168_check_attribution_mismatch,
    _w168_extract_prose_function_names,
    _W168_EXPLICIT_ANCHOR_SOURCES,
    evaluate_grounding,
)

# A markdown body that names FN_PROSE_SUBJECT in all three extractable
# framings (heading, prose-framing, parenthesised citation) so the prose
# function set is unambiguous.
_MD_NAMES_PROSE_SUBJECT = (
    "## FN_PROSE_SUBJECT\n"
    "The function `FN_PROSE_SUBJECT` performs the calculation "
    "(FN_PROSE_SUBJECT, Lines 5-10)."
)


def _src(line_count, text="dummy"):
    """Build a multi_source source_code list of *line_count* line dicts."""
    return [{"line": i, "text": text} for i in range(1, line_count + 1)]


# ===========================================================================
# Extractor
# ===========================================================================

def test_extract_collects_all_three_framings():
    assert _w168_extract_prose_function_names(_MD_NAMES_PROSE_SUBJECT) == {
        "FN_PROSE_SUBJECT"
    }


def test_extract_empty_when_no_function_named():
    assert _w168_extract_prose_function_names(
        "Just prose with no function tokens, only Lines 5-10 cited."
    ) == set()


def test_extract_normalizes_case():
    # Backticked prose-framing form with mixed case -> normalized UPPER.
    md = "The function `Cs_Net_At1` runs here (Cs_Net_At1, Lines 1-2)."
    assert _w168_extract_prose_function_names(md) == {"CS_NET_AT1"}


def test_extract_drops_table_and_column_tokens():
    # W58 filter: FCT_/N_ prefixes are not functions.
    md = (
        "## FCT_STANDARD_ACCT_HEAD\n"
        "The function `N_STD_ACCT_HEAD_AMT` is referenced "
        "(FN_REAL_ONE, Lines 1-2)."
    )
    assert _w168_extract_prose_function_names(md) == {"FN_REAL_ONE"}


# ===========================================================================
# POSITIVE — W168 should fire
# ===========================================================================

def test_fires_explicit_bi_routing_anchor_absent_from_prose():
    warnings = _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="FUNCTION_LOGIC",
        w70_anchor={
            "function": "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT",
            "source": "bi_routing",
            "confidence": "high",
        },
    )
    assert len(warnings) == 1
    assert warnings[0].startswith("GROUNDING-ATTRIBUTION-MISMATCH-HIGH:")
    assert "FN_PROSE_SUBJECT" in warnings[0]
    assert "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT" in warnings[0]


@pytest.mark.parametrize("source", sorted(_W168_EXPLICIT_ANCHOR_SOURCES))
def test_fires_for_each_explicit_source(source):
    warnings = _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="COLUMN_LOGIC",
        w70_anchor={"function": "CS_DIFFERENT_FN", "source": source},
    )
    assert len(warnings) == 1


def test_fires_on_w76_prefix_fallback_when_w70_unset():
    warnings = _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="COLUMN_LOGIC",
        w70_anchor=None,
        w76_anchor={"function": "CS_DIFFERENT_FN", "source": "w76_prefix"},
    )
    assert len(warnings) == 1


def test_fires_on_bare_w76_anchor_without_source_field():
    # A w76 fallback with no explicit source string is treated as an
    # explicit "In <Fn>" prefix (w76_prefix).
    warnings = _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="FUNCTION_LOGIC",
        w70_anchor=None,
        w76_anchor={"function": "CS_DIFFERENT_FN"},
    )
    assert len(warnings) == 1


# ===========================================================================
# NEGATIVE — W168 must NOT fire
# ===========================================================================

def test_no_fire_semantic_top1_suppressed():
    """The Q9 / W160 guard: weak semantic anchor -> comparison unreliable."""
    assert _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="COLUMN_LOGIC",
        w70_anchor={"function": "CS_DIFFERENT_FN", "source": "semantic_top1"},
    ) == []


def test_no_fire_unknown_source_suppressed():
    """Fail-safe allow-list: an unrecognized future source defaults to
    suppress, not fire untested."""
    assert _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="COLUMN_LOGIC",
        w70_anchor={"function": "CS_DIFFERENT_FN", "source": "some_future_src"},
    ) == []


def test_no_fire_variable_trace_excluded():
    """The W159 fan-in must-not-regress: VARIABLE_TRACE legitimately spans
    many writers, none equal to the single anchor."""
    assert _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="VARIABLE_TRACE",
        w70_anchor={"function": "CS_DIFFERENT_FN", "source": "bi_routing"},
    ) == []


def test_no_fire_anchor_is_prose_member():
    """anchor == the prose subject (the passing COLUMN_LOGIC case)."""
    assert _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="FUNCTION_LOGIC",
        w70_anchor={"function": "FN_PROSE_SUBJECT", "source": "bi_routing"},
    ) == []


def test_no_fire_anchor_member_case_insensitive():
    assert _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="FUNCTION_LOGIC",
        w70_anchor={"function": "fn_prose_subject", "source": "bi_routing"},
    ) == []


def test_no_fire_q39_multi_function_comparison():
    """Q39: a legitimate 'compare A and B' answer names both; the anchor is
    one of them -> present among prose fns -> no fire."""
    md = (
        "## FN_ALPHA_CALC\nThe function `FN_ALPHA_CALC` differs from "
        "`FN_BETA_CALC` (FN_ALPHA_CALC, Lines 1-2) (FN_BETA_CALC, Lines 3-4)."
    )
    assert _w168_check_attribution_mismatch(
        markdown=md,
        query_type="COLUMN_LOGIC",
        w70_anchor={"function": "FN_BETA_CALC", "source": "raw_query_scan"},
    ) == []


def test_no_fire_substring_belt_anchor_named_informally():
    """The anchor is mentioned in the body outside any structured framing,
    so it is not in the extracted prose set but IS a body substring -> the
    belt suppresses (FP on a trust gate is worse than a Phase-2-backstopped
    miss)."""
    md = (
        "The flow runs through CS_DIFFERENT_FN before writing "
        "(FN_PROSE_SUBJECT, Lines 5-10)."
    )
    assert _w168_check_attribution_mismatch(
        markdown=md,
        query_type="FUNCTION_LOGIC",
        w70_anchor={"function": "CS_DIFFERENT_FN", "source": "bi_routing"},
    ) == []


def test_no_fire_when_prose_names_no_function():
    assert _w168_check_attribution_mismatch(
        markdown="Just prose, only Lines 5-10 cited, no function token.",
        query_type="FUNCTION_LOGIC",
        w70_anchor={"function": "CS_DIFFERENT_FN", "source": "bi_routing"},
    ) == []


def test_no_fire_when_no_anchor():
    assert _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="FUNCTION_LOGIC",
        w70_anchor=None,
        w76_anchor=None,
    ) == []


def test_no_fire_anchor_empty_function_string():
    assert _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="FUNCTION_LOGIC",
        w70_anchor={"function": "", "source": "bi_routing"},
    ) == []


def test_no_fire_non_dict_anchor_safe():
    assert _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="FUNCTION_LOGIC",
        w70_anchor="not a dict",
    ) == []


def test_w70_takes_precedence_over_w76_source():
    """w70 carries the authoritative source; a w70 semantic_top1 anchor must
    suppress even if a w76 explicit anchor is also present."""
    assert _w168_check_attribution_mismatch(
        markdown=_MD_NAMES_PROSE_SUBJECT,
        query_type="COLUMN_LOGIC",
        w70_anchor={"function": "CS_DIFFERENT_FN", "source": "semantic_top1"},
        w76_anchor={"function": "CS_OTHER_FN", "source": "w76_prefix"},
    ) == []


# ===========================================================================
# GROUNDING MACHINERY (non-negotiable gate): warning -> blocking_warnings ->
# UNVERIFIED -> 0.4, exactly like W85. Mirrors
# test_w57_grounding.test_evaluate_grounding_d1_caveat_flips_badge.
# ===========================================================================

def test_w168_warning_flips_badge_via_evaluate_grounding():
    """The W168 warning must route through blocking_warnings -> UNVERIFIED.
    Isolation: the prose subject (FN_PROSE_SUBJECT) IS in functions_analyzed
    so per-claim-binding stays silent; raw_query carries no identifier code
    so UNGROUNDED_IDENTIFIERS does not confound -> W168 is the sole flipper."""
    result = evaluate_grounding(
        raw_query="How does the phase-in deduction work?",
        markdown=_MD_NAMES_PROSE_SUBJECT,
        multi_source={"FN_PROSE_SUBJECT": {"source_code": _src(100)}},
        functions_analyzed=["FN_PROSE_SUBJECT"],
        query_type="FUNCTION_LOGIC",
        redis_client=None,
        w70_anchor={
            "function": "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT",
            "source": "bi_routing",
            "confidence": "high",
        },
    )
    assert result["badge"] == "UNVERIFIED"
    assert result["confidence"] == 0.4  # citations present -> 0.4 bucket
    assert any(
        "GROUNDING-ATTRIBUTION-MISMATCH-HIGH" in w for w in result["warnings"]
    )


def test_evaluate_grounding_verified_when_anchor_in_prose():
    """Non-regression: when the anchor IS the prose subject, the badge stays
    VERIFIED (the passing COLUMN_LOGIC case, Q9 broad answer)."""
    result = evaluate_grounding(
        raw_query="How does the phase-in deduction work?",
        markdown=_MD_NAMES_PROSE_SUBJECT,
        multi_source={"FN_PROSE_SUBJECT": {"source_code": _src(100)}},
        functions_analyzed=["FN_PROSE_SUBJECT"],
        query_type="FUNCTION_LOGIC",
        redis_client=None,
        w70_anchor={"function": "FN_PROSE_SUBJECT", "source": "bi_routing"},
    )
    assert result["badge"] == "VERIFIED"
    assert all(
        "GROUNDING-ATTRIBUTION-MISMATCH-HIGH" not in w
        for w in result["warnings"]
    )


def test_evaluate_grounding_semantic_anchor_no_w168_downgrade():
    """A weak semantic anchor that differs from the prose must NOT trigger a
    W168 downgrade (the Q9 / W160 regime is owned by W150 / Phase 2)."""
    result = evaluate_grounding(
        raw_query="How does the goodwill deduction logic work?",
        markdown=_MD_NAMES_PROSE_SUBJECT,
        multi_source={"FN_PROSE_SUBJECT": {"source_code": _src(100)}},
        functions_analyzed=["FN_PROSE_SUBJECT"],
        query_type="COLUMN_LOGIC",
        redis_client=None,
        w70_anchor={"function": "CS_DIFFERENT_FN", "source": "semantic_top1"},
    )
    assert all(
        "GROUNDING-ATTRIBUTION-MISMATCH-HIGH" not in w
        for w in result["warnings"]
    )


def test_evaluate_grounding_variable_trace_no_w168():
    """VARIABLE_TRACE fan-in: even with a divergent explicit anchor, W168 is
    excluded by the query_type guard (W159 non-regression at the
    evaluate_grounding boundary)."""
    result = evaluate_grounding(
        raw_query="How is N_STD_ACCT_HEAD_AMT calculated?",
        markdown=_MD_NAMES_PROSE_SUBJECT,
        multi_source={"FN_PROSE_SUBJECT": {"source_code": _src(100)}},
        functions_analyzed=["FN_PROSE_SUBJECT"],
        query_type="VARIABLE_TRACE",
        redis_client=None,
        w70_anchor={"function": "CS_DIFFERENT_FN", "source": "bi_routing"},
    )
    assert all(
        "GROUNDING-ATTRIBUTION-MISMATCH-HIGH" not in w
        for w in result["warnings"]
    )
