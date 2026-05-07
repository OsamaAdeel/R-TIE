"""W57: post-generation grounding-enforcement tests.

Six independent checks, each with (passing case, failing case, edge case)
plus end-to-end fixtures driven by Run 3 benchmark fabrications:
  - A4 / FN_LOAD_OPS_RISK_DATA — 172 line citations all on one range
  - C1 / TLX_OPS_ADJ_MISDATE — 296 citations, asked-about mismatch
  - D1 — self-aware caveat present, badge currently VERIFIED
  - A4 hierarchy/batch mismatch — banner says one batch, body cites another
  - D2/D3 chain coherence — N functions presented as steps with no shared tables
  - A1 clean — no warnings at all
"""

from src.agents.logic_explainer import (
    evaluate_grounding,
    w57_enforce_grounding,
    _W57_FUNC_CITATION_RE,
    _w57_check_per_claim_binding,
    _w57_check_citation_count_cap,
    _w57_check_anchoring,
    _w57_check_chain_coherence,
    _w57_check_hierarchy_body_consistency,
    _w57_check_template_phrases,
    _w57_check_caveat_vs_badge,
)


def _src(line_count, text="dummy"):
    """Build a multi_source source_code list of *line_count* line dicts."""
    return [{"line": i, "text": text} for i in range(1, line_count + 1)]


# ===========================================================================
# Check 1: per-claim citation binding
# ===========================================================================

def test_check1_pass_single_function_clean():
    md = "Step 1: it does X (Lines 5-10). Step 2: Y (Lines 15-20)."
    multi = {"FN_FOO": {"source_code": _src(100)}}
    assert _w57_check_per_claim_binding(md, multi, ["FN_FOO"]) == []


def test_check1_fail_unknown_function_in_explicit_citation():
    md = "(FN_NOT_EXIST, Lines 5-10) is what does it."
    multi = {"FN_FOO": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(md, multi, ["FN_FOO"])
    assert any("not in retrieved sources" in w for w in warnings)


def test_check1_fail_range_exceeds_source_length():
    md = "(FN_FOO, Lines 5-500) wide range."
    multi = {"FN_FOO": {"source_code": _src(50)}}
    warnings = _w57_check_per_claim_binding(md, multi, ["FN_FOO"])
    assert any("exceeds source length" in w for w in warnings)


def test_check1_fail_a4_172_repeats_one_range():
    """Run 3 benchmark A4: 172 citations to Lines 198-369 in one response."""
    md = " ".join(f"Step {i} (Lines 198-369)." for i in range(1, 173))
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(500)}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_LOAD_OPS_RISK_DATA"]
    )
    assert any("Lines 198-369 cited 172 times" in w for w in warnings)


