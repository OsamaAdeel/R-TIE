"""W83C: calendar-general overgeneralization detection.

W83B (merged 2026-05-13) catches hedged-framing claims that a function
is gated on December / year-end / Q4 when the cited source contains no
month-12 logic. W83C (this ticket, 2026-05-15) extends W83B from
December-only to *all* months, quarters, year-end variants, and
month-end-date claims.

Failure surface — stakeholder test 2 (2026-05-14):

    Query: "Trace `N_SIGNIFICANT_INVST_AMT` from classification
            through deduction."
    RTIE response (verbatim sentence):
        "This entire function ONLY runs when the reporting month is
        March 2026, specifically on the date March 31, 2026."
    Source: `D_CALENDAR_DATE = TO_DATE('20260331', ...)` — a single
            calendar-date filter, NOT a month-3 gate.

W83B's source-content gate would have caught the fabrication (source
has no month-12 logic), but W83B's prose pattern set is December-only
and never picked up the "March" claim. W83C closes the gap by:

  (1) extending Class C to cover all months / quarters / year-end /
      month-end dates with per-token (period_id, claim_type, label)
      tags;
  (2) adding a strict per-claim source-content gate:
      * month claims require MONTH/EXTRACT logic for that specific
        month (date literals do NOT suffice — closes the stakeholder
        case);
      * date claims accept matching date literals;
      * year-end stays lenient (December month evidence OR year-end
        date) for backward compat with W83a's `December gate`;
      * quarter claims accept any quarter-month evidence (lenient).

Asymmetric design (matches W83B): false positives on legitimate
calendar-gated functions intolerable; OVERGENERALIZATION (source has
a localized predicate, prose claims whole-function gating) is
deferred to W83D.

Anchor resolution, proximity rule (80 chars), dedup vs W83a Check 5,
and badge effect (UNVERIFIED) are unchanged from W83B.
"""

import pytest

from src.agents.logic_explainer import (
    _W57_MONTHS_META,
    _W83B_C_TOKEN_TAG_PAIRS,
    _W83B_CALENDAR_REFERENT,
    _w57_calendar_gate_supports_claim,
    _w57_check_calendar_gating_grounded,
    _w83b_collect_claim_tags,
    w57_enforce_grounding,
)


def _src_text(text: str):
    return [{"line": 1, "text": text}]


# Stakeholder-test-2 canonical artefact. Verbatim sentence reproduced
# from the 2026-05-14 chain-ordering test transcript.
_STAKEHOLDER_TEST_2_BODY = (
    "This entire function ONLY runs when the reporting month is "
    "March 2026, specifically on the date March 31, 2026."
)

# Source matching the stakeholder case: a single March-31 date
# filter, no MONTH/EXTRACT logic.
_MARCH_31_DATE_ONLY_SRC = (
    "  WHERE D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD') "
    "AND DIM_RUN.n_run_skey = '870'"
)

# Genuine March month-gate (used to confirm suppression of month
# claims when source has actual MONTH/EXTRACT logic).
_GENUINE_MARCH_MONTH_SRC = (
    "  IF EXTRACT(MONTH FROM v_mis_date) = 3 THEN ..."
)


# ===========================================================================
# Stakeholder test 2 reproduction (the canonical W83C target)
# ===========================================================================

def test_stakeholder_test_2_march_overgeneralization_fires():
    """Stakeholder test 2 verbatim. Body claims both a March-month
    gate AND a March-31 date gate; source has only the March-31 date.
    Month claim is unsupported → W83C fires. Date claim is supported
    → does not contribute to the warning but does not block firing on
    the unsupported month claim."""
    multi_source = {
        "N_SIGNIFICANT_INVST_AMT_FN": {
            "source_code": _src_text(_MARCH_31_DATE_ONLY_SRC)
        }
    }
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_STAKEHOLDER_TEST_2_BODY,
        multi_source=multi_source,
        asked_about_function="N_SIGNIFICANT_INVST_AMT_FN",
    )
    assert len(warnings) == 1
    msg = warnings[0]
    assert "GROUNDING-CALENDAR-HIGH" in msg
    assert "N_SIGNIFICANT_INVST_AMT_FN" in msg
    assert "March" in msg


# ===========================================================================
# Positive cases — month-claim shapes for all 11 non-December months
# ===========================================================================

def test_march_hedged_framing_fires():
    body = (
        "FN_X is contingent on the reporting month being March, which "
        "is the regulatory cutoff."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_MARCH_31_DATE_ONLY_SRC)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    assert "March" in warnings[0]


