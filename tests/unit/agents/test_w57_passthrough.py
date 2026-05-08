"""W68: refined 'pass-through' predicate for W57 Check 5.

The pre-W68 predicate flagged any function containing MERGE as
not-pass-through, which over-fired on legitimate pass-through
descriptions of column-mapping MERGEs (the v2 benchmark Run 7 A2
false-flip on CS_Goodwill_Calculation). Post-W68, the predicate
inspects the WHEN MATCHED THEN UPDATE SET clause for transform
indicators (arithmetic, aggregate, sub-SELECT) and only rejects when
one is present in the SET expression itself.

The test fixtures cover both the helpers in isolation
(`_w57_supports_passthrough`, `_w57_extract_merge_set_clauses`,
`_w57_set_has_transform`) and the integration via
`_w57_check_template_phrases`.
"""

from src.agents.logic_explainer import (
    _w57_check_template_phrases,
    _w57_extract_merge_set_clauses,
    _w57_set_has_transform,
    _w57_supports_passthrough,
)


# ===========================================================================
# Real-source excerpt: the asked-about function from the v2 Run 7 A2
# false-flip. The MERGE SET clause assigns a CASE arm of bare aliases
# (EXP_..._10 / EXP_..._11). The transformations live in the USING
# subquery, not in the SET expression.
# ===========================================================================

CS_GOODWILL_MERGE_EXCERPT = (
    "MERGE  INTO FCT_STANDARD_ACCT_HEAD TT USING ( "
    "SELECT  /*+ PARALLEL(4) */ FCT_STANDARD_ACCT_HEAD.N_RUN_SKEY, "
    "MIN(FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT) AS T_1470990981178_0, "
    "MIN(CASE WHEN ( ((DIM_STANDARD_ACCT_HEAD.V_STD_ACCT_HEAD_ID = 'CAP012')) ) "
    "THEN 10 ELSE 11 END) AS COND_1470990981178_10, "
    "(MAX(coalesce(CASE WHEN CAPITAL_ACCOUNTING.N_STD_ACCT_HEAD_SKEY IN "
    "(SELECT DIM_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_SKEY FROM DIM_STANDARD_ACCT_HEAD "
    "WHERE DIM_STANDARD_ACCT_HEAD.V_STD_ACCT_HEAD_ID = 'CAP1506') "
    "THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END ,0)) + "
    "MAX(coalesce(CASE WHEN CAPITAL_ACCOUNTING.N_STD_ACCT_HEAD_SKEY IN "
    "(SELECT DIM_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_SKEY FROM DIM_STANDARD_ACCT_HEAD "
    "WHERE DIM_STANDARD_ACCT_HEAD.V_STD_ACCT_HEAD_ID = 'CAP1507') "
    "THEN CAPITAL_ACCOUNTING.n_std_acct_head_amt ELSE NULL END ,0))) AS EXP_1470990981178_10, "
    "MIN(FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT) AS EXP_1470990981178_11 "
    "FROM FCT_STANDARD_ACCT_HEAD WHERE 1=1 ) SS "
    "ON ( TT.N_RUN_SKEY = SS.N_RUN_SKEY ) "
    "WHEN MATCHED THEN UPDATE SET "
    "TT.N_STD_ACCT_HEAD_AMT = CASE WHEN COND_1470990981178_10=10 "
    "THEN EXP_1470990981178_10 ELSE EXP_1470990981178_11 END;"
)


# ===========================================================================
# Real-source excerpt: a transforming UPDATE/INSERT body. The active SQL
# in FN_LOAD_OPS_RISK_DATA contains 2 INSERT INTOs and arithmetic in an
# UPDATE SET (it is *not* a MERGE — its disqualification flows through
# Condition A failing, not Condition B). Predicate must still reject.
# ===========================================================================

