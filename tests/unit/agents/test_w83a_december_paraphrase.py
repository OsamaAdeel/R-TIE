"""W83 Option A: December/year-end paraphrase pattern tests.

W57 Check 5 catches the literal phrase ``only runs when the reporting
month is December``. W70 (merge d106d7e, 2026-05-10) reframed
gpt-4o-mini output to paraphrases (``is executed only when the
reporting month is December, as indicated by the conditional checks in
the code``) that evade the literal matcher. W83 Option A adds
:func:`_w57_check_december_paraphrase` to catch the paraphrase
classes.

Asymmetric design: false positives — flagging non-December date filters
as if they were December claims — NOT tolerable. False negatives on
edge paraphrases (``limited to the year-end reporting cycle``,
``processes fiscal year-end``) tolerable — W83 Option B's
content-grounded check covers those post-Run-8.

Reference incident: source check on
CS_Deferred_Tax_Asset_Net_of_DTL_Calculation (2026-05-11) confirmed no
month-12 logic; its date filter is
``D_CALENDAR_DATE = TO_DATE('20260331', 'yyyymmdd')`` — March 31,
2026. Post-W70 the response asserted December gating in paraphrased
language and badged VERIFIED. W83a fixes the calibration gap.
"""

from src.agents.logic_explainer import (
    _W57_DECEMBER_PARAPHRASE_PATTERNS,
    _W57_DECEMBER_GATE_PATTERNS,
    _w57_check_december_paraphrase,
    _w57_source_has_december_gate,
    w57_enforce_grounding,
)


def _src_text(text: str):
    """Build a single-line source_code list with arbitrary text."""
    return [{"line": 1, "text": text}]


# Source excerpt for CS_Deferred_Tax_Asset_Net_of_DTL_Calculation —
# hardcoded March 31 date filter, no month-12 logic. Per the W70 canary
# B reproduction.
_CS_DEFERRED_TAX_SRC_NO_DECEMBER = (
    "  WHERE D_CALENDAR_DATE = TO_DATE('20260331', 'yyyymmdd') "
    "AND n_run_skey = '870'"
)

# Source excerpt that legitimately has December gating (negative
# control). Used to confirm the check stays silent when the claim is
# grounded.
_GENUINE_DECEMBER_SRC = (
    "  WHERE EXTRACT(MONTH FROM v_mis_date) = 12 "
    "AND n_run_skey = '870'"
)


# ===========================================================================
# Source-content gate
# ===========================================================================

def test_gate_extract_month_from_returns_true():
    assert _w57_source_has_december_gate(
        "WHERE EXTRACT(MONTH FROM v_mis_date) = 12"
    )


def test_gate_to_char_mm_12_returns_true():
    assert _w57_source_has_december_gate(
        "WHERE TO_CHAR(v_mis_date, 'MM') = '12'"
    )


def test_gate_month_december_literal_returns_true():
    assert _w57_source_has_december_gate(
        "WHERE TO_CHAR(v_mis_date, 'MONTH') = 'DECEMBER'"
    )


def test_gate_month_equals_12_returns_true():
    assert _w57_source_has_december_gate("AND v_month = 12")


def test_gate_year_end_calendar_literal_returns_true():
    """OFSAA year-end run: TO_DATE('20251231', ...)."""
    assert _w57_source_has_december_gate(
        "WHERE D_CALENDAR_DATE = TO_DATE('20251231', 'yyyymmdd')"
    )


def test_gate_march_31_returns_false():
    """The actual CS_Deferred_Tax filter — March 31, NOT month-12."""
    assert not _w57_source_has_december_gate(
        _CS_DEFERRED_TAX_SRC_NO_DECEMBER
    )


def test_gate_empty_source_returns_false():
    assert not _w57_source_has_december_gate("")


def test_gate_unrelated_date_filter_returns_false():
    """``v_mis_date = SYSDATE`` is a date filter but not December."""
    assert not _w57_source_has_december_gate(
        "WHERE v_mis_date = SYSDATE AND n_run_skey = '870'"
    )


# ===========================================================================
# Positive — pattern fires when source has no December gate
# ===========================================================================

