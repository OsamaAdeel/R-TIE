"""W78a: heading + responsibility framing citation tests.

W78 (merge 09dcdc3) added ``_W57_PROSE_FUNCTION_REF_RE`` to catch
gpt-4o-mini's ``the function `NAME` performs`` framing. W70's canary
(c) on CAP973 (post-W70 merge d106d7e) surfaced two framings W78
doesn't catch:

  - markdown heading: ``## CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT``
  - responsibility: ``NAME is responsible for ...`` /
    ``NAME has the responsibility of ...``

Pre-W78a these slipped past W57 and badged VERIFIED on a body anchored
on a function not in retrieval. W78a's companion regex
``_W57_HEADING_AND_RESPONSIBILITY_REF_RE`` closes both gaps with a
shared dedup set so the same fabrication cited via multiple framings
fires exactly one warning.

Asymmetric design (same as W68/W78/W70): false positives — extracting
table prefixes / alias literals from headings or responsibility phrases
— are NOT tolerable; false negatives on edge framings (e.g.,
``tasked with``, ``the role of NAME``) are tolerable.
"""

from src.agents.logic_explainer import (
    _W57_HEADING_AND_RESPONSIBILITY_REF_RE,
    _w57_check_per_claim_binding,
)


def _src(line_count, text="dummy"):
    return [{"line": i, "text": text} for i in range(1, line_count + 1)]


# ===========================================================================
# Pattern A: markdown heading extraction
# ===========================================================================

def test_w78a_heading_simple_h2():
    """``## CS_FOO_BAR`` -> extracts CS_FOO_BAR."""
    body = "## CS_FOO_BAR\n\nbody text follows"
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("heading_name") for m in matches if m.group("heading_name")]
    assert "CS_FOO_BAR" in extracted, extracted


def test_w78a_heading_with_intervening_word():
    """``### Function FN_LOAD_OPS_RISK_DATA`` -> extracts FN_LOAD_OPS_RISK_DATA.

    Optional non-name words (here, ``Function``) between the ``#`` markers
    and the identifier are skipped via the non-greedy ``(?:\\S+[ \\t]+)*?``.
    """
    body = "### Function FN_LOAD_OPS_RISK_DATA"
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("heading_name") for m in matches if m.group("heading_name")]
    assert "FN_LOAD_OPS_RISK_DATA" in extracted, extracted


def test_w78a_heading_h1():
    """``# CS_GOODWILL_CALCULATION`` (single ``#``)."""
    body = "# CS_GOODWILL_CALCULATION\n\nthis is the body"
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("heading_name") for m in matches if m.group("heading_name")]
    assert "CS_GOODWILL_CALCULATION" in extracted, extracted


def test_w78a_heading_first_token_only():
    """``## CS_FOO — The Goodwill Calculator`` -> CS_FOO only.

    The ``Goodwill`` and ``Calculator`` words (no underscores) are
    irrelevant; ``CS_FOO`` is the first underscore-bearing token.
    """
    body = "## CS_FOO — The Goodwill Calculator"
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("heading_name") for m in matches if m.group("heading_name")]
    assert extracted == ["CS_FOO"], extracted


def test_w78a_heading_after_double_newline():
    """Heading after ``\\n\\n`` (the typical CAP973 spacing) still anchors."""
    body = "Some intro paragraph.\n\n## CS_REGULATORY_ADJUSTMENTS_DATA_POP\n\nbody"
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("heading_name") for m in matches if m.group("heading_name")]
    assert "CS_REGULATORY_ADJUSTMENTS_DATA_POP" in extracted, extracted


def test_w78a_heading_inline_hash_not_matched():
    """``see issue #123`` mid-prose -> no match (no line-start anchor)."""
    body = "Check the build, see issue #123 for details on FN_FOO_BAR."
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("heading_name") for m in matches if m.group("heading_name")]
    assert extracted == [], extracted