FN_LOAD_OPS_RISK_DATA_EXCERPT = (
    "INSERT INTO STG_OPS_RISK_DATA ORD (ORD.FIC_MIS_DATE, ORD.N_ALPHA_PERCENT) "
    "SELECT CQD FIC_MIS_DATE, 0.15 FROM ABL_OPS_RISK_DATA M "
    "WHERE M.FIC_MIS_DATE = CQD; "
    "UPDATE STG_OPS_RISK_DATA OPS SET OPS.N_ANNUAL_GROSS_INCOME = "
    "CASE WHEN OPS.V_LOB_CODE = 'CBA' "
    "THEN NVL (OPS.N_ANNUAL_GROSS_INCOME + TOT1 + CBA_DEDUCTION, 0) "
    "WHEN OPS.V_LOB_CODE = 'RBA' "
    "THEN NVL (OPS.N_ANNUAL_GROSS_INCOME, 0) + LN_DEDUCITON_RATIO_1 END "
    "WHERE OPS.FIC_MIS_DATE = CQD; "
    "INSERT INTO OFSDWH_ERROR_LOG (V_MAIN_PROG_NAME) VALUES (LV_PROG_NAME);"
)


# ===========================================================================
# Predicate: positive cases (returns True — pass-through is supported)
# ===========================================================================

def test_predicate_pos_pure_insert_only_function():
    """Condition A — a single INSERT INTO and no MERGE. This is the
    pre-W68 baseline behaviour, still preserved."""
    src = "INSERT INTO TARGET SELECT V_GL_CODE FROM STG_GL_DATA;"
    assert _w57_supports_passthrough(src) is True


def test_predicate_pos_cs_goodwill_real_source_excerpt():
    """Real-source excerpt: CS_Goodwill_Calculation MERGE. The SET
    assigns a CASE arm of bare aliases (EXP_..._10 / EXP_..._11) — the
    transformation lives in the USING subquery and the SET itself is
    column-mapping shaped. This is the v2 Run 7 A2 false-flip case."""
    assert _w57_supports_passthrough(CS_GOODWILL_MERGE_EXCERPT) is True


def test_predicate_pos_simple_column_mapping_merge():
    """Bare column copy: SET TT.col_a = SS.col_a, TT.col_b = SS.col_b."""
    src = (
        "MERGE INTO FCT_TGT TT USING (SELECT * FROM STG_SRC) SS "
        "ON (TT.N_KEY = SS.N_KEY) "
        "WHEN MATCHED THEN UPDATE SET "
        "TT.V_COL_A = SS.V_COL_A, TT.V_COL_B = SS.V_COL_B;"
    )
    assert _w57_supports_passthrough(src) is True


def test_predicate_pos_case_with_existing_columns_only():
    """CASE WHEN with bare column references in the THEN/ELSE arms is
    still pass-through (selecting between existing values, not
    computing). The W68 archetype: MERGE SET = CASE WHEN cond THEN
    SS.colA ELSE SS.colB END."""
    src = (
        "MERGE INTO FCT_TGT TT USING (SELECT * FROM STG_SRC) SS "
        "ON (TT.N_KEY = SS.N_KEY) "
        "WHEN MATCHED THEN UPDATE SET "
        "TT.N_AMT = CASE WHEN SS.V_FLAG='Y' THEN SS.N_NEW_AMT "
        "ELSE TT.N_AMT END;"
    )
    assert _w57_supports_passthrough(src) is True


# ===========================================================================
# Predicate: negative cases (returns False — pass-through unsupported)
# ===========================================================================

def test_predicate_neg_arithmetic_in_set():
    """Arithmetic operator in SET expression → transforming MERGE."""
    src = (
        "MERGE INTO FCT_TGT TT USING (SELECT * FROM STG_SRC) SS "
        "ON (TT.N_KEY = SS.N_KEY) "
        "WHEN MATCHED THEN UPDATE SET TT.N_AMT = SS.N_AMT * 0.15;"
    )
    assert _w57_supports_passthrough(src) is False


def test_predicate_neg_aggregate_in_set():
    """Aggregate function in SET expression → transforming MERGE."""
    src = (
        "MERGE INTO FCT_TGT TT USING (SELECT * FROM STG_SRC) SS "
        "ON (TT.N_KEY = SS.N_KEY) "
        "WHEN MATCHED THEN UPDATE SET TT.N_TOTAL = SUM(SS.N_VALUES);"
    )
    assert _w57_supports_passthrough(src) is False


def test_predicate_neg_subquery_in_set():
    """Sub-SELECT in SET expression → transforming MERGE."""
    src = (
        "MERGE INTO FCT_TGT TT USING (SELECT * FROM STG_SRC) SS "
        "ON (TT.N_KEY = SS.N_KEY) "
        "WHEN MATCHED THEN UPDATE SET TT.N_RATE = "
        "(SELECT N_RATE FROM DIM_RATES WHERE V_CODE = SS.V_CODE);"
    )
    assert _w57_supports_passthrough(src) is False