def test_june_hedged_framing_fires():
    body = (
        "FN_X operates under the condition that the reporting month is "
        "June, particularly for half-year regulatory reporting."
    )
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    assert "June" in warnings[0]


def test_september_particularly_when_fires():
    body = (
        "FN_X is executed under specific conditions, particularly when "
        "the reporting month is September for Q3 reporting."
    )
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    # September month + Q3 quarter both match — message lists at
    # least one of them.
    assert "September" in warnings[0] or "Q3" in warnings[0]


@pytest.mark.parametrize("month_name", [
    "January", "February", "April", "May", "June", "July",
    "August", "October", "November",
])
def test_all_non_december_non_march_months_fire(month_name):
    """Smoke-test for the full month coverage. Verifies that every
    month name participates in W83C's firing rule with hedged
    framing. December has its own W83B suite; March is covered
    above; the remaining 9 months are parameterized here."""
    body = (
        f"FN_X is contingent on the reporting month being {month_name}, "
        f"as required by regulatory submission."
    )
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    assert month_name in warnings[0]


# ===========================================================================
# Positive cases — quarter and year-end framings
# ===========================================================================

def test_q1_restricted_to_fires():
    body = "FN_X is restricted to the first quarter reporting cycle."
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    assert "Q1" in warnings[0]


def test_q2_only_during_fires():
    body = "FN_X runs exclusively during Q2."
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    assert "Q2" in warnings[0]


def test_q3_third_quarter_fires():
    body = "FN_X is fired only during the third quarter cycle."
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    assert "Q3" in warnings[0]


def test_end_of_q1_fires():
    body = "FN_X operates only at end of Q1 reporting."
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    assert "Q1" in warnings[0]


def test_year_end_fiscal_year_end_fires_still():
    """W83B regression-style — year-end hedged framing still fires
    under W83C with the same message shape (label "year-end / fiscal
    year-end"). Body uses a hedged form ("operates exclusively
    during ...") that W83a's verb-direct regex set deliberately
    excludes, so dedup-vs-W83a does not suppress."""
    body = (
        "FN_X operates exclusively during fiscal year-end processing "
        "windows, per the regulatory submission cycle."
    )
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    assert "year-end" in warnings[0]


# ===========================================================================
# Source-content gate — strict month-claim semantic
# ===========================================================================

