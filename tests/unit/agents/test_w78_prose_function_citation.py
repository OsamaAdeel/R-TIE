"""W78: prose-framing function-citation tests.

W77 promoted logic_explainer to gpt-4o-mini, which cites functions in
prose (``The function `NAME` performs ...``) instead of the gpt-5-mini
``(NAME, Lines X-Y)`` parenthesised binding. The pre-W78 W57 Check 1.1
regex (``_W57_FUNC_CITATION_RE``) only matched the parenthesised form,
so a fabricated function name in prose framing slipped past with the
badge VERIFIED.

Each test below isolates a framing variation and asserts the W78 prose
extractor fires (or correctly stays silent) — anchored on the
asymmetric-design principle that false positives are not tolerable
(table tokens, alias literals must NOT trip the check) while false
negatives on edge-case framings are acceptable.
"""

from src.agents.logic_explainer import (
    _W57_PROSE_FUNCTION_REF_RE,
    _w57_check_per_claim_binding,
    _w57_passes_function_name_filters,
)


def _src(line_count, text="dummy"):
    return [{"line": i, "text": text} for i in range(1, line_count + 1)]


# ===========================================================================
# Regex shape: prose extraction
# ===========================================================================

def test_w78_regex_extracts_function_backtick_name():
    """gpt-4o-mini's CAP973 framing: ``The function `NAME` performs``."""
    body = (
        "The function `REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP` "
        "performs an insert operation."
    )
    matches = list(_W57_PROSE_FUNCTION_REF_RE.finditer(body))
    assert len(matches) == 1
    cand = matches[0].group(1) or matches[0].group(2)
    assert cand == "REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP"


def test_w78_regex_extracts_function_no_backticks():
    """Plain ``the function NAME`` — no backticks, valid identifier."""
    body = "The function FN_LOAD_OPS_RISK_DATA loads operational risk data."
    matches = list(_W57_PROSE_FUNCTION_REF_RE.finditer(body))
    assert len(matches) == 1
    cand = matches[0].group(1) or matches[0].group(2)
    assert cand == "FN_LOAD_OPS_RISK_DATA"


def test_w78_regex_extracts_function_called_qualifier():
    """``The function called NAME ...`` — keyword + qualifier."""
    body = "The function called REGULATORY_FOO_DATA_POP runs in December."
    matches = list(_W57_PROSE_FUNCTION_REF_RE.finditer(body))
    assert len(matches) == 1
    cand = matches[0].group(1) or matches[0].group(2)
    assert cand == "REGULATORY_FOO_DATA_POP"


def test_w78_regex_extracts_name_then_function():
    """``the `NAME` function ...`` — keyword last."""
    body = "the `FN_LOAD_OPS_RISK_DATA` function is responsible for loading."
    matches = list(_W57_PROSE_FUNCTION_REF_RE.finditer(body))
    assert len(matches) == 1
    cand = matches[0].group(1) or matches[0].group(2)
    assert cand == "FN_LOAD_OPS_RISK_DATA"


def test_w78_regex_extracts_procedure_keyword():
    """``procedure NAME`` mirrors the ``function`` framing."""
    body = "The procedure FN_HELPER_LOAD does the heavy lifting."
    matches = list(_W57_PROSE_FUNCTION_REF_RE.finditer(body))
    assert len(matches) == 1
    cand = matches[0].group(1) or matches[0].group(2)
    assert cand == "FN_HELPER_LOAD"


def test_w78_regex_skips_table_keyword_framing():
    """``the `FCT_*` table`` MUST NOT match — only function/procedure framings.

    This is the asymmetric-design guard: backtick + uppercase is not
    enough; the surrounding word must be ``function`` or ``procedure``.
    """
    body = (
        "the data is inserted into the `ABL_OPS_RISK_DATA` table "
        "from the staging table `STG_OPS_RISK_DATA`."
    )
    matches = list(_W57_PROSE_FUNCTION_REF_RE.finditer(body))
    assert matches == []