def test_predicate_neg_multi_insert_no_merge():
    """Function with multiple INSERT INTOs and no MERGE — Condition A
    fails on the count, Condition B doesn't apply. Pre-W68 baseline."""
    src = (
        "INSERT INTO T1 SELECT * FROM S1; "
        "INSERT INTO T2 SELECT * FROM S2; "
        "INSERT INTO T3 SELECT * FROM S3;"
    )
    assert _w57_supports_passthrough(src) is False


def test_predicate_neg_fn_load_ops_risk_data_real_excerpt():
    """Real-source excerpt: FN_LOAD_OPS_RISK_DATA active body. Multiple
    INSERTs + arithmetic UPDATE SET. Predicate must still reject — this
    is the canonical canary-b guard."""
    assert _w57_supports_passthrough(FN_LOAD_OPS_RISK_DATA_EXCERPT) is False


def test_predicate_neg_merge_with_arithmetic_in_case_arm():
    """A MERGE whose SET CASE has arithmetic inside one arm. Even when
    the CASE shape looks like column selection at the top level, the
    arithmetic in the THEN expression is a transform indicator."""
    src = (
        "MERGE INTO FCT_TGT TT USING (SELECT * FROM STG_SRC) SS "
        "ON (TT.N_KEY = SS.N_KEY) "
        "WHEN MATCHED THEN UPDATE SET "
        "TT.N_AMT = CASE WHEN SS.V_FLAG='Y' "
        "THEN SS.N_AMT + SS.N_FEE ELSE SS.N_AMT END;"
    )
    assert _w57_supports_passthrough(src) is False


def test_predicate_neg_merge_without_set_clause():
    """A MERGE keyword present but no parseable WHEN MATCHED THEN UPDATE
    SET (e.g. malformed or insert-only MERGE). Strict reject — this
    preserves the pre-W68 catch on synthetic test fixtures used by the
    existing W57 suite (e.g. test_mixed_severity_flips_on_high)."""
    src = "INSERT INTO FCT_X SELECT * FROM STG_X; MERGE INTO FCT_Y;"
    assert _w57_supports_passthrough(src) is False


# ===========================================================================
# Helper: SET-clause extraction
# ===========================================================================

def test_extract_set_clauses_skips_when_inside_case():
    """The terminator scan must not stop at WHEN tokens nested inside a
    CASE expression — that would truncate the SET clause prematurely."""
    src = (
        "MERGE INTO TT USING SS ON (1=1) "
        "WHEN MATCHED THEN UPDATE SET "
        "TT.N_AMT = CASE WHEN SS.V_FLAG='Y' THEN SS.N_NEW ELSE SS.N_OLD END;"
    )
    clauses = _w57_extract_merge_set_clauses(src)
    assert len(clauses) == 1
    # The CASE/END must be inside the extracted clause — otherwise the
    # transform-indicator check loses visibility into the arms.
    assert "CASE" in clauses[0].upper() and "END" in clauses[0].upper()


def test_extract_set_clauses_skips_where_inside_subquery():
    """The terminator scan must not stop at WHERE inside a sub-SELECT,
    or it'd return a partial SET clause that misses the (SELECT marker
    that disqualifies it."""
    src = (
        "MERGE INTO TT USING SS ON (1=1) "
        "WHEN MATCHED THEN UPDATE SET "
        "TT.N_RATE = (SELECT N_RATE FROM DIM_RATES "
        "WHERE V_CODE = SS.V_CODE);"
    )
    clauses = _w57_extract_merge_set_clauses(src)
    assert len(clauses) == 1
    assert "(SELECT" in clauses[0].upper()


def test_extract_set_clauses_returns_empty_when_no_set():
    """A MERGE without WHEN MATCHED THEN UPDATE SET → empty list,
    which `_w57_supports_passthrough` interprets as strict reject."""
    src = "INSERT INTO FCT_X SELECT * FROM STG_X; MERGE INTO FCT_Y;"
    assert _w57_extract_merge_set_clauses(src) == []


# ===========================================================================
# Helper: transform-indicator detection
# ===========================================================================