def test_check1_edge_three_repeats_passes():
    """Threshold is >3, so exactly 3 repeats must NOT fire."""
    md = "(Lines 5-10). (Lines 5-10). (Lines 5-10)."
    multi = {"FN_FOO": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(md, multi, ["FN_FOO"])
    assert all("cited 3 times" not in w for w in warnings)


def test_check1_edge_four_repeats_fires():
    md = "(Lines 5-10). (Lines 5-10). (Lines 5-10). (Lines 5-10)."
    multi = {"FN_FOO": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(md, multi, ["FN_FOO"])
    assert any("cited 4 times" in w for w in warnings)


# ===========================================================================
# Check 2: total citation count cap
# ===========================================================================

def test_check2_pass_30_citations():
    md = " ".join(f"(Lines {10*i}-{10*i+5})." for i in range(1, 31))
    assert _w57_check_citation_count_cap(md) == []


def test_check2_pass_at_cap():
    md = " ".join(f"(Lines {10*i}-{10*i+5})." for i in range(1, 51))
    # 50 citations exactly at the cap; cap is exclusive.
    assert _w57_check_citation_count_cap(md) == []


def test_check2_fail_above_cap():
    md = " ".join(f"(Lines {10*i}-{10*i+5})." for i in range(1, 60))
    warnings = _w57_check_citation_count_cap(md)
    assert any("59 line citations" in w for w in warnings)


def test_check2_fail_c1_296_citations():
    """Run 3 benchmark C1: 296 citations to TLX_OPS_ADJ_MISDATE range."""
    md = " ".join(
        f"(TLX_OPS_ADJ_MISDATE, Lines 1-50)." for _ in range(296)
    )
    warnings = _w57_check_citation_count_cap(md)
    assert any("296 line citations" in w for w in warnings)


# ===========================================================================
# Check 3a: anchoring
# ===========================================================================

def test_check3a_pass_no_named_function():
    """User asked a generic question, no specific function named."""
    assert _w57_check_anchoring(
        "What is risk weighting?", ["FN_FOO", "FN_BAR"], "Some prose."
    ) == []


def test_check3a_pass_single_function_response():
    """User asked about FN_FOO, response is single-function — anchored
    by construction."""
    assert _w57_check_anchoring(
        "How does FN_LOAD_OPS_RISK_DATA work?",
        ["FN_LOAD_OPS_RISK_DATA"],
        "Step 1 (Lines 5-10).",
    ) == []


def test_check3a_fail_dominant_other_function():
    """Run 3 benchmark C2: user asked about CSTM, response talks
    primarily about a different function."""
    md = " ".join(["CAP_CONSL_EFFECTIVE"] * 100) + " OPS_RISK_DATA_POPULATION_CSTM"
    warnings = _w57_check_anchoring(
        "How does OPS_RISK_DATA_POPULATION_CSTM work?",
        ["OPS_RISK_DATA_POPULATION_CSTM", "CAP_CONSL_EFFECTIVE"],
        md,
    )
    assert any("primarily cites" in w for w in warnings)


def test_check3a_pass_asked_function_dominates():
    """Asked function appears most often — anchoring satisfied."""
    md = "FN_FOO does X. FN_FOO does Y. FN_FOO does Z. FN_BAR mentioned once."
    assert _w57_check_anchoring(
        "How does FN_FOO work?",
        ["FN_FOO", "FN_BAR"],
        md,
    ) == []


def test_check3a_pass_balanced_multi_function():
    """When the asked-about and other functions are mentioned at parity,
    anchoring is satisfied."""
    md = "FN_FOO (Lines 1-5). FN_BAR (Lines 10-15)."
    assert _w57_check_anchoring(
        "How does FN_FOO interact with FN_BAR?",
        ["FN_FOO", "FN_BAR"],
        md,
    ) == []


# ===========================================================================
# Check 3b: chain coherence
# ===========================================================================

def test_check3b_pass_no_step_headers():
    """No "## Step N: X" pattern — not making a chain claim."""
    md = "Some prose without step headers."
    assert _w57_check_chain_coherence(md, {}) == []


def test_check3b_pass_single_step():
    """One step alone is not a chain."""
    md = "## Step 1: FN_FOO\n\nIt does X."
    assert _w57_check_chain_coherence(md, {}) == []


def test_check3b_fail_no_shared_tables():
    """Two functions presented as steps but their sources share no
    table references."""
    md = (
        "## Step 1: FN_FOOBAR\n\nDoes things.\n\n"
        "## Step 2: FN_BARBAZ\n\nDoes other things."
    )
    multi = {
        "FN_FOOBAR": {"source_code": [
            {"line": 1, "text": "INSERT INTO FCT_X"}
        ]},
        "FN_BARBAZ": {"source_code": [
            {"line": 1, "text": "INSERT INTO FCT_Y"}
        ]},
    }
    warnings = _w57_check_chain_coherence(md, multi)
    assert any("share no table references" in w for w in warnings)


def test_check3b_pass_shared_table():
    """Two functions share a table — chain link is supported."""
    md = (
        "## Step 1: FN_FOOBAR\n\n## Step 2: FN_BARBAZ\n\n"
    )
    multi = {
        "FN_FOOBAR": {"source_code": [
            {"line": 1, "text": "INSERT INTO FCT_SHARED"}
        ]},
        "FN_BARBAZ": {"source_code": [
            {"line": 1, "text": "SELECT FROM FCT_SHARED"}
        ]},
    }
    assert _w57_check_chain_coherence(md, multi) == []


def test_check3b_skips_internal_alias_step_labels():
    """Regression: chain regex used to capture CASE labels like 'EXP_'
    and SQL keywords like 'MERGE' from generated step headers and then
    falsely flag them as 'not in retrieved sources'. Real-world hit:
    C09 in the post-W57 canary run. The check must filter such tokens
    out before declaring a chain claim exists."""
    md = (
        "## Step 1: EXP_\n\nDoes things.\n\n"
        "## Step 2: MERGE\n\nDoes other things."
    )
    multi = {"FN_FOOBAR": {"source_code": [{"line": 1, "text": "x"}]}}
    # Both step labels are filtered (EXP_ matches internal-alias, MERGE
    # is < 6 chars and is in the not-real keyword list); after filtering,
    # fewer than 2 step functions remain and no warning fires.
    assert _w57_check_chain_coherence(md, multi) == []


# ===========================================================================
# Check 4: hierarchy/body consistency
# ===========================================================================

def test_check4_pass_no_redis():
    """Check 4 falls open when redis_client is None."""
    md = "This function runs in BATCH_X → PROC → SUB"
    assert _w57_check_hierarchy_body_consistency(md, {}, None) == []


def test_check4_pass_no_banner():
    """No hierarchy banner present — nothing to check."""
    assert _w57_check_hierarchy_body_consistency("Some prose.", {}, None) == []


def test_check4_pass_with_mocked_redis_matching_batch():
    """When redis lookup returns matching batch for a cited function."""

    class _MockRedis:
        def scan(self, cursor=0, match=None, count=200):
            return 0, [b"graph:OFSERM:FN_FOO"]

    md = "This function runs in BATCH_X → PROC → SUB"
    multi = {"FN_FOO": {"source_code": [{"line": 1, "text": "x"}]}}

    import src.parsing.store as store_mod
    import src.parsing.schema_discovery as sd_mod

    orig_get = store_mod.get_function_graph
    orig_disc = sd_mod.discovered_schemas
    try:
        store_mod.get_function_graph = lambda r, s, fn: {
            "hierarchy": {"batch": "BATCH_X", "process": "PROC"}
        }
        sd_mod.discovered_schemas = lambda r: ["OFSERM"]
        # Force re-import path through evaluate_grounding's lazy import
        warnings = _w57_check_hierarchy_body_consistency(
            md, multi, _MockRedis()
        )
    finally:
        store_mod.get_function_graph = orig_get
        sd_mod.discovered_schemas = orig_disc

    assert warnings == []


def test_check4_fail_with_mocked_redis_mismatched_batch():
    """A4 case: hierarchy banner names one batch but cited function
    belongs to a different batch."""
    class _MockRedis:
        def scan(self, cursor=0, match=None, count=200):
            return 0, [b"graph:OFSERM:FN_FOO"]

    md = "This function runs in OFSDMINFO_ABL_DATA_PREPARATION → PROC → SUB"
    multi = {"FN_FOO": {"source_code": [{"line": 1, "text": "x"}]}}

    import src.parsing.store as store_mod
    import src.parsing.schema_discovery as sd_mod

    orig_get = store_mod.get_function_graph
    orig_disc = sd_mod.discovered_schemas
    try:
        store_mod.get_function_graph = lambda r, s, fn: {
            "hierarchy": {"batch": "ABL_CAR_CSTM_V4", "process": "PROC"}
        }
        sd_mod.discovered_schemas = lambda r: ["OFSERM"]
        warnings = _w57_check_hierarchy_body_consistency(
            md, multi, _MockRedis()
        )
    finally:
        store_mod.get_function_graph = orig_get
        sd_mod.discovered_schemas = orig_disc

    assert any("banner and body disagree" in w for w in warnings)


# ===========================================================================
# Check 5: template phrases
# ===========================================================================

def test_check5_pass_no_template():
    md = "This function inserts into FCT_X joining DIM_Y."
    multi = {"FN_FOO": {"source_code": [{"line": 1, "text": "INSERT INTO FCT_X"}]}}
    assert _w57_check_template_phrases(md, multi) == []


def test_check5_pass_template_supported_by_source():
    """Phrase claim "ONLY runs in December" — source has EXTRACT(MONTH... = 12."""
    md = "This function only runs in december when triggered."
    multi = {"FN_FOO": {"source_code": [
        {"line": 1, "text": "WHERE EXTRACT(MONTH FROM dt) = 12"}
    ]}}
    assert _w57_check_template_phrases(md, multi) == []


def test_check5_fail_template_unsupported():
    md = "This function only runs in december."
    multi = {"FN_FOO": {"source_code": [
        {"line": 1, "text": "INSERT INTO FCT_X SELECT * FROM STG_X"}
    ]}}
    warnings = _w57_check_template_phrases(md, multi)
    assert any("only runs in december" in w for w in warnings)


def test_check5_fail_no_internal_gating_when_source_has_if():
    md = "There is no internal gating logic in this function."
    multi = {"FN_FOO": {"source_code": [
        {"line": 1, "text": "IF v_flag = 'Y' THEN INSERT INTO FCT_X END IF"}
    ]}}
    warnings = _w57_check_template_phrases(md, multi)
    assert any("no internal gating" in w for w in warnings)


# ===========================================================================
# Check 6: caveat-vs-badge
# ===========================================================================

def test_check6_pass_no_caveat():
    md = "Step 1 (Lines 5-10). Clean prose."
    assert _w57_check_caveat_vs_badge(md) == []


def test_check6_fail_may_describe_related():
    """Run 3 benchmark D1: response contains the canonical RTIE caveat."""
    md = (
        "Step 1 (Lines 5-10). The explanation below may describe "
        "functions related to FOO rather than FOO itself."
    )
    warnings = _w57_check_caveat_vs_badge(md)
    assert any(
        "may describe functions related to" in w for w in warnings
    )


def test_check6_fail_semantic_search_phrase():
    md = "The semantic search returned different functions than asked."
    warnings = _w57_check_caveat_vs_badge(md)
    assert any("semantic search returned different functions" in w
               for w in warnings)


def test_check6_fail_verify_against_production_phrase():
    md = "Please verify against the actual production code."
    warnings = _w57_check_caveat_vs_badge(md)
    assert any("verify against the actual production code" in w
               for w in warnings)


# ===========================================================================
# Integration: w57_enforce_grounding aggregation + evaluate_grounding badge
# ===========================================================================

def test_w57_enforce_aggregates_warnings():
    """A4 fabrication: range repeats AND citation cap both fire."""
    md = " ".join(f"Step {i} (Lines 198-369)." for i in range(1, 173))
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(500)}}
    warnings = w57_enforce_grounding(
        raw_query="How does FN_LOAD_OPS_RISK_DATA work?",
        markdown=md,
        multi_source=multi,
        functions_analyzed=["FN_LOAD_OPS_RISK_DATA"],
        redis_client=None,
    )
    assert any("cited 172 times" in w for w in warnings)
    assert any("172 line citations" in w for w in warnings)


