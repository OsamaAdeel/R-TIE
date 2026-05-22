"""W137: Check 5 strict-validator replacement for the December literals.

Pre-W137 the two ``_W57_TEMPLATE_PHRASES`` entries for
``only runs when the reporting month is december`` and
``only runs in december`` validated via a substring lambda:

    ("EXTRACT(MONTH" in src.upper() or "TO_CHAR" in src.upper()) and "12" in src

``TO_CHAR`` appears in nearly every OFSAA function (generic skey-to-text
conversion in INSERTs) and the literal ``"12"`` appears in arithmetic
constants (``365/12``, ``* 12``), stage counters (``LV_STAGE := 12``),
account numbers, and debug literals. The lenient predicate returned
True-supported for ~the entire corpus.

Because W83a and W83b both dedup to Check 5 when a literal December
phrase is in the body, a lenient-True from Check 5 silently suppressed
all three checks. P1 query B4 ("What determines if an exposure gets
deducted from capital?") landed on the ABL_MARKET_RISK_EXPOSURES_FROM_
MRVAR anchor whose source contains ``TO_CHAR(n_acct_skey, ...)`` and
``LV_STAGE`` arithmetic literals but **no** ``MONTH = 12`` / ``EXTRACT
(MONTH ...) = 12`` / ``'DECEMBER'`` / ``YYYY1231`` evidence; the
response asserted December gating and badged VERIFIED.

W137 replaces the lenient lambda with a call to
:func:`_w57_calendar_gate_supports_claim` under the
``("december", "month", "December")`` claim_tag. The strict-month
branch accepts only direct MONTH=12 / EXTRACT(MONTH ...) = 12 /
``'DECEMBER'`` evidence — date literals are deliberately excluded
(per W83C's stakeholder-test-2 design at lines 1485-1487). Choice of
``month`` over ``year-end`` matches W83B's own December claim tag at
line 1279 and W83C's design intent.

Scope: ``_W57_TEMPLATE_PHRASES`` December lambdas only. ``no internal
gating`` (absence check), ``only runs march 2026`` (already strict
literal), and ``pass-through`` (helper) are untouched.

Diagnostic source: scratch/w83d_diagnostic.md (Cowork, P2-pre Step 1).
"""

import inspect

from src.agents.logic_explainer import (
    _W57_TEMPLATE_PHRASES,
    _w57_calendar_gate_supports_claim,
    _w57_check_template_phrases,
)


# ---------------------------------------------------------------------------
# Test fixture: B4-shape source.
#
# Models ABL_MARKET_RISK_EXPOSURES_FROM_MRVAR's pattern: TO_CHAR present
# (skey conversion), the literal "12" appears in arithmetic / stage
# counter contexts, but NO month-12 gating logic exists. Pre-W137 the
# lenient lambda returns True (TO_CHAR present + "12" present). Post-
# W137 the strict gate returns False.
# ---------------------------------------------------------------------------
_B4_SHAPE_SOURCE = (
    "INSERT INTO ABL_MARKET_RISK_EXPOSURES_F (n_acct_skey, n_market_value)\n"
    "SELECT TO_CHAR(n_acct_skey), interest_rate / 12 * months_outstanding\n"
    "FROM STG_MARKET_RISK_EXPOSURES m\n"
    "WHERE LV_STAGE = 12 AND m.f_is_active = 'Y'"
)


def _src_lines(text: str):
    """Build a multi_source source_code list from a multi-line text blob."""
    return [
        {"line": i + 1, "text": line}
        for i, line in enumerate(text.split("\n"))
    ]


# ===========================================================================
# Strict-gate verification (claim_tag direct calls)
# ===========================================================================

def test_w137_strict_gate_rejects_b4_shape_noisy_source():
    """The B4-shape source has TO_CHAR + literal "12" arithmetic noise
    but no MONTH=12 logic. _w57_calendar_gate_supports_claim under the
    ``("december", "month", "December")`` tag must return False."""
    assert _w57_calendar_gate_supports_claim(
        ("december", "month", "December"), _B4_SHAPE_SOURCE,
    ) is False