def test_set_has_transform_detects_aggregates():
    assert _w57_set_has_transform("TT.N_TOTAL = SUM(SS.N_VAL)") is True
    assert _w57_set_has_transform("TT.N_MAX = MAX(SS.N_VAL)") is True
    assert _w57_set_has_transform("TT.N_MIN = MIN(SS.N_VAL)") is True
    assert _w57_set_has_transform("TT.N_AVG = AVG(SS.N_VAL)") is True
    assert _w57_set_has_transform("TT.N_CT = COUNT(SS.N_KEY)") is True


def test_set_has_transform_detects_arithmetic():
    assert _w57_set_has_transform("TT.N = SS.A + SS.B") is True
    assert _w57_set_has_transform("TT.N = SS.A - SS.B") is True
    assert _w57_set_has_transform("TT.N = SS.A * 0.15") is True
    assert _w57_set_has_transform("TT.N = SS.A / 100") is True


def test_set_has_transform_detects_subquery():
    assert _w57_set_has_transform(
        "TT.N = (SELECT V FROM DIM_T WHERE K=1)"
    ) is True
    # Whitespace tolerance after the opening paren.
    assert _w57_set_has_transform(
        "TT.N = (  SELECT V FROM DIM_T)"
    ) is True


def test_set_has_transform_clean_column_mapping_returns_false():
    assert _w57_set_has_transform("TT.A = SS.A, TT.B = SS.B") is False
    # CASE arms with bare column references — no transform.
    assert _w57_set_has_transform(
        "TT.N = CASE WHEN COND=10 THEN EXP_X ELSE EXP_Y END"
    ) is False
    # Sentinels are pass-through-shaped.
    assert _w57_set_has_transform(
        "TT.N = CASE WHEN SS.V='Y' THEN 1 ELSE 0 END"
    ) is False


# ===========================================================================
# End-to-end: integration through _w57_check_template_phrases
# ===========================================================================

def test_e2e_a2_no_warning_on_cs_goodwill_passthrough_claim():
    """W68 design proof. The asked-about function's source is the real
    CS_Goodwill_Calculation MERGE excerpt; the response describes the
    EXP_..._11 fallback as 'pass-through'. Post-W68 this must NOT
    produce a Check 5 warning."""
    markdown = (
        "## CS_Goodwill_Calculation\n\n"
        "For non-CAP012 standard account heads, the existing "
        "FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT is passed through "
        "without modification (CS_Goodwill_Calculation, Lines 24-24). "
        "The MERGE SET clause selects between EXP_..._10 (the CAP012 "
        "branch) and EXP_..._11 (the pass-through branch)."
    )
    multi_source = {
        "CS_GOODWILL_CALCULATION": {
            "source_code": [
                {"line": i + 1, "text": chunk}
                for i, chunk in enumerate(CS_GOODWILL_MERGE_EXCERPT.split(" "))
            ],
            "score": 0.9,
        },
    }
    warnings = _w57_check_template_phrases(
        markdown=markdown,
        multi_source=multi_source,
        asked_about_function="CS_GOODWILL_CALCULATION",
    )
    high = [w for w in warnings if "pass-through" in w]
    assert high == [], (
        f"W68: pass-through HIGH must NOT fire on the CS_Goodwill "
        f"column-mapping MERGE; got: {warnings}"
    )


def test_e2e_canary_b_fn_load_still_fires_high():
    """Quality gate 2: the FN_LOAD_OPS_RISK_DATA pre-existing catch
    must be preserved post-W68. The active body has multiple INSERTs
    plus arithmetic in an UPDATE SET — predicate should reject."""
    markdown = (
        "## FN_LOAD_OPS_RISK_DATA\n\n"
        "This is a pass-through that loads operational risk data "
        "(FN_LOAD_OPS_RISK_DATA, Lines 200-300)."
    )
    multi_source = {
        "FN_LOAD_OPS_RISK_DATA": {
            "source_code": [
                {"line": i + 1, "text": chunk}
                for i, chunk in enumerate(
                    FN_LOAD_OPS_RISK_DATA_EXCERPT.split(" ")
                )
            ],
            "score": 0.9,
        },
    }
    warnings = _w57_check_template_phrases(
        markdown=markdown,
        multi_source=multi_source,
        asked_about_function="FN_LOAD_OPS_RISK_DATA",
    )
    assert any(
        w.startswith("GROUNDING-HIGH:") and "pass-through" in w
        for w in warnings
    ), (
        f"W68: FN_LOAD_OPS_RISK_DATA pass-through catch lost — predicate "
        f"loosened too far; got: {warnings}"
    )
