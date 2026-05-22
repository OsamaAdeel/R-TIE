"""W136: extend ``_W83B_RESTRICTIVE_QUALIFIER`` with four hedging phrases.

E3 ("What feeds data into FN_G_TEST_CSTM?") in the P1 quality harness
surfaced as HOLLOW VERIFIED. Its body asserted "executed under
specific conditions, primarily when the reporting month is December"
with no calendar warning fired. Cowork's W83d diagnostic isolated
the mechanism:

  - Check 5 literal phrases require "only runs when..." / "only runs
    in..." -- E3's "primarily when" matches NEITHER
  - W83a paraphrase patterns require some form of "only\\s+" near
    the verb -- E3 has no "only"
  - W83b's ``_W83B_RESTRICTIVE_QUALIFIER`` contained
    "particularly when" / "specifically when" / "under the condition
    that" but NOT "primarily when" / "mainly when" / "principally
    when" / "chiefly when"

Pure linguistic gap. W136 extends the qualifier tuple with the four
missing hedges. Same semantics as the existing entries, different
surface forms.

Note on the baseline test (``test_w136_baseline_e3_no_warning``):
the prompt's test-first protocol asks for a snapshot that captures
pre-fix behavior. Pre-W136 it passes (the E3 body produces no
calendar warning); post-W136 it FAILS by design (the warning now
fires). It is retained in this file only to demonstrate the flip
during code review and is expected to be marked failing post-fix.
The durable regression guard is
``test_w136_e3_warning_post_fix``.
"""

import pytest

from src.agents.logic_explainer import (
    _W83B_RESTRICTIVE_QUALIFIER,
    _w57_check_calendar_gating_grounded,
)


def _src_text(text: str):
    return [{"line": 1, "text": text}]


# Source for FN_G_TEST_CSTM (and any FN_X stand-in). No month-12
# logic; matches the shape used in test_w83b_calendar_gating_grounded
# and confirmed real for CS_Goodwill_Calculation. Triggers the W83b
# "source has no December gate" branch.
_NO_DECEMBER_SRC = (
    "  WHERE D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD') "
    "AND DIM_RUN.n_run_skey = '870'"
)


# Verbatim shape of E3's HOLLOW VERIFIED body from the P1 harness
# comparison report. Contains:
#   - gating verb "executed" (Class A)
#   - hedge "primarily when" (Class B -- NEW in W136)
#   - calendar token "December" (Class C)
# all within 80 chars in one sentence. No "only" near the verb, no
# Check-5 literal phrase, so neither W83a nor Check 5 fires; only
# the W83b qualifier-list extension can catch it.
_E3_HEDGED_BODY = (
    "The FN_G_TEST_CSTM function processes capital adequacy figures. "
    "This function is executed under specific conditions, primarily "
    "when the reporting month is December."
)


# -----------------------------------------------------------------------------
# Baseline (pre-W136) -- expected to flip post-fix
# -----------------------------------------------------------------------------

@pytest.mark.xfail(
    reason="W136 flips this: post-fix the body fires a "
           "GROUNDING-CALENDAR-HIGH warning. Retained to demonstrate "
           "the pre-fix state during review."
)
def test_w136_baseline_e3_no_warning():
    """Pre-W136: body with 'primarily when the reporting month is
    December' produces no calendar warning. W83b's qualifier list
    does not yet contain 'primarily when' so the co-occurrence rule
    does not match."""
    multi_source = {
        "FN_G_TEST_CSTM": {"source_code": _src_text(_NO_DECEMBER_SRC)}
    }
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_E3_HEDGED_BODY,
        multi_source=multi_source,
        asked_about_function="FN_G_TEST_CSTM",
    )
    assert warnings == []


# -----------------------------------------------------------------------------
# Post-W136 regression guards
# -----------------------------------------------------------------------------