# ===========================================================================
# W58 filter integration
# ===========================================================================

def test_w78_filters_exclude_table_prefix():
    assert not _w57_passes_function_name_filters("FCT_OPS_RISK_DATA")
    assert not _w57_passes_function_name_filters("DIM_DATES")
    assert not _w57_passes_function_name_filters("STG_PRODUCT_PROCESSOR")
    assert not _w57_passes_function_name_filters("FSI_PHASE_IN_TREATMENT")


def test_w78_filters_exclude_alias_literals():
    assert not _w57_passes_function_name_filters("EXP_11")
    assert not _w57_passes_function_name_filters("COND_45")
    assert not _w57_passes_function_name_filters("T_5")
    assert not _w57_passes_function_name_filters("SS_FOO_BAR")
    assert not _w57_passes_function_name_filters("TT_FOO_BAR")


def test_w78_filters_exclude_column_prefix():
    assert not _w57_passes_function_name_filters("N_EOP_BAL")
    assert not _w57_passes_function_name_filters("V_LV_CODE")
    assert not _w57_passes_function_name_filters("F_REGULATORY_ENTITY_IND")


def test_w78_filters_keep_real_function_names():
    assert _w57_passes_function_name_filters("FN_LOAD_OPS_RISK_DATA")
    assert _w57_passes_function_name_filters(
        "REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP"
    )
    assert _w57_passes_function_name_filters(
        "REG_ADJUSTMENT_RWA_STD_ACCT_HEAD_DATA_POP"
    )
    assert _w57_passes_function_name_filters("CAPITAL_STD_ACCT_HEAD_POP")


# ===========================================================================
# End-to-end: _w57_check_per_claim_binding with W78 prose extractor
# ===========================================================================

# The captured CAP973 gpt-4o-mini body excerpt — same fabrication pattern
# as scratch/w77p4/c_cap973.txt. functions_analyzed contains different
# DATA_POP variants; the cited REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_
# DATA_POP is NOT among them.
_CAP973_BODY = (
    "## How is CAP973 calculated?\n\n"
    "### This entire function ONLY runs when the reporting month is December.\n\n"
    "### Step 1: Initial Insert (Lines 203-223)\n"
    "The function `REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP` "
    "performs an insert operation into the `FCT_STANDARD_ACCT_HEAD` table, "
    "which is crucial for calculating CAP973.\n\n"
    "The values originate from the `FSI_PHASE_IN_TREATMENT`, `DIM_RUN`, "
    "and `DIM_DATES` tables.\n"
)

_CAP973_ANALYZED = [
    "REGULATORY_INVST_DEDUCTION_STD_ACCT_HEAD_DATA_POP",
    "THRESHOLD_DEDUCTION_STD_ACCT_HEAD_DATA_POP",
    "REG_ADJUSTMENT_RWA_STD_ACCT_HEAD_DATA_POP",
    "STD_ACCT_HEAD_THRESHOLD_TREATMENT_DATA_POP",
    "INTERNAL_TRANSACTIONS_STANDARD_ACCT_HEAD_DATA_POP",
]


def test_w78_cap973_repro_fires_grounding_high():
    """The W78 design proof: same fabrication pattern that pre-W77
    fired GROUNDING-HIGH on must fire again post-W78 on gpt-4o-mini's
    framing (where the function name appears in prose, not in
    parenthesised line bindings)."""
    multi = {fn: {"source_code": _src(500)} for fn in _CAP973_ANALYZED}
    warnings = _w57_check_per_claim_binding(_CAP973_BODY, multi, _CAP973_ANALYZED)
    assert any(
        w.startswith("GROUNDING-HIGH:")
        and "REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP" in w
        and "not in retrieved sources" in w
        for w in warnings
    ), f"expected HIGH on cited fabrication; got: {warnings}"


