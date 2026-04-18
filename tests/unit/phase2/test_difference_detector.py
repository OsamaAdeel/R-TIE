"""Unit tests for src.phase2.difference_detector."""

from src.phase2.difference_detector import DifferenceDetector


def _step(
    step_number,
    output_value,
    expected_output=None,
    node_type="UPDATE",
    overrides=None,
    data_available=True,
    row_count=1,
    conditions=None,
    formula="",
):
    return {
        "step_number": step_number,
        "function": "FN_X",
        "operation": node_type,
        "node_type": node_type,
        "node_id": f"FN_X_N{step_number}",
        "input_value": None,
        "output_value": output_value,
        "expected_output": expected_output,
        "transformation": f"Step {step_number}",
        "formula": formula,
        "conditions_met": conditions or [],
        "overrides_triggered": overrides or [],
        "data_available": data_available,
        "row_count": row_count,
        "notes": "",
        "source_ref": f"FN_X.sql, line {step_number * 10}",
        "query": "SELECT ...",
    }


def test_detect_delta_at_override():
    """If step 2 has an override and actual diverges, OVERRIDE is reported."""
    detector = DifferenceDetector()
    proof_chain = {
        "target_variable": "N_EOP_BAL",
        "steps": [
            _step(1, 50_000_000.0, expected_output=50_000_000.0),
            _step(2, 0.0, expected_output=50_000_000.0,
                  overrides=[{"rule": "V_GL_CODE = 'X'"}]),
        ],
    }
    result = detector.detect_delta_source(
        proof_chain, expected_value=50_000_000.0, actual_value=0.0,
    )
    assert result["cause_type"] == "OVERRIDE"
    assert result["root_cause_step"] == 2
    assert "V_GL_CODE = 'X'" in result["evidence"]["triggering_rule"]


def test_detect_delta_at_missing_data():
    """No rows at step 1 with expected>actual -> MISSING_DATA."""
    detector = DifferenceDetector()
    proof_chain = {
        "target_variable": "N_EOP_BAL",
        "steps": [
            _step(1, None, data_available=False, row_count=0),
        ],
    }
    result = detector.detect_delta_source(
        proof_chain, expected_value=100.0, actual_value=0.0,
    )
    assert result["cause_type"] == "MISSING_DATA"
    assert result["root_cause_step"] == 1


def test_no_delta_within_tolerance():
    """Tiny difference below tolerance is reported as NONE."""
    detector = DifferenceDetector()
    proof_chain = {
        "target_variable": "N_EOP_BAL",
        "steps": [_step(1, 100.005, expected_output=100.0)],
    }
    result = detector.detect_delta_source(
        proof_chain, expected_value=100.0, actual_value=100.005, tolerance=0.01,
    )
    assert result["cause_type"] == "NONE"
    assert result["delta"] == 0.0
    assert result["root_cause_step"] is None


def test_detect_delta_calculation():
    """UPDATE without overrides but with a formula -> CONDITION (it has conditions)
    or CALCULATION. Asserts cause_type is one of those and root step is correct."""
    detector = DifferenceDetector()
    proof_chain = {
        "target_variable": "N_EOP_BAL",
        "steps": [
            _step(1, 100.0, expected_output=100.0),
            _step(2, 50.0, expected_output=100.0, formula="input * 0.5",
                  conditions=["V_LOB_CODE = 'CBA'"]),
        ],
    }
    result = detector.detect_delta_source(
        proof_chain, expected_value=100.0, actual_value=50.0,
    )
    assert result["root_cause_step"] == 2
    assert result["cause_type"] in ("CONDITION", "CALCULATION")


def test_delta_percent_computed():
    detector = DifferenceDetector()
    proof_chain = {"target_variable": "X", "steps": []}
    result = detector.detect_delta_source(
        proof_chain, expected_value=100.0, actual_value=80.0,
    )
    assert abs(result["delta_percent"] - 20.0) < 0.001