def test_w70_canary_b_reproduction_fires():
    """The exact W70-canary-B paraphrase that surfaced the gap."""
    body = (
        "This function is executed only when the reporting month is "
        "December, as indicated by the conditional checks in the code."
    )
    multi = {
        "CS_Deferred_Tax_Asset_Net_of_DTL_Calculation": {
            "source_code": _src_text(_CS_DEFERRED_TAX_SRC_NO_DECEMBER)
        }
    }
    warnings = _w57_check_december_paraphrase(
        body, multi,
        asked_about_function="CS_Deferred_Tax_Asset_Net_of_DTL_Calculation",
    )
    assert len(warnings) == 1
    assert "GROUNDING-HIGH" in warnings[0]
    assert "CS_Deferred_Tax_Asset_Net_of_DTL_Calculation" in warnings[0]


def test_executes_only_in_december_fires():
    body = "It executes only in December for fiscal year-end reporting."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


def test_fires_only_in_december_fires():
    body = "The procedure fires only in December."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


def test_is_triggered_only_in_december_fires():
    body = "It is triggered only when the reporting month is December."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


def test_operates_only_in_december_fires():
    body = "The function operates only in December every fiscal year."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


def test_runs_at_year_end_fires():
    body = "This function only runs at year-end."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


def test_year_end_processing_only_fires():
    body = "This procedure is year-end processing only."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


def test_executes_only_at_fiscal_year_end_fires():
    body = "The routine executes only at fiscal year-end."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


def test_runs_only_in_q4_fires():
    body = "Only runs in Q4."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


def test_only_fires_in_fourth_quarter_fires():
    body = "It only fires in the fourth quarter of each year."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


# ===========================================================================
# Negative — pattern does NOT fire when source DOES have December logic
# ===========================================================================

