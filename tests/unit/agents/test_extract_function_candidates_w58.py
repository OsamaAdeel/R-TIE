"""W58: function-precheck false-positive exclusions.

Pin the candidate-extraction filter against three identifier classes that
look like function names but never are:

  W58.a — OFSAA table prefixes (FCT_, DIM_, STG_, FSI_, SETUP_, AAI_)
  W58.b — OFSAA-generated internal aliases (EXP_<n>, COND_<n>, T_<n>, SS_*, TT_*)
  W58.c — OFSAA column prefixes (N_, V_, F_, D_, I_, T_)

Without these exclusions the function-precheck DECLINEs legitimate queries
that mention staging tables, generated CASE labels, or column names by
returning ``function_not_found``.
"""

from src.agents.orchestrator import extract_function_candidates


# ---------------------------------------------------------------------------
# W58.a — Table prefixes
# ---------------------------------------------------------------------------

def test_w58a_excludes_fct_table():
    assert "FCT_OPS_RISK_DATA" not in extract_function_candidates(
        "What columns does FCT_OPS_RISK_DATA have?"
    )


def test_w58a_excludes_stg_table():
    assert "STG_GL_DATA" not in extract_function_candidates(
        "Trace from STG_GL_DATA to FCT_STANDARD_ACCT_HEAD"
    )


def test_w58a_excludes_dim_table():
    assert "DIM_BASEL_METHODOLOGY" not in extract_function_candidates(
        "What is DIM_BASEL_METHODOLOGY used for?"
    )


def test_w58a_excludes_fsi_table():
    assert "FSI_CAP_MITIGANTS" not in extract_function_candidates(
        "How is FSI_CAP_MITIGANTS populated?"
    )


def test_w58a_excludes_setup_table():
    assert "SETUP_MASTER_TABLE" not in extract_function_candidates(
        "Look at SETUP_MASTER_TABLE for config"
    )


def test_w58a_excludes_aai_table():
    assert "AAI_AOM_APP_PACK_B" not in extract_function_candidates(
        "Is AAI_AOM_APP_PACK_B updated by the install?"
    )


# ---------------------------------------------------------------------------
# W58.b — Internal aliases
# ---------------------------------------------------------------------------

def test_w58b_excludes_exp_alias():
    assert "EXP_11" not in extract_function_candidates(
        "When does the EXP_11 branch fire?"
    )


def test_w58b_excludes_exp_long_alias():
    assert "EXP_1470990981178_10" not in extract_function_candidates(
        "What does EXP_1470990981178_10 evaluate to?"
    )


def test_w58b_excludes_cond_alias():
    assert "COND_10" not in extract_function_candidates(
        "What is COND_10?"
    )


def test_w58b_excludes_t_digit_alias():
    assert "T_1470990981178_0" not in extract_function_candidates(
        "Trace T_1470990981178_0 in the merge"
    )


def test_w58b_excludes_ss_alias():
    assert "SS_PARTY" not in extract_function_candidates(
        "What rows feed SS_PARTY in the subquery?"
    )


def test_w58b_excludes_tt_alias():
    assert "TT_TARGET" not in extract_function_candidates(
        "What columns of TT_TARGET get updated?"
    )


# ---------------------------------------------------------------------------
# W58.c — Column prefixes
# ---------------------------------------------------------------------------

def test_w58c_excludes_n_column():
    assert "N_ANNUAL_GROSS_INCOME" not in extract_function_candidates(
        "How is N_ANNUAL_GROSS_INCOME calculated?"
    )


def test_w58c_excludes_v_column():
    assert "V_LV_CODE" not in extract_function_candidates(
        "Filter by V_LV_CODE"
    )


def test_w58c_excludes_f_column():
    assert "F_REGULATORY_ENTITY_IND" not in extract_function_candidates(
        "What is F_REGULATORY_ENTITY_IND set to?"
    )


def test_w58c_excludes_d_column():
    assert "D_FINANCIAL_YEAR" not in extract_function_candidates(
        "Group by D_FINANCIAL_YEAR"
    )


# ---------------------------------------------------------------------------
# Positive cases — must STILL be extracted
# ---------------------------------------------------------------------------

def test_extracts_real_function_name():
    candidates = extract_function_candidates(
        "How does ABL_CAP_MITIGANT_DATA_POPULATION work?"
    )
    assert "ABL_CAP_MITIGANT_DATA_POPULATION" in candidates


def test_extracts_camelcase_function():
    candidates = extract_function_candidates(
        "How does Cap_Consl_Effective_Shareholding_Percent_for_an_Entity_Based_on_Consolidation_Approach work?"
    )
    assert any("Cap_Consl" in c for c in candidates)


def test_extracts_fn_prefix_function():
    candidates = extract_function_candidates(
        "How does FN_LOAD_OPS_RISK_DATA work?"
    )
    assert "FN_LOAD_OPS_RISK_DATA" in candidates


def test_extracts_cstm_suffix_function():
    candidates = extract_function_candidates(
        "How does OPS_RISK_DATA_POPULATION_CSTM work?"
    )
    assert "OPS_RISK_DATA_POPULATION_CSTM" in candidates


def test_extracts_t2t_prefixed_function():
    """T2T_* function names start with T2T_ (digit between letters), so the
    T_<digit> internal-alias pattern must not catch them."""
    candidates = extract_function_candidates(
        "How does T2T_FCT_CCP_DETAILS_STD_ACCT_HEAD_POP populate the target?"
    )
    assert "T2T_FCT_CCP_DETAILS_STD_ACCT_HEAD_POP" in candidates


# ---------------------------------------------------------------------------
# Mixed prompts — only real functions extracted
# ---------------------------------------------------------------------------

def test_mixed_table_and_function():
    candidates = extract_function_candidates(
        "How does FN_LOAD_OPS_RISK_DATA populate STG_OPS_RISK_DATA?"
    )
    assert "FN_LOAD_OPS_RISK_DATA" in candidates
    assert "STG_OPS_RISK_DATA" not in candidates


def test_mixed_column_and_function():
    candidates = extract_function_candidates(
        "In FN_LOAD_OPS_RISK_DATA, how is N_ANNUAL_GROSS_INCOME adjusted?"
    )
    assert "FN_LOAD_OPS_RISK_DATA" in candidates
    assert "N_ANNUAL_GROSS_INCOME" not in candidates


def test_mixed_alias_and_function():
    candidates = extract_function_candidates(
        "Inside ABL_CAP_MITIGANT_DATA_POPULATION, what does EXP_11 represent?"
    )
    assert "ABL_CAP_MITIGANT_DATA_POPULATION" in candidates
    assert "EXP_11" not in candidates