def test_no_fire_when_source_has_real_march_month_gate():
    """Body claims March-month gating; source has EXTRACT(MONTH FROM
    ...) = 3. Month evidence supports the month claim → no fire."""
    body = (
        "FN_X is contingent on the reporting month being March, "
        "as required by quarter-end."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_GENUINE_MARCH_MONTH_SRC)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_no_fire_when_source_has_to_char_march_form():
    """Source uses TO_CHAR(..., 'MONTH') = 'MARCH' — counts as month
    evidence."""
    body = (
        "FN_X is contingent on the reporting month being March."
    )
    src = "WHERE TO_CHAR(v_mis_date, 'MONTH') = 'MARCH'"
    multi_source = {"FN_X": {"source_code": _src_text(src)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_no_fire_when_source_has_to_char_mm_form():
    """Source uses TO_CHAR(..., 'MM') = '03' — counts as month
    evidence for March."""
    body = "FN_X is contingent on the reporting month being March."
    src = "WHERE TO_CHAR(v_mis_date, 'MM') = '03'"
    multi_source = {"FN_X": {"source_code": _src_text(src)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_date_only_source_does_not_support_month_claim():
    """The W83C strict-semantic case: source has a March-31 date
    literal but NO MONTH/EXTRACT logic. A month claim is NOT
    supported; W83C fires.

    This is intentionally different from W83B's lenient December
    behavior (where a `'20251231'` literal suppresses a December
    month claim). The change scopes only to non-December months;
    year-end / December cases retain the lenient gate for backward
    compat with W83a."""
    body = (
        "FN_X is contingent on the reporting month being March, "
        "as required by quarter-end."
    )
    multi_source = {"FN_X": {"source_code": _src_text(_MARCH_31_DATE_ONLY_SRC)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    assert "March" in warnings[0]


def test_quarter_claim_suppressed_by_member_month_evidence():
    """Quarter claims are lenient — a function gating any single
    month of the quarter is treated as supporting the quarter claim."""
    body = "FN_X is restricted to the first quarter reporting cycle."
    src = "WHERE EXTRACT(MONTH FROM v_mis_date) = 2"  # February — Q1 member
    multi_source = {"FN_X": {"source_code": _src_text(src)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_year_end_claim_suppressed_by_date_literal():
    """Year-end remains lenient (W83a semantic): a `TO_DATE('YYYY1231',
    ...)` literal suppresses a year-end claim."""
    body = "FN_X runs exclusively at year-end as required."
    src = "WHERE D_CALENDAR_DATE = TO_DATE('20261231', 'YYYYMMDD')"
    multi_source = {"FN_X": {"source_code": _src_text(src)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


# ===========================================================================
# Negative cases — non-fabrications should not fire
# ===========================================================================

def test_descriptive_march_mention_no_fire():
    """Body mentions March without restrictive qualifier → no fire."""
    body = "FN_X processes records used in March reporting."
    multi_source = {"FN_X": {"source_code": _src_text(_MARCH_31_DATE_ONLY_SRC)}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_demarcation_substring_does_not_match_march():
    """Word-boundary regex prevents `demarcation` / `marching` from
    matching the bare `march` Class C token."""
    body = (
        "FN_X executes only when the demarcation policy applies."
    )
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_modal_may_does_not_match_may_month():
    """Modal `may` (verb auxiliary) is not the month claim. Word-
    boundary regex matches "may" only as a standalone word — which
    here is still a bare match. The fire-rule still requires a B
    qualifier in proximity. A descriptive "FN_X may execute under
    certain conditions" has no restrictive qualifier near `may`, so
    no fire."""
    body = "FN_X may execute under certain conditions."
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


def test_no_fire_when_empty_multi_source():
    warnings = _w57_check_calendar_gating_grounded(
        markdown=_STAKEHOLDER_TEST_2_BODY,
        multi_source={},
        asked_about_function="FN_X",
    )
    assert warnings == []


# ===========================================================================
# Dedup vs W83a Check 5 / W83a paraphrase
# ===========================================================================

def test_w83a_check5_literal_dedups_w83c():
    """If a Check 5 literal December phrase is present in the body,
    W83C must defer (same dedup that W83B uses)."""
    body = (
        "FN_X only runs in December. It is also contingent on the "
        "reporting month being March."
    )
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    # Check 5 literal phrase is in body → dedup gate skips W83C
    # entirely (even though a March claim would otherwise fire).
    assert warnings == []


def test_w83a_paraphrase_dedups_w83c():
    """A W83a paraphrase pattern in the body suppresses W83C."""
    body = (
        "FN_X is executed only when the reporting month is December "
        "for year-end. It also operates under specific conditions, "
        "particularly when the reporting month is March."
    )
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert warnings == []


# ===========================================================================
# Message naming and multi-period collation
# ===========================================================================

def test_message_names_detected_month():
    body = (
        "FN_X is contingent on the reporting month being June."
    )
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    msg = warnings[0]
    assert "June" in msg
    # Should NOT name unrelated periods.
    assert "December" not in msg
    assert "March" not in msg


def test_message_lists_multiple_periods_up_to_two():
    """Body claims gating on March AND June; source has neither.
    Message lists both periods (up to 2)."""
    body = (
        "FN_X is contingent on the reporting month being March. "
        "Additionally, FN_X operates under the condition that the "
        "reporting month is June for half-year reconciliation."
    )
    multi_source = {"FN_X": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_calendar_gating_grounded(
        markdown=body, multi_source=multi_source,
        asked_about_function="FN_X",
    )
    assert len(warnings) == 1
    msg = warnings[0]
    assert "March" in msg
    assert "June" in msg


# ===========================================================================
# Claim-tag collector white-box (anchor for future refactoring)
# ===========================================================================

def test_collect_claim_tags_picks_up_march_month_and_date():
    """The stakeholder body raises two claims: March-month and
    March-31-date. The collector should return both tags."""
    body_lower = _STAKEHOLDER_TEST_2_BODY.lower()
    tags = _w83b_collect_claim_tags(body_lower)
    assert any(t == ("march", "month", "March") for t in tags)
    assert any(t == ("march-31", "date", "March 31") for t in tags)


def test_collect_claim_tags_empty_when_no_firing():
    body_lower = "FN_X processes records used in march reporting.".lower()
    tags = _w83b_collect_claim_tags(body_lower)
    # Descriptive mention, no restrictive qualifier → no fire → no tags.
    assert tags == []


def test_calendar_gate_supports_month_claim_with_extract():
    assert _w57_calendar_gate_supports_claim(
        ("march", "month", "March"),
        "WHERE EXTRACT(MONTH FROM v_mis_date) = 3",
    )


def test_calendar_gate_does_not_support_month_claim_with_date_only():
    """Strict semantic: a single March date literal does NOT support
    a March-month claim."""
    assert not _w57_calendar_gate_supports_claim(
        ("march", "month", "March"),
        "WHERE D_CALENDAR_DATE = TO_DATE('20260331', 'YYYYMMDD')",
    )


def test_calendar_gate_supports_date_claim_with_matching_literal():
    """Date claims accept matching literals."""
    assert _w57_calendar_gate_supports_claim(
        ("march-31", "date", "March 31"),
        "WHERE D_CALENDAR_DATE = TO_DATE('20260331', 'YYYYMMDD')",
    )


def test_calendar_gate_supports_year_end_with_dec_date_literal():
    """Year-end is lenient — Dec-31 literal supports the claim
    (W83a backward compat)."""
    assert _w57_calendar_gate_supports_claim(
        ("year-end", "year-end", "year-end / fiscal year-end"),
        "WHERE D_CALENDAR_DATE = TO_DATE('20251231', 'YYYYMMDD')",
    )


def test_calendar_gate_supports_quarter_with_member_month():
    """Quarter is lenient — month-evidence for any member month
    supports the quarter claim."""
    assert _w57_calendar_gate_supports_claim(
        ("q1", "quarter", "Q1"),
        "WHERE EXTRACT(MONTH FROM v_mis_date) = 2",
    )


# ===========================================================================
# Existing W83B sanity — preserved through W83C
# ===========================================================================

def test_legacy_december_token_still_in_calendar_referent():
    assert "december" in _W83B_CALENDAR_REFERENT


def test_legacy_q4_token_still_in_calendar_referent():
    assert "q4" in _W83B_CALENDAR_REFERENT


def test_legacy_year_end_token_still_in_calendar_referent():
    assert "year-end" in _W83B_CALENDAR_REFERENT


def test_new_march_tokens_in_calendar_referent():
    """`march` is added via word-boundary regex (not literal
    substring), so the `in` membership check uses
    `_W83B_C_TOKEN_TAG_PAIRS` rather than the flat tuple."""
    march_tags = [
        tag for _tok, tag in _W83B_C_TOKEN_TAG_PAIRS
        if tag == ("march", "month", "March")
    ]
    assert march_tags  # at least one token has the March-month tag


def test_new_quarter_tokens_in_calendar_referent():
    q1_tags = [
        tag for _tok, tag in _W83B_C_TOKEN_TAG_PAIRS
        if tag == ("q1", "quarter", "Q1")
    ]
    assert q1_tags


def test_metadata_covers_all_twelve_months():
    assert len(_W57_MONTHS_META) == 12
    names = [m[1] for m in _W57_MONTHS_META]
    for expected in (
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november",
        "december",
    ):
        assert expected in names


# ===========================================================================
# End-to-end via w57_enforce_grounding
# ===========================================================================

def test_enforce_grounding_emits_one_w83c_warning_for_stakeholder_case():
    multi_source = {
        "N_SIGNIFICANT_INVST_AMT_FN": {
            "source_code": _src_text(_MARCH_31_DATE_ONLY_SRC)
        }
    }
    warnings = w57_enforce_grounding(
        raw_query="Trace N_SIGNIFICANT_INVST_AMT.",
        markdown=_STAKEHOLDER_TEST_2_BODY,
        multi_source=multi_source,
        functions_analyzed=["N_SIGNIFICANT_INVST_AMT_FN"],
    )
    cal_warnings = [w for w in warnings if "GROUNDING-CALENDAR-HIGH" in w]
    assert len(cal_warnings) == 1
    assert "March" in cal_warnings[0]


def test_enforce_grounding_w83b_december_regression_unchanged():
    """W83B's canonical A2 case (CS_Goodwill December overgen) must
    keep firing under W83C with the same warning shape."""
    body = (
        "The CS_Goodwill_Calculation function is designed to compute "
        "and merge goodwill-related capital adjustments. This function "
        "is executed under specific conditions, particularly when the "
        "reporting month is December, which is crucial for year-end "
        "financial reporting."
    )
    multi_source = {
        "CS_GOODWILL_CALCULATION": {
            "source_code": _src_text(
                "  WHERE D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD') "
                "AND DIM_RUN.n_run_skey = '870'"
            )
        }
    }
    warnings = w57_enforce_grounding(
        raw_query="How does CS_Goodwill_Calculation work?",
        markdown=body,
        multi_source=multi_source,
        functions_analyzed=["CS_GOODWILL_CALCULATION"],
    )
    cal_warnings = [w for w in warnings if "GROUNDING-CALENDAR-HIGH" in w]
    assert len(cal_warnings) == 1
    assert "December" in cal_warnings[0]