def test_evaluate_grounding_a1_clean_remains_verified():
    """A real ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM-shaped answer: 5
    citations, single function, no template phrases. Should remain
    VERIFIED with no W57 warnings."""
    md = (
        "Step 1: it inserts into FSI_RW_MAP_MASTER (Lines 22-23).\n\n"
        "Step 2: with PARALLEL hint (Lines 22).\n\n"
        "Step 3: from DIM_BASEL_ASSET_CLASS (Lines 22).\n\n"
        "Step 4: with COALESCE on null branches (Lines 22).\n\n"
        "Step 5: REJECT LIMIT 50 (Lines 23)."
    )
    result = evaluate_grounding(
        raw_query="How does ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM work?",
        markdown=md,
        multi_source={
            "ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM": {
                "source_code": _src(50)
            },
        },
        functions_analyzed=["ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM"],
        query_type="COLUMN_LOGIC",
        redis_client=None,
    )
    assert result["badge"] == "VERIFIED"
    # No W57 warnings of any tier (HIGH or LOW). The "GROUNDING-"
    # substring is shared by both tiered prefixes and not produced by
    # any pre-existing warning code.
    assert all("GROUNDING-" not in w for w in result["warnings"])


def test_evaluate_grounding_d1_caveat_flips_badge():
    """D1: response contains a self-aware caveat. Even with otherwise
    clean signals, badge must be UNVERIFIED."""
    md = (
        "Step 1 (Lines 5-10). Step 2 (Lines 11-20).\n\n"
        "The explanation below may describe functions related to FN_FOO."
    )
    result = evaluate_grounding(
        raw_query="How does FN_FOO work?",
        markdown=md,
        multi_source={"FN_FOO": {"source_code": _src(100)}},
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
        redis_client=None,
    )
    assert result["badge"] == "UNVERIFIED"
    assert any("self-aware caveat" in w for w in result["warnings"])