def test_executes_only_in_december_grounded_no_warning():
    body = (
        "This function is executed only when the reporting month is "
        "December."
    )
    multi = {"CS_FOO": {"source_code": _src_text(_GENUINE_DECEMBER_SRC)}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert warnings == []


def test_year_end_grounded_with_month_december_literal_no_warning():
    body = "Only runs at year-end."
    src = "WHERE TO_CHAR(v_mis_date, 'MONTH') = 'DECEMBER'"
    multi = {"CS_FOO": {"source_code": _src_text(src)}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert warnings == []


def test_executes_only_in_december_grounded_to_char_mm_no_warning():
    body = "Executes only in December."
    src = "WHERE TO_CHAR(v_mis_date, 'MM') = '12'"
    multi = {"CS_FOO": {"source_code": _src_text(src)}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert warnings == []


def test_year_end_grounded_year_end_calendar_literal_no_warning():
    """An OFSAA year-end batch with TO_DATE('20251231', ...)."""
    body = "Only runs at year-end."
    src = "WHERE D_CALENDAR_DATE = TO_DATE('20251231', 'yyyymmdd')"
    multi = {"CS_FOO": {"source_code": _src_text(src)}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert warnings == []


# ===========================================================================
# Asymmetric — out-of-scope generic phrasings must NOT fire
# ===========================================================================

def test_generic_conditional_checks_phrase_no_warning():
    """Without a specific December claim, the phrase is too generic."""
    body = "The behavior is indicated by the conditional checks in the code."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert warnings == []


def test_generic_date_conditions_phrase_no_warning():
    body = "The date conditions in this function restrict the rows."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert warnings == []


def test_generic_month_end_gating_phrase_no_warning():
    """``month-end gating`` could be any month — W83 Option A skips
    this; W83 Option B's content-grounded check picks it up."""
    body = "There is month-end gating in this function."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert warnings == []


def test_generic_reporting_period_filter_no_warning():
    body = "The reporting period filter restricts the date range."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert warnings == []


def test_generic_calendar_gate_phrase_no_warning():
    body = "There is a calendar gate before the main logic."
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert warnings == []


# ===========================================================================
# Dedup — multiple matching patterns produce ONE warning
# ===========================================================================

def test_two_paraphrase_patterns_in_one_body_dedup_to_one():
    """Two distinct W83a paraphrase patterns in one body → one warning.

    Avoids Check 5's literal phrases ('only runs in december' /
    'only runs when the reporting month is december') so that the
    Check-5-cross-check skip doesn't short-circuit the test.
    """
    body = (
        "This function is executed only in December and operates "
        "only during December — both phrasings appear here."
    )
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


def test_three_paraphrase_patterns_in_one_body_dedup_to_one():
    body = (
        "It is triggered only in December. The function fires only in "
        "December. It also operates only during December."
    )
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1


# ===========================================================================
# W76 anchor integration — validation runs against asked-about, not siblings
# ===========================================================================

def test_anchor_validates_asked_about_not_sibling():
    """Asked-about CS_FOO has NO December; sibling CS_BAR does.
    Warning must fire (anchored to CS_FOO, ignores CS_BAR)."""
    body = "The function CS_FOO executes only in December."
    multi = {
        "CS_FOO": {"source_code": _src_text("WHERE v_mis_date = SYSDATE")},
        "CS_BAR": {
            "source_code": _src_text(
                "WHERE EXTRACT(MONTH FROM v_mis_date) = 12"
            )
        },
    }
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function="CS_FOO",
    )
    assert len(warnings) == 1
    assert "CS_FOO" in warnings[0]


def test_anchor_skips_when_target_unresolved():
    """No asked-about, no name in body, multiple sources -> skip."""
    body = "Something is executed only in December here."
    multi = {
        "CS_A": {"source_code": _src_text("WHERE x = y")},
        "CS_B": {"source_code": _src_text("WHERE p = q")},
    }
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function=None,
    )
    assert warnings == []


def test_anchor_falls_back_to_most_cited_when_no_asked_about():
    """No asked-about, but body cites CS_FOO repeatedly -> anchor on it."""
    body = (
        "CS_FOO is the focus. CS_FOO computes a value. "
        "CS_FOO is executed only in December."
    )
    multi = {
        "CS_FOO": {"source_code": _src_text("WHERE x = y")},
        "CS_BAR": {
            "source_code": _src_text(
                "WHERE EXTRACT(MONTH FROM v_mis_date) = 12"
            )
        },
    }
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function=None,
    )
    assert len(warnings) == 1
    assert "CS_FOO" in warnings[0]


def test_anchor_single_source_unambiguous():
    """One function in multi_source -> always that one, no warning when grounded."""
    body = "It executes only in December."
    multi = {"CS_ONLY": {"source_code": _src_text(_GENUINE_DECEMBER_SRC)}}
    warnings = _w57_check_december_paraphrase(
        body, multi, asked_about_function=None,
    )
    assert warnings == []


# ===========================================================================
# CAP973 / CS_Deferred_Tax reproduction (regression guard)
# ===========================================================================

def test_literal_and_paraphrase_in_same_body_dedup_to_one():
    """Cross-check between Check 5 literal phrase and W83a paraphrase.

    Body contains BOTH the literal Check-5 phrase ('only runs when the
    reporting month is December') AND a W83a paraphrase ('is executed
    only in December'). Source has no December gate.

    Expected: exactly ONE GROUNDING-HIGH warning about December gating
    in the final warnings array — not two (one from Check 5, one from
    W83a). This goes through ``w57_enforce_grounding`` end-to-end so
    the bottom-of-pipeline dedup is exercised.
    """
    body = (
        "## CS_FOO\n\n"
        "This function only runs when the reporting month is December. "
        "Furthermore, it is executed only in December for fiscal "
        "year-end reporting."
    )
    multi = {"CS_FOO": {"source_code": _src_text("WHERE x = y")}}
    warnings = w57_enforce_grounding(
        raw_query="In CS_FOO, when does it fire?",
        markdown=body,
        multi_source=multi,
        functions_analyzed=["CS_FOO"],
    )
    december_warnings = [
        w for w in warnings
        if "december" in w.lower() or "year-end" in w.lower()
    ]
    assert len(december_warnings) == 1, (
        f"Expected exactly one December warning across Check 5 + W83a; "
        f"got {len(december_warnings)}: {december_warnings}"
    )


def test_cs_deferred_tax_post_w70_canary_b_reproduction():
    """W70 canary B reproduction: VERIFIED + empty warnings is the bug;
    W83a must flip to a single GROUNDING-HIGH on the paraphrase."""
    body = (
        "## CS_Deferred_Tax_Asset_Net_of_DTL_Calculation\n\n"
        "When the EXP_11 branch fires (Lines 24-30), the function sets "
        "the deferred tax asset net of DTL value. This function is "
        "executed only when the reporting month is December, as "
        "indicated by the conditional checks in the code."
    )
    multi = {
        "CS_Deferred_Tax_Asset_Net_of_DTL_Calculation": {
            "source_code": _src_text(_CS_DEFERRED_TAX_SRC_NO_DECEMBER)
        }
    }
    warnings = _w57_check_december_paraphrase(
        body, multi,
        asked_about_function="CS_Deferred_Tax_Asset_Net_of_DTL_Calculation",
    )
    assert len(warnings) == 1
    assert "GROUNDING-HIGH" in warnings[0]
    assert "CS_Deferred_Tax_Asset_Net_of_DTL_Calculation" in warnings[0]