def test_w137_strict_gate_accepts_extract_month_equals_12():
    """Direct MONTH-12 evidence — strict gate must return True."""
    src = "WHERE EXTRACT(MONTH FROM d_calendar_date) = 12"
    assert _w57_calendar_gate_supports_claim(
        ("december", "month", "December"), src,
    ) is True


def test_w137_strict_gate_accepts_month_equals_12_bare():
    """Bare ``MONTH = 12`` predicate — strict gate must return True."""
    src = "WHERE MONTH = 12 AND fic_mis_date IS NOT NULL"
    assert _w57_calendar_gate_supports_claim(
        ("december", "month", "December"), src,
    ) is True


def test_w137_strict_gate_accepts_to_char_month_december_literal():
    """``TO_CHAR(..., 'MONTH') = 'DECEMBER'`` — strict gate accepts."""
    src = "WHERE TO_CHAR(d_dt, 'MONTH') = 'DECEMBER'"
    assert _w57_calendar_gate_supports_claim(
        ("december", "month", "December"), src,
    ) is True


def test_w137_strict_gate_rejects_year_end_date_literal_for_month_claim():
    """``D_CALENDAR_DATE = TO_DATE('20261231', ...)`` is year-end-shaped
    but the ``month`` claim type deliberately excludes date literals
    (per W83C's design intent: 'ONLY runs in <month>' + date-literal-
    only function MUST fire as fabricated). Strict gate returns False
    under the month claim. Confirms the W137 claim-tag choice
    diverges from W83a's lenient year-end conflation."""
    src = "WHERE D_CALENDAR_DATE = TO_DATE('20261231', 'YYYYMMDD')"
    assert _w57_calendar_gate_supports_claim(
        ("december", "month", "December"), src,
    ) is False


def test_w137_strict_gate_rejects_no_calendar_logic():
    """Source with no calendar gating at all — strict gate returns False."""
    src = "INSERT INTO FCT_X SELECT * FROM STG_X WHERE f_is_active = 'Y'"
    assert _w57_calendar_gate_supports_claim(
        ("december", "month", "December"), src,
    ) is False


# ===========================================================================
# End-to-end Check 5 integration
# ===========================================================================

def test_w137_check5_fires_on_b4_shape_short_phrase():
    """Post-W137 expected: response claims ``only runs in december``,
    target source has TO_CHAR + arithmetic ``12`` noise but no MONTH=12
    logic — Check 5 must emit GROUNDING-HIGH.

    Pre-W137 this test FAILS (lenient lambda returns True, no warning
    emitted). Post-W137 it passes."""
    md = "This function only runs in december to roll up year-end positions."
    multi = {"FN_FOO": {"source_code": _src_lines(_B4_SHAPE_SOURCE)}}
    warnings = _w57_check_template_phrases(md, multi)
    assert any(
        "only runs in december" in w and "FN_FOO" in w for w in warnings
    ), warnings


def test_w137_check5_fires_on_b4_shape_full_reporting_month_phrase():
    """Same noisy source, the longer literal phrase variant
    ``only runs when the reporting month is december``."""
    md = (
        "This function only runs when the reporting month is december, "
        "according to the conditional checks."
    )
    multi = {"FN_FOO": {"source_code": _src_lines(_B4_SHAPE_SOURCE)}}
    warnings = _w57_check_template_phrases(md, multi)
    assert any(
        "only runs when the reporting month is december" in w and "FN_FOO" in w
        for w in warnings
    ), warnings


def test_w137_check5_silent_when_b4_shape_phrase_absent():
    """Sanity: no December phrase in the body → no Check 5 warning,
    regardless of source content. Guards against accidentally widening
    the trigger surface."""
    md = "This function rolls up market-risk exposures for risk-weighted assets."
    multi = {"FN_FOO": {"source_code": _src_lines(_B4_SHAPE_SOURCE)}}
    warnings = _w57_check_template_phrases(md, multi)
    assert not any("december" in w.lower() for w in warnings), warnings