def test_evaluate_grounding_data_query_skips_w57():
    """W57 is gated to query types that require citations. DATA_QUERY
    paths must not be subject to it (they have their own validators)."""
    md = "The total is 1234.56."
    result = evaluate_grounding(
        raw_query="What is the total?",
        markdown=md,
        multi_source={},
        functions_analyzed=[],
        query_type="DATA_QUERY",
        redis_client=None,
    )
    # No W57 warnings should appear for non-explanation query types.
    # No W57 warnings of any tier (HIGH or LOW). The "GROUNDING-"
    # substring is shared by both tiered prefixes and not produced by
    # any pre-existing warning code.
    assert all("GROUNDING-" not in w for w in result["warnings"])


# ===========================================================================
# Warning deduplication (post-canary follow-up)
# ===========================================================================

def test_w57_dedupes_identical_warnings():
    """C13 case: 13 (FN_NAME, Lines X-Y) citations to a function not in
    retrieved sources emit 13 identical warnings from the per-claim
    binding check. After dedup, the user sees one entry per unique
    problem."""
    md = " ".join(
        f"(REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP, Lines {i}-{i+2})"
        for i in range(10, 23)
    )
    multi = {"CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION": {
        "source_code": _src(50)
    }}
    warnings = w57_enforce_grounding(
        raw_query="How is CAP943 calculated?",
        markdown=md,
        multi_source=multi,
        functions_analyzed=["CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"],
        redis_client=None,
    )
    # Each citation has a distinct line range, so the per-claim binding
    # check fires 13 times — but the warning text is identical for
    # every "cited function not in retrieved sources" emission. After
    # dedup the user sees that message exactly once.
    not_in_sources = [
        w for w in warnings
        if "REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP" in w
        and "not in retrieved sources" in w
    ]
    assert len(not_in_sources) == 1