def test_w136_e3_warning_post_fix():
    """Post-W136: same body produces a single
    GROUNDING-CALENDAR-HIGH warning naming FN_G_TEST_CSTM."""
    multi_source = {
        "FN_G_TEST_CSTM": {"source_code": _src_text(_NO_DECEMBER_SRC)}
    }
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_E3_HEDGED_BODY,
        multi_source=multi_source,
        asked_about_function="FN_G_TEST_CSTM",
    )
    assert len(warnings) == 1
    assert "GROUNDING-CALENDAR-HIGH" in warnings[0]
    assert "FN_G_TEST_CSTM" in warnings[0]


def test_w136_qualifier_list_extended():
    """Static: the four W136 hedges are present in
    ``_W83B_RESTRICTIVE_QUALIFIER``."""
    assert "primarily when" in _W83B_RESTRICTIVE_QUALIFIER
    assert "mainly when" in _W83B_RESTRICTIVE_QUALIFIER
    assert "principally when" in _W83B_RESTRICTIVE_QUALIFIER
    assert "chiefly when" in _W83B_RESTRICTIVE_QUALIFIER


def test_w136_existing_qualifiers_unchanged():
    """The pre-W136 qualifier members must remain present. W136 is
    additive only -- not a list replacement."""
    # The three the prompt called out explicitly...
    assert "particularly when" in _W83B_RESTRICTIVE_QUALIFIER
    assert "specifically when" in _W83B_RESTRICTIVE_QUALIFIER
    assert "under the condition that" in _W83B_RESTRICTIVE_QUALIFIER
    # ...plus a sample of the other pre-W136 entries to guard
    # against accidental list-truncation regressions.
    assert "only" in _W83B_RESTRICTIVE_QUALIFIER
    assert "contingent on" in _W83B_RESTRICTIVE_QUALIFIER
    assert "is fired when" in _W83B_RESTRICTIVE_QUALIFIER


def test_w136_non_calendar_body_unaffected():
    """A body containing one of the W136 hedges but NO calendar token
    must not trigger W83b. The hedge alone is not enough; co-
    occurrence with a Class C calendar referent within 80 chars is
    required."""
    body = (
        "FN_X is executed primarily when the upstream batch publishes "
        "its completion signal. Result is written to the staging table."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_NO_DECEMBER_SRC)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_w136_existing_qualifiers_still_fire():
    """The three original prompt-cited qualifiers
    ('particularly when', 'specifically when', 'under the condition
    that') must continue to trigger W83b on their canonical hedged
    framings. Confirms W136 didn't regress prior coverage."""
    multi_source = {"FN_X": {"source_code": _src_text(_NO_DECEMBER_SRC)}}

    for phrase in (
        "FN_X is executed particularly when the reporting month is December.",
        "FN_X is executed specifically when the reporting month is December.",
        "FN_X operates under the condition that the reporting month is December.",
    ):
        warnings = _w57_check_calendar_gating_grounded(
            markdown=phrase, multi_source=multi_source,
            asked_about_function="FN_X",
        )
        assert len(warnings) == 1, (
            f"Pre-W136 qualifier regressed; phrase did not fire: {phrase}"
        )
        assert "GROUNDING-CALENDAR-HIGH" in warnings[0]


def test_w136_each_new_hedge_fires_independently():
    """Each of the four new hedges, paired with a calendar token
    and a gating verb in one sentence, fires W83b on its own --
    confirming the list addition is wired through the co-occurrence
    matcher and isn't only catching 'primarily when' by accident."""
    multi_source = {"FN_X": {"source_code": _src_text(_NO_DECEMBER_SRC)}}

    for phrase in (
        "FN_X is executed primarily when the reporting month is December.",
        "FN_X is executed mainly when the reporting month is December.",
        "FN_X is executed principally when the reporting month is December.",
        "FN_X is executed chiefly when the reporting month is December.",
    ):
        warnings = _w57_check_calendar_gating_grounded(
            markdown=phrase, multi_source=multi_source,
            asked_about_function="FN_X",
        )
        assert len(warnings) == 1, (
            f"W136 hedge did not fire end-to-end: {phrase}"
        )
        assert "GROUNDING-CALENDAR-HIGH" in warnings[0]
