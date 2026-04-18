"""Unit tests for src.phase2.proof_builder."""

from src.phase2.proof_builder import ProofBuilder


def _direct_node():
    return {
        "id": "FN_X_N1",
        "type": "INSERT",
        "target_table": "STG_OPS_RISK_DATA",
        "source_tables": ["ABL_OPS_RISK_DATA"],
        "column_maps": {"mapping": {"N_EOP_BAL": "N_EOP_BAL"}},
        "calculation": [],
        "conditions": [],
        "line_start": 10,
        "line_end": 15,
    }


def _override_node():
    return {
        "id": "FN_X_N2",
        "type": "UPDATE",
        "target_table": "STG_OPS_RISK_DATA",
        "source_tables": [],
        "column_maps": {
            "assignments": [["N_EOP_BAL", "CASE WHEN V_GL_CODE = 'X' THEN 0 ELSE N_EOP_BAL END"]],
        },
        "calculation": [{"type": "OVERRIDE", "expression": "CASE WHEN V_GL_CODE = 'X' THEN 0"}],
        "overrides": [{"rule": "V_GL_CODE = 'X'", "value": 0, "line": 42}],
        "conditions": ["V_GL_CODE = 'X'"],
        "line_start": 40,
        "line_end": 50,
    }


def _value_result(value):
    return {
        "status": "found" if value is not None else "empty",
        "rows": [{"N_EOP_BAL": value}] if value is not None else [],
        "row_count": 1 if value is not None else 0,
        "query": "SELECT N_EOP_BAL FROM X WHERE FIC_MIS_DATE = :mis_date",
        "bind_params": {"mis_date": "2025-12-31"},
        "error": None,
    }


def test_build_proof_chain_direct_copy():
    builder = ProofBuilder()
    chain = [
        {"node": _direct_node(), "function": "FN_X",
         "value_result": _value_result(100.0)},
    ]
    result = builder.build_proof_chain([], chain, "N_EOP_BAL")
    assert result["origin_value"] == 100.0
    assert result["final_value"] == 100.0
    assert result["total_delta"] == 0.0
    assert result["confidence"] == 1.0
    assert len(result["steps"]) == 1


def test_build_proof_chain_override_triggered():
    builder = ProofBuilder()
    chain = [
        {"node": _direct_node(), "function": "FN_X",
         "value_result": _value_result(50_000_000.0)},
        {"node": _override_node(), "function": "FN_X",
         "value_result": _value_result(0.0)},
    ]
    result = builder.build_proof_chain([], chain, "N_EOP_BAL")
    assert result["origin_value"] == 50_000_000.0
    assert result["final_value"] == 0.0
    assert result["total_delta"] == -50_000_000.0
    # step 2 is UPDATE -- conditions should be recorded
    step2 = result["steps"][1]
    assert "V_GL_CODE = 'X'" in (step2["conditions_met"] or [""])[0]


def test_compute_expected_output_direct():
    builder = ProofBuilder()
    out = builder.compute_expected_output(_direct_node(), input_value=100.0, filters={})
    assert out["expected_value"] == 100.0
    assert out["calculation_applied"] == "direct copy"
    assert out["override_triggered"] is False


def test_compute_expected_output_arithmetic():
    """ARITHMETIC expression referencing 'input' should be evaluated."""
    builder = ProofBuilder()
    node = {
        "id": "FN_X_N3",
        "type": "UPDATE",
        "calculation": [{"type": "ARITHMETIC", "expression": "input * 1.5"}],
    }
    out = builder.compute_expected_output(node, input_value=100.0, filters={})
    assert out["expected_value"] == 150.0


def test_compute_expected_output_override_no_match():
    """OVERRIDE with filters that don't match returns expected=None."""
    builder = ProofBuilder()
    node = _override_node()
    out = builder.compute_expected_output(node, input_value=100.0,
                                           filters={"gl_code": "Y"})
    assert out["expected_value"] is None


def test_confidence_based_on_data_availability():
    builder = ProofBuilder()
    chain = [
        {"node": _direct_node(), "function": "FN_X",
         "value_result": _value_result(100.0)},
        {"node": _direct_node(), "function": "FN_X",
         "value_result": _value_result(None)},
    ]
    result = builder.build_proof_chain([], chain, "N_EOP_BAL")
    # 1 of 2 steps had data -> 0.5
    assert result["confidence"] == 0.5


def test_summary_mentions_target_variable():
    builder = ProofBuilder()
    chain = [
        {"node": _direct_node(), "function": "FN_X",
         "value_result": _value_result(42.0)},
    ]
    result = builder.build_proof_chain([], chain, "N_EOP_BAL")
    assert "N_EOP_BAL" in result["summary"]