def test_w57_dedup_preserves_order():
    """First-occurrence wins. The order in which checks run determines
    the order of warnings; dedup must not reshuffle survivors."""
    # Construct a markdown that triggers (in this order):
    #   1. range-repeat (per-claim binding, fires first)
    #   2. citation count cap (Check 2, fires second)
    md = " ".join(f"(Lines 5-10)." for _ in range(60))
    warnings = w57_enforce_grounding(
        raw_query="How does FN_FOO work?",
        markdown=md,
        multi_source={"FN_FOO": {"source_code": _src(100)}},
        functions_analyzed=["FN_FOO"],
        redis_client=None,
    )
    # Both fire; the range-repeat warning comes from Check 1 (first in
    # the aggregation order) and must precede the cap warning.
    repeat_idx = next(
        i for i, w in enumerate(warnings) if "cited 60 times" in w
    )
    cap_idx = next(
        i for i, w in enumerate(warnings) if "60 line citations" in w
    )
    assert repeat_idx < cap_idx


def test_w57_dedup_keeps_distinct_messages():
    """Distinct warnings (different message text) survive dedup as
    separate entries — dedup must not collapse genuine variety."""
    md = (
        # Padding fires
        " ".join(f"(Lines 5-10)." for _ in range(5))
        # Self-aware caveat fires
        + " The explanation may describe functions related to FN_FOO."
    )
    warnings = w57_enforce_grounding(
        raw_query="How does FN_FOO work?",
        markdown=md,
        multi_source={"FN_FOO": {"source_code": _src(100)}},
        functions_analyzed=["FN_FOO"],
        redis_client=None,
    )
    # Two distinct warnings, neither collapsed.
    assert any("cited 5 times" in w for w in warnings)
    assert any("self-aware caveat" in w for w in warnings)
    # And total length matches unique message count — no
    # accidental truncation.
    assert len(warnings) == len(set(warnings))