def test_w78a_heading_no_space_after_hashes_not_matched():
    """``##NAME`` (no space) is not a markdown heading; not matched."""
    body = "##CS_NO_SPACE_HERE"
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("heading_name") for m in matches if m.group("heading_name")]
    assert extracted == [], extracted


def test_w78a_heading_digit_only_not_matched():
    """``# 123 issue`` -> no match (digits don't start an identifier; ``issue``
    has no underscore)."""
    body = "# 123 issue"
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("heading_name") for m in matches if m.group("heading_name")]
    assert extracted == [], extracted


# ===========================================================================
# Pattern B: responsibility framing extraction
# ===========================================================================

def test_w78a_responsibility_is_responsible_for():
    """``CS_FOO is responsible for calculating ...``."""
    body = "Earlier we discussed CS_FOO is responsible for calculating CAP973."
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("resp_name") for m in matches if m.group("resp_name")]
    assert "CS_FOO" in extracted, extracted


def test_w78a_responsibility_with_backticks():
    """```NAME` is responsible for...`` -> backtick framing absorbed."""
    body = "`FN_LOAD_OPS_RISK_DATA` is responsible for loading ops risk data."
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("resp_name") for m in matches if m.group("resp_name")]
    assert "FN_LOAD_OPS_RISK_DATA" in extracted, extracted


def test_w78a_responsibility_has_the_responsibility_of():
    """``CS_BAR has the responsibility of ...``."""
    body = "Then CS_BAR has the responsibility of dispatching the result."
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("resp_name") for m in matches if m.group("resp_name")]
    assert "CS_BAR" in extracted, extracted


def test_w78a_responsibility_no_name_not_matched():
    """``The function is responsible for X.`` -> no extraction.

    This is the actual second mention in the CAP973 body. Since
    ``function`` lacks an underscore, the identifier shape constraint
    fails and Pattern B doesn't match. (Pattern A's heading catch is
    sufficient for the CAP973 fabrication; dedup keeps it to one
    warning.)
    """
    body = "The function is responsible for calculating the deduction amount."
    matches = list(_W57_HEADING_AND_RESPONSIBILITY_REF_RE.finditer(body))
    extracted = [m.group("resp_name") for m in matches if m.group("resp_name")]
    assert extracted == [], extracted


# ===========================================================================
# W58 exclusion gate integration
# ===========================================================================

def test_w78a_table_prefix_in_heading_filtered():
    """``## STG_OPS_RISK_DATA staging table`` -> regex extracts the token,
    but the ``_w57_passes_function_name_filters`` (W58.a table-prefix gate)
    rejects it before any warning is fired.

    This is the asymmetric-design false-positive guard — a heading that
    happens to start with a table-prefix-shaped name must not produce a
    GROUNDING-HIGH warning.
    """
    body = "## STG_OPS_RISK_DATA staging table\n\nbody text"
    multi = {"FN_FOO": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(body, multi, ["FN_FOO"])
    assert not any("STG_OPS_RISK_DATA" in w for w in warnings), warnings


def test_w78a_alias_literal_in_responsibility_filtered():
    """``EXP_11 is responsible for ...`` -> regex extracts EXP_11, but the
    W58.b alias-literal gate rejects it. No warning."""
    body = "EXP_11 is responsible for the aggregation step."
    multi = {"FN_FOO": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(body, multi, ["FN_FOO"])
    assert not any("EXP_11" in w for w in warnings), warnings


# ===========================================================================
# End-to-end: _w57_check_per_claim_binding integration
# ===========================================================================

def test_w78a_heading_in_retrieval_no_warning():
    """Heading cites a function that IS in functions_analyzed -> no warning.

    Asymmetric design: must not fire phantom warnings when the cited
    function is legitimately retrieved.
    """
    body = (
        "## FN_LOAD_OPS_RISK_DATA\n\n"
        "Some explanation. FN_LOAD_OPS_RISK_DATA is responsible for "
        "loading data."
    )
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(500)}}
    warnings = _w57_check_per_claim_binding(
        body, multi, ["FN_LOAD_OPS_RISK_DATA"]
    )
    assert not any(
        "FN_LOAD_OPS_RISK_DATA" in w and "not in retrieved sources" in w
        for w in warnings
    ), f"phantom self-citation warning: {warnings}"