# ===========================================================================
# Regression guards: existing W57 Check 5 contract preserved
# ===========================================================================

def test_w137_regression_real_december_gate_no_warning():
    """A function with real ``EXTRACT(MONTH FROM dt) = 12`` correctly
    supports the December claim. Check 5 must NOT emit a warning.
    Preserves :func:`test_check5_pass_template_supported_by_source`."""
    md = "This function only runs in december when triggered."
    multi = {"FN_BAR": {"source_code": [
        {"line": 1, "text": "WHERE EXTRACT(MONTH FROM dt) = 12"},
    ]}}
    warnings = _w57_check_template_phrases(md, multi)
    assert warnings == [], warnings


def test_w137_regression_unsupported_no_calendar_still_fires():
    """A function with no calendar logic at all — Check 5 still emits
    its GROUNDING-HIGH warning. Preserves
    :func:`test_check5_fail_template_unsupported`."""
    md = "This function only runs in december."
    multi = {"FN_BAZ": {"source_code": [
        {"line": 1, "text": "INSERT INTO FCT_X SELECT * FROM STG_X"},
    ]}}
    warnings = _w57_check_template_phrases(md, multi)
    assert any("only runs in december" in w for w in warnings), warnings


def test_w137_regression_no_internal_gating_phrase_unchanged():
    """The third template entry (``no internal gating``) is a
    structural absence check, NOT a December predicate, and is OUT OF
    SCOPE for W137. Its existing positive-fire behavior must be
    preserved unchanged."""
    md = "There is no internal gating logic in this function."
    multi = {"FN_QUX": {"source_code": [
        {"line": 1, "text": "IF v_flag = 'Y' THEN INSERT INTO FCT_X END IF"},
    ]}}
    warnings = _w57_check_template_phrases(md, multi)
    assert any("no internal gating" in w for w in warnings), warnings


# ===========================================================================
# Static check: lambda source references the strict gate
# ===========================================================================

def test_w137_december_lambdas_call_strict_gate_not_substring():
    """Post-W137: both December phrase entries in
    ``_W57_TEMPLATE_PHRASES`` must reference
    ``_w57_calendar_gate_supports_claim`` in their lambda source. The
    pre-W137 substring shape (``"EXTRACT(MONTH" in src.upper()`` +
    ``"12" in src``) must not appear in either body — catches accidental
    regressions to the lenient predicate."""
    dec_lambda_1 = _W57_TEMPLATE_PHRASES[0][1]
    dec_lambda_2 = _W57_TEMPLATE_PHRASES[1][1]
    src_1 = inspect.getsource(dec_lambda_1)
    src_2 = inspect.getsource(dec_lambda_2)

    assert "_w57_calendar_gate_supports_claim" in src_1, src_1
    assert "_w57_calendar_gate_supports_claim" in src_2, src_2

    # Ensure both target the "december"/"month" claim tag — guards
    # against future drift back to the lenient year-end variant.
    assert '("december", "month", "December")' in src_1, src_1
    assert '("december", "month", "December")' in src_2, src_2

    # The pre-W137 substring fingerprint must be absent from both
    # bodies. Anchors on the unique-shaped predicate fragment.
    pre_w137_fingerprint = '"12" in src'
    assert pre_w137_fingerprint not in src_1, src_1
    assert pre_w137_fingerprint not in src_2, src_2


def test_w137_phrase_tuple_unchanged_size_and_phrases():
    """Scope guard: W137 touches ONLY the lambdas, never the phrase
    strings or the tuple shape. Catches accidental phrase drift /
    accidental addition or removal of entries."""
    phrases = [p for p, _ in _W57_TEMPLATE_PHRASES]
    assert phrases == [
        "only runs when the reporting month is december",
        "only runs in december",
        "no internal gating",
        "only runs march 2026",
        "pass-through",
    ], phrases