# ===========================================================================
# Severity tiering (W57 follow-up: GROUNDING-HIGH vs GROUNDING-LOW)
# ===========================================================================

def test_low_severity_does_not_flip_badge():
    """Padding-only signals (Check 2: total citations >50) are LOW
    severity. They surface as advisories but the badge stays VERIFIED."""
    md = " ".join(f"(Lines {10*i}-{10*i+5})." for i in range(1, 60))
    result = evaluate_grounding(
        raw_query="How does FN_FOOBAR work?",
        markdown=md,
        multi_source={"FN_FOOBAR": {"source_code": _src(700)}},
        functions_analyzed=["FN_FOOBAR"],
        query_type="COLUMN_LOGIC",
        redis_client=None,
    )
    assert result["badge"] == "VERIFIED"
    assert any(w.startswith("GROUNDING-LOW:") for w in result["warnings"])
    assert not any(w.startswith("GROUNDING-HIGH:") for w in result["warnings"])


def test_high_severity_flips_badge():
    """Content trust violations (Check 6: self-aware caveat) are HIGH
    severity. Badge flips to UNVERIFIED even when no other signal fires."""
    md = (
        "Step 1 (Lines 5-10).\n\n"
        "The explanation below may describe functions related to FN_FOOBAR."
    )
    result = evaluate_grounding(
        raw_query="How does FN_FOOBAR work?",
        markdown=md,
        multi_source={"FN_FOOBAR": {"source_code": _src(100)}},
        functions_analyzed=["FN_FOOBAR"],
        query_type="COLUMN_LOGIC",
        redis_client=None,
    )
    assert result["badge"] == "UNVERIFIED"
    assert any(w.startswith("GROUNDING-HIGH:") for w in result["warnings"])


def test_mixed_severity_flips_on_high():
    """When both LOW and HIGH warnings fire, the HIGH wins — badge
    UNVERIFIED, both warnings preserved in the list."""
    # Range repeated 5× → Check 1.3 LOW.
    # Plus an unsupported "PASS-THROUGH" template phrase → Check 5 HIGH.
    md = (
        " ".join("(Lines 5-10)." for _ in range(5))
        + " The function is a pass-through with no internal gating."
    )
    multi = {"FN_FOOBAR": {"source_code": [
        # Source has MERGE INTO + multiple INSERTs → "pass-through"
        # claim is unsupported, and IF/CASE present so "no internal
        # gating" is also unsupported.
        {"line": i, "text": (
            "INSERT INTO FCT_X SELECT * FROM STG_X; "
            "MERGE INTO FCT_Y; IF v_flag THEN ..."
        )} for i in range(1, 50)
    ]}}
    result = evaluate_grounding(
        raw_query="How does FN_FOOBAR work?",
        markdown=md,
        multi_source=multi,
        functions_analyzed=["FN_FOOBAR"],
        query_type="COLUMN_LOGIC",
        redis_client=None,
    )
    assert result["badge"] == "UNVERIFIED"
    has_low = any(w.startswith("GROUNDING-LOW:") for w in result["warnings"])
    has_high = any(w.startswith("GROUNDING-HIGH:") for w in result["warnings"])
    assert has_low and has_high


def test_b3_style_over_cited_correct_answer_stays_verified():
    """Manual B3 fixture: 296 citations to one valid line range, content
    is otherwise correct (no anchoring/template/hierarchy issues). Per
    the severity-tier design, this is padding-only (Check 1.3 LOW + Check 2
    LOW) — badge must stay VERIFIED with a citation-hygiene advisory."""
    md = " ".join(f"Step {i}: explained (Lines 30-40)." for i in range(1, 297))
    result = evaluate_grounding(
        raw_query="How does FN_FOOBAR work?",
        markdown=md,
        multi_source={"FN_FOOBAR": {"source_code": _src(500)}},
        functions_analyzed=["FN_FOOBAR"],
        query_type="COLUMN_LOGIC",
        redis_client=None,
    )
    # Over-cited correct answer: VERIFIED + LOW advisory.
    assert result["badge"] == "VERIFIED"
    low_warnings = [
        w for w in result["warnings"] if w.startswith("GROUNDING-LOW:")
    ]
    high_warnings = [
        w for w in result["warnings"] if w.startswith("GROUNDING-HIGH:")
    ]
    assert low_warnings, "expected at least one GROUNDING-LOW: advisory"
    assert not high_warnings, (
        f"unexpected HIGH warnings on padding-only fabrication: {high_warnings}"
    )