def test_w78a_heading_not_in_retrieval_fires():
    """Heading cites a function NOT in functions_analyzed -> GROUNDING-HIGH."""
    body = "## CS_FAKE_HEADING_FUNCTION\n\nbody"
    multi = {"FN_REAL": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(body, multi, ["FN_REAL"])
    assert any(
        w.startswith("GROUNDING-HIGH:")
        and "CS_FAKE_HEADING_FUNCTION" in w
        and "not in retrieved sources" in w
        for w in warnings
    ), f"expected HIGH on heading fabrication; got: {warnings}"


def test_w78a_responsibility_not_in_retrieval_fires():
    """Responsibility-framed cite of out-of-retrieval function -> HIGH."""
    body = "Then CS_FAKE_RESP_FUNCTION is responsible for the next step."
    multi = {"FN_REAL": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(body, multi, ["FN_REAL"])
    assert any(
        w.startswith("GROUNDING-HIGH:")
        and "CS_FAKE_RESP_FUNCTION" in w
        and "not in retrieved sources" in w
        for w in warnings
    ), f"expected HIGH on responsibility fabrication; got: {warnings}"


def test_w78a_dedup_heading_and_responsibility_one_warning():
    """Same fabricated function cited in BOTH heading AND responsibility
    framing -> exactly ONE warning (shared seen_cited_fn dedup)."""
    body = (
        "## CS_FAKE_FOO_BAR\n\n"
        "This is the body. CS_FAKE_FOO_BAR is responsible for foo."
    )
    multi = {"FN_REAL": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(body, multi, ["FN_REAL"])
    fake_warnings = [
        w for w in warnings
        if "CS_FAKE_FOO_BAR" in w and "not in retrieved sources" in w
    ]
    assert len(fake_warnings) == 1, (
        f"expected 1 warning after dedup across heading + responsibility; "
        f"got {len(fake_warnings)}: {fake_warnings}"
    )


def test_w78a_dedup_with_w78_prose_one_warning():
    """Same fabrication cited via W78 prose AND W78a heading -> ONE warning.

    The shared ``seen_cited_fn`` set spans both the W78 prose pass and
    the W78a heading + responsibility pass.
    """
    body = (
        "## CS_FAKE_FN\n\n"
        "Body text. The function `CS_FAKE_FN` performs an insert."
    )
    multi = {"FN_REAL": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(body, multi, ["FN_REAL"])
    fake_warnings = [
        w for w in warnings
        if "CS_FAKE_FN" in w and "not in retrieved sources" in w
    ]
    assert len(fake_warnings) == 1, (
        f"expected 1 warning after cross-pass dedup; "
        f"got {len(fake_warnings)}: {fake_warnings}"
    )


def test_w78a_w78_prose_still_fires():
    """Regression: W78a's addition does NOT disable W78's prose pass.

    A pure prose-framed fabrication (no heading, no responsibility) must
    still fire on the W78 regex.
    """
    body = "The function `CS_PROSE_ONLY_FN` performs an insert into FCT_FOO."
    multi = {"FN_REAL": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(body, multi, ["FN_REAL"])
    assert any(
        "CS_PROSE_ONLY_FN" in w and "not in retrieved sources" in w
        for w in warnings
    ), f"W78 prose pass regressed: {warnings}"


# ===========================================================================
# CAP973 reproduction (the design proof)
# ===========================================================================

# Captured CAP973 body excerpt from scratch/w70_canary_c.txt (post-W70
# merge d106d7e). Body's first mention of the cited function is a `##`
# heading; second mention is "The function is responsible for..." (no
# preceding name, so Pattern B correctly does not match this exact
# phrase — Pattern A catches the heading, dedup keeps it to one warning).
_CAP973_BODY = (
    "This function runs in ABL_CAR_CSTM_V4 → "
    "ABL_CAPITAL_STRUCTURE_DATA_PROCESSING → "
    "ABL_SIGNIFICANT_INVESTMENT_IN_ENTITIES_OUTSIDE_REG_CONSOLIDATION_PROCESSING "
    "(task #6).\n\n"
    "## CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT\n\n"
    "This entire function ONLY runs when the reporting month is December. "
    "The function is responsible for calculating the deduction amount for "
    "regulatory adjustments, specifically for the business identifier CAP973.\n\n"
    "### Step 1: Initial Insert (Lines 203-223)\n\n"
    "INSERT INTO FCT_STANDARD_ACCT_HEAD ...\n"
)

# functions_analyzed from the actual CAP973 meta event — note the cited
# function CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT is NOT
# present (the closest member is CS_REGULATORY_INVESTMENTS_PHASE_IN_
# DEDUCTION_AMOUNT — different word in position 3).
_CAP973_ANALYZED = [
    "CS_PHASE_IN_TREATMENT_SIGNIFICANT_INVST_DEDUCTION_AMOUNT_ASSIGNMENT",
    "CS_PHASE_IN_TREATMENT_INDIVIDUAL_THRESHOLD_DEDUCTION_AMOUNT_ASSIGNMENT",
    "CS_PHASE_IN_DEDUCTION_AMOUNT",
    "CS_REGULATORY_INVESTMENTS_PHASE_IN_DEDUCTION_AMOUNT",
    "THRESHOLD_DEDUCTION_STD_ACCT_HEAD_DATA_POP",
    "TLX_PROV_AMT_FOR_CAP013",
    "POPULATE_STDACC_FROMGL",
    "FN_LOAD_OPS_RISK_DATA",
    "TLX_OPS_ADJ_MISDATE",
    "POPULATE_PP_FROMGL",
]


def test_w78a_cap973_repro_fires_grounding_high():
    """The W78a design proof: CAP973's heading-anchored fabrication must
    fire GROUNDING-HIGH cited-function-not-in-retrieval after W78a."""
    multi = {fn: {"source_code": _src(500)} for fn in _CAP973_ANALYZED}
    warnings = _w57_check_per_claim_binding(_CAP973_BODY, multi, _CAP973_ANALYZED)
    assert any(
        w.startswith("GROUNDING-HIGH:")
        and "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT" in w
        and "not in retrieved sources" in w
        for w in warnings
    ), f"expected HIGH on heading fabrication; got: {warnings}"


def test_w78a_cap973_repro_single_warning_only():
    """CAP973 mentions the fabrication in the heading AND in a "is
    responsible" phrase (with no preceding name — Pattern B doesn't
    match). Even so, dedup must keep the warning count at 1 across all
    framings, and no other HIGH warnings should fire on table tokens
    in the body (FCT_*, DIM_*, FSI_*) or on legitimate analyzed
    functions."""
    multi = {fn: {"source_code": _src(500)} for fn in _CAP973_ANALYZED}
    warnings = _w57_check_per_claim_binding(_CAP973_BODY, multi, _CAP973_ANALYZED)
    high = [w for w in warnings if w.startswith("GROUNDING-HIGH:")]
    fabricated = [
        w for w in high
        if "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT" in w
    ]
    other_high = [
        w for w in high
        if "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT" not in w
    ]
    assert len(fabricated) == 1, (
        f"expected exactly 1 fabrication HIGH (dedup); got: {fabricated}"
    )
    assert other_high == [], (
        f"unexpected additional HIGH warnings (table-mislabel false "
        f"positives?): {other_high}"
    )
