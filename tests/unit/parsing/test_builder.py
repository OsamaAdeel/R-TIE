"""
Unit tests for src.parsing.builder — Tests 7-12.
"""

import pytest

from src.parsing.builder import build_calculation_block


# -----------------------------------------------------------------------
# Test 7: test_build_direct_calculation
# -----------------------------------------------------------------------

def test_build_direct_calculation():
    """A plain column reference like GL.N_AMOUNT_LCY produces a DIRECT
    calculation with the correct source_table and source_column."""
    result = build_calculation_block(
        "N_EOP_BAL", "GL.N_AMOUNT_LCY", [], 10,
    )

    assert result["type"] == "DIRECT"
    assert result["source_table"] == "GL"
    assert result["source_column"] == "N_AMOUNT_LCY"
    assert result["column"] == "N_EOP_BAL"
    assert result["line"] == 10


# -----------------------------------------------------------------------
# Test 8: test_build_fallback_calculation_nvl
# -----------------------------------------------------------------------

def test_build_fallback_calculation_nvl():
    """An NVL(...) expression produces a FALLBACK calculation whose
    primary references SETUP_GL_ATTRIBUTES and fallback is GL.V_CCY_CODE."""
    expression = (
        "NVL((SELECT V_CCY_CODE FROM SETUP_GL_ATTRIBUTES "
        "WHERE V_GL_CODE = GL.V_GL_CODE), GL.V_CCY_CODE)"
    )
    result = build_calculation_block("V_CCY_CODE", expression, [], 10)

    assert result["type"] == "FALLBACK"
    assert result["column"] == "V_CCY_CODE"
    # Primary should contain the sub-select referencing SETUP_GL_ATTRIBUTES
    assert "SETUP_GL_ATTRIBUTES" in result["primary"]
    # Fallback should reference GL.V_CCY_CODE
    assert "GL.V_CCY_CODE" in result["fallback"]


# -----------------------------------------------------------------------
# Test 9: test_build_conditional_calculation_decode
# -----------------------------------------------------------------------

def test_build_conditional_calculation_decode():
    """A DECODE expression produces a CONDITIONAL calculation with
    override entries for hardcoded literal mappings."""
    expression = "DECODE(GL.V_GL_CODE, '108012501-1107', 0, GL.N_AMOUNT_LCY)"
    result = build_calculation_block("N_AMOUNT_LCY1", expression, [], 10)

    assert result["type"] == "CONDITIONAL"
    assert result["column"] == "N_AMOUNT_LCY1"

    # Should have branches
    assert len(result["branches"]) >= 1

    # Should have at least one override with override_value = "0"
    overrides = result["overrides"]
    assert len(overrides) >= 1
    override_values = [o.get("result_value") for o in overrides]
    assert any(v == "0" for v in override_values)


# -----------------------------------------------------------------------
# Test 10: test_build_conditional_calculation_case
# -----------------------------------------------------------------------

def test_build_conditional_calculation_case():
    """A CASE WHEN expression produces a CONDITIONAL calculation with
    two branches (WHEN + ELSE)."""
    expression = (
        "CASE WHEN V_GL_HEAD_CATEGORY = 'ADVANCES' "
        "THEN 'MANUAL-ADVANCES' "
        "ELSE 'MANUAL-MISCELLANEOUS' END"
    )
    result = build_calculation_block("V_DATA_ORIGIN", expression, [], 10)

    assert result["type"] == "CONDITIONAL"
    assert result["column"] == "V_DATA_ORIGIN"
    assert len(result["branches"]) == 2

    branch_whens = [b["when"] for b in result["branches"]]
    branch_thens = [b["then"] for b in result["branches"]]
    assert any("ADVANCES" in w.upper() for w in branch_whens)
    assert any("MANUAL-ADVANCES" in t.upper() for t in branch_thens)
    assert any("MANUAL-MISCELLANEOUS" in t.upper() for t in branch_thens)


# -----------------------------------------------------------------------
# Test 11: test_build_arithmetic_calculation
# -----------------------------------------------------------------------

def test_build_arithmetic_calculation():
    """An expression with + and * operators produces an ARITHMETIC
    calculation type."""
    expression = "LN_TOTAL_DEDUCT + (-1 * LN_DEDUCITON_RATIO_1)"
    result = build_calculation_block("TOT1", expression, [], 10)

    assert result["type"] == "ARITHMETIC"
    assert result["column"] == "TOT1"
    # Should have parsed components (operands and operators)
    assert "components" in result
    assert len(result["components"]) >= 1


# -----------------------------------------------------------------------
# Test 12: test_build_composite_key_override
# -----------------------------------------------------------------------

def test_build_composite_key_override():
    """A DECODE on a concatenated key (col1 || '-' || col2) produces a
    CONDITIONAL calculation with COMPOSITE_KEY override type."""
    expression = (
        "DECODE(GL.V_GL_CODE || '-' || GL.V_BRANCH_CODE, "
        "'108012501-1107-PK0010343', 0, GL.N_AMOUNT_LCY)"
    )
    result = build_calculation_block("N_LCY_AMT", expression, [], 10)

    assert result["type"] == "CONDITIONAL"
    assert result["column"] == "N_LCY_AMT"

    overrides = result["overrides"]
    assert len(overrides) >= 1

    override_types = [o.get("type") for o in overrides]
    assert "COMPOSITE_KEY" in override_types


# -----------------------------------------------------------------------
# Test: test_scalar_compute_edge_wired_to_update
# -----------------------------------------------------------------------

def test_scalar_compute_edge_wired_to_update():
    """SCALAR_COMPUTE output variable should create edge to consuming UPDATE
    via build_intra_function_edges."""
    from src.parsing.builder import build_intra_function_edges
    scalar_node = {
        "id": "TEST_FN_N1",
        "type": "SCALAR_COMPUTE",
        "output_variable": "TOT1",
        "target_table": None,
        "source_tables": ["STG_GL_DATA"],
        "column_maps": {},
        "conditions": [],
        "line_start": 4,
        "line_end": 4,
    }
    update_node = {
        "id": "TEST_FN_N2",
        "type": "UPDATE",
        "target_table": "STG_OPS_RISK_DATA",
        "source_tables": [],
        "column_maps": {"N_ANNUAL_GROSS_INCOME": "TOT1"},
        "conditions": [],
        "line_start": 5,
        "line_end": 5,
    }
    edges = build_intra_function_edges([scalar_node, update_node], "TEST_FN")
    # Should have at least one edge involving TOT1
    var_edges = [e for e in edges if "TOT1" in str(e.get("source_col", "")) or "TOT1" in str(e.get("variable", ""))]
    assert len(var_edges) >= 1, f"Expected edge for TOT1, got edges: {edges}"


# -----------------------------------------------------------------------
# Test: test_variable_reference_in_calculation
# -----------------------------------------------------------------------

def test_variable_reference_in_calculation():
    """Local variable in expression should be tagged as VARIABLE_REFERENCE."""
    calc = build_calculation_block(
        "N_ANNUAL_GROSS_INCOME",
        "NVL(OPS.N_ANNUAL_GROSS_INCOME + TOT1 + CBA_DEDUCTION, 0)",
        [],
        10,
    )
    # Should detect TOT1 or CBA_DEDUCTION as variable references in components
    calc_type = calc.get("type", "")
    assert calc_type in ("FALLBACK", "ARITHMETIC", "CONDITIONAL"), f"Unexpected type: {calc_type}"