# ===========================================================================
# String-handling defenses (Run 5 / A4 lost-catch regressions)
# ===========================================================================
#
# Run 5 produced VERIFIED with zero warnings on A4 ("How does
# FN_LOAD_OPS_RISK_DATA work?") because three independent string-handling
# defects in the W57 helpers all happened to align on that one body:
#
#   * _LINE_REF_RE was case-sensitive, so "lines 198-369" (lowercase l)
#     bypassed Check 1.3 (range repeat) AND Check 2 (citation count cap).
#   * _w57_check_template_phrases compared with ASCII "pass-through",
#     but A4 wrote "Pass‑through" with U+2011 NON-BREAKING HYPHEN,
#     bypassing Check 5.
#   * _W57_FUNC_CITATION_RE required a comma between the function name
#     and "Lines", but A4's format was "(FN_X lines 198-369)" with no
#     comma — disabling Checks 1.1/1.2 on that shape.
#
# Each test below pins one of these regressions; the integration test at
# the end is the non-negotiable design proof — A4's body must produce
# at least one HIGH warning post-fix.

def test_fix1_lowercase_lines_caught_by_check_1_3():
    """Bug 1 regression: lowercase "lines X-Y" must contribute to
    range-repeat counting (Check 1.3)."""
    md = " ".join(
        f"Step {i}: explanation (FN_LOAD_OPS_RISK_DATA lines 198-369)."
        for i in range(1, 11)
    )
    multi = {"FN_LOAD_OPS_RISK_DATA": {"source_code": _src(400)}}
    warnings = _w57_check_per_claim_binding(
        md, multi, ["FN_LOAD_OPS_RISK_DATA"]
    )
    assert any(
        w.startswith("GROUNDING-LOW:") and "Lines 198-369 cited 10 times" in w
        for w in warnings
    ), f"Expected range-repeat LOW on lowercase 'lines'; got: {warnings}"


def test_fix1_lowercase_lines_counted_by_citation_cap():
    """Bug 1 regression: Check 2 must count lowercase "lines" events.

    Build a body with 60 lowercase "lines X-Y" citations to force the
    cap of 50."""
    md = " ".join(
        f"Step {i}: explanation (FN_X lines {i}-{i+1})."
        for i in range(1, 61)
    )
    warnings = _w57_check_citation_count_cap(md)
    assert any(
        w.startswith("GROUNDING-LOW:") and "60 line citations" in w
        for w in warnings
    ), f"Expected citation-cap LOW on lowercase 'lines'; got: {warnings}"


def test_fix2_unicode_dash_in_template_phrase():
    """Bug 2 regression: "Pass‑through" with U+2011 NON-BREAKING
    HYPHEN must match the ASCII "pass-through" template phrase after
    the dash-normalization shim."""
    md = "The value is Pass‑through (no transformation)."
    multi = {"FN_X": {"source_code": [
        # Validator predicate: src has >1 INSERT INTO and no MERGE →
        # "pass-through" claim is unsupported → HIGH warning.
        {"line": 1, "text": "INSERT INTO TARGET_A SELECT * FROM STG_A;"},
        {"line": 2, "text": "INSERT INTO TARGET_B SELECT * FROM STG_B;"},
        {"line": 3, "text": "INSERT INTO TARGET_C SELECT * FROM STG_C;"},
    ]}}
    warnings = _w57_check_template_phrases(md, multi)
    assert any(
        w.startswith("GROUNDING-HIGH:") and "pass-through" in w
        for w in warnings
    ), (
        f"Expected template-phrase HIGH on U+2011 hyphen; got: {warnings}"
    )


# Note: _w57_check_caveat_vs_badge also applies _w57_ascii_normalize
# defensively, but none of the current _W57_CAVEAT_TRIGGERS contain
# hyphens, so there is no live failure mode to regress against here.
# The shim is applied for symmetry and to future-proof additions to
# the trigger list (e.g. "may not be the actual function you asked
# about" already uses spaces; a future hyphenated trigger would
# benefit from the normalization).