def test_w78_gpt5mini_parenthesised_form_still_fires():
    """gpt-5-mini regression: pre-W78 parenthesised binding form still
    catches a fabricated cited function name."""
    md = "Step 1 (FN_NOT_RETRIEVED, Lines 5-10) is the key claim."
    multi = {"FN_FOO": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(md, multi, ["FN_FOO"])
    assert any(
        w.startswith("GROUNDING-HIGH:")
        and "FN_NOT_RETRIEVED" in w
        and "not in retrieved sources" in w
        for w in warnings
    )


def test_w78_legitimate_match_no_warning():
    """Negative — body cites a function that IS in functions_analyzed.
    Asymmetric design: must not fire."""
    md = (
        "The function `FN_LOAD_OPS_RISK_DATA` loads operational risk data "
        "from the staging table."
    )
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(500)}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_LOAD_OPS_RISK_DATA"]
    )
    assert not any(
        "FN_LOAD_OPS_RISK_DATA" in w and "not in retrieved sources" in w
        for w in warnings
    ), f"phantom self-citation warning: {warnings}"


def test_w78_alias_literal_in_body_no_warning():
    """Negative — body mentions ``EXP_11`` (W58.b alias literal pattern).
    The W58 filter must exclude this from prose extraction."""
    md = (
        "The function `EXP_11` performs an aggregation. "
        "The function `COND_45` checks a condition."
    )
    multi = {"FN_FOO": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(md, multi, ["FN_FOO"])
    assert not any("EXP_11" in w for w in warnings), warnings
    assert not any("COND_45" in w for w in warnings), warnings


def test_w78_table_prefix_in_body_no_warning():
    """Negative — body mentions ``FCT_OPS_RISK_DATA`` framed as a function
    (theoretically — gpt-4o-mini doesn't do this, but if it did, the
    W58.a filter must exclude it). Asymmetric-design guard against
    over-firing on the table-mislabelled-as-function pattern."""
    md = (
        "The function `FCT_OPS_RISK_DATA` is mentioned. "
        "The function `STG_PRODUCT_PROCESSOR` is also referenced."
    )
    multi = {"FN_FOO": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(md, multi, ["FN_FOO"])
    assert not any("FCT_OPS_RISK_DATA" in w for w in warnings), warnings
    assert not any("STG_PRODUCT_PROCESSOR" in w for w in warnings), warnings


def test_w78_cap973_body_does_not_flag_table_tokens():
    """Stronger CAP973 negative: the body mentions FCT_*, DIM_*, FSI_*,
    N_* tokens — none of these should appear in the W78 warning set
    even though some are not in functions_analyzed."""
    multi = {fn: {"source_code": _src(500)} for fn in _CAP973_ANALYZED}
    warnings = _w57_check_per_claim_binding(_CAP973_BODY, multi, _CAP973_ANALYZED)
    # Only one HIGH warning expected: the fabricated cited function.
    high_warnings = [w for w in warnings if w.startswith("GROUNDING-HIGH:")]
    fabricated = [
        w for w in high_warnings
        if "REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP" in w
    ]
    other_high = [
        w for w in high_warnings
        if "REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP" not in w
    ]
    assert len(fabricated) == 1, f"expected 1 fabrication HIGH; got: {fabricated}"
    assert other_high == [], (
        f"unexpected additional HIGH warnings (table-mislabel false "
        f"positives?): {other_high}"
    )


def test_w78_dedup_collapses_multiple_prose_mentions():
    """If gpt-4o-mini cites the same fabricated function twice in prose,
    the per-prose-pass dedup keeps the warning set to one entry per
    distinct fabrication name."""
    md = (
        "The function `FN_FAKE_FOO` performs an insert. "
        "Later, the `FN_FAKE_FOO` function also runs."
    )
    multi = {"FN_REAL": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(md, multi, ["FN_REAL"])
    fake_warnings = [w for w in warnings if "FN_FAKE_FOO" in w]
    assert len(fake_warnings) == 1, (
        f"expected exactly 1 FN_FAKE_FOO warning after dedup; "
        f"got {len(fake_warnings)}: {fake_warnings}"
    )