def test_fix3_no_comma_function_citation_parsed():
    """Bonus regression: "(FN_X lines 198-369)" — no comma between the
    function name and "Lines" — must be matched by the relaxed
    _W57_FUNC_CITATION_RE."""
    body = "(FN_X lines 198-369) discusses the same range repeatedly."
    matches = list(_W57_FUNC_CITATION_RE.finditer(body))
    assert len(matches) == 1, f"expected 1 match, got {len(matches)}"
    assert matches[0].group(1) == "FN_X"
    assert matches[0].group(2) == "198"
    assert matches[0].group(3) == "369"


def test_fix3_no_comma_routes_to_check_1_1():
    """Bonus regression: with the relaxed comma-or-whitespace separator,
    Check 1.1 (cited fn not in retrieved sources) now fires on
    A4-shaped citations whose function isn't in multi_source."""
    md = "Step 1 (FN_NOT_RETRIEVED lines 5-10) is the key claim."
    multi = {"FN_FOO": {"source_code": _src(100)}}
    warnings = _w57_check_per_claim_binding(md, multi, ["FN_FOO"])
    assert any(
        w.startswith("GROUNDING-HIGH:") and
        "FN_NOT_RETRIEVED" in w and
        "not in retrieved sources" in w
        for w in warnings
    ), f"expected HIGH on no-comma A4-shape format; got: {warnings}"


# Synthetic A4-style body covering all three regression vectors:
#   - 9× lowercase "(FN_LOAD_OPS_RISK_DATA lines 198-369)" + 2× capital
#     "Lines 198-369" so Check 1.3 sees count=11>3 → LOW
#   - 60+ events total so Check 2 also fires LOW
#   - "Pass‑through" with U+2011 in a section header so Check 5
#     fires HIGH (validator: source has >1 INSERT INTO and no MERGE)
_A4_INLINE_BODY = (
    "## FN_LOAD_OPS_RISK_DATA — Overview\n\n"
    "Execution condition: This function runs whenever called "
    "(FN_LOAD_OPS_RISK_DATA lines 198-369). The behaviour is described "
    "below as a Pass‑through with date alignment.\n\n"
    "```sql\n"
    "-- FUNCTION: FN_LOAD_OPS_RISK_DATA\n"
    "-- Lines 198-369\n"
    "```\n\n"
    "### Step 1: Pass‑through behaviour (Lines 198-369)\n"
    + "\n".join(
        f"- Claim {i}: detail (FN_LOAD_OPS_RISK_DATA lines 198-369)."
        for i in range(1, 60)
    )
)


def test_a4_full_body_gets_caught_after_fixes():
    """End-to-end: an A4-style body that produced VERIFIED in benchmark
    Run 5 must now produce at least one GROUNDING-HIGH warning. This is
    the non-negotiable design proof for the three string-handling
    fixes — the lost catch must not be lost again."""
    multi_source = {"FN_LOAD_OPS_RISK_DATA": {
        "source_code": [
            # Validator for "pass-through": src has >1 INSERT INTO and
            # no MERGE → claim unsupported → HIGH.
            {"line": i, "text": "INSERT INTO TARGET_A SELECT * FROM STG_A;"}
            for i in range(1, 401)
        ],
    }}
    warnings = w57_enforce_grounding(
        raw_query="How does FN_LOAD_OPS_RISK_DATA work?",
        markdown=_A4_INLINE_BODY,
        multi_source=multi_source,
        functions_analyzed=["FN_LOAD_OPS_RISK_DATA"],
        redis_client=None,
    )
    assert any(w.startswith("GROUNDING-HIGH:") for w in warnings), (
        f"A4-style body must produce at least one HIGH warning post-fix; "
        f"got: {warnings}"
    )
    # And specifically the pass-through template HIGH for the U+2011
    # path (the canonical A4 lost-catch signature).
    assert any(
        "pass-through" in w and w.startswith("GROUNDING-HIGH:")
        for w in warnings
    ), f"expected pass-through HIGH on A4 body; got: {warnings}"
