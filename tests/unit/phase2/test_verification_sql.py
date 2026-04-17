"""Unit tests for src.phase2.verification_sql."""

from src.phase2.verification_sql import VerificationSQLGenerator


def _step(n, sql, out=None, source="FN_X.sql, line 10"):
    return {
        "step_number": n,
        "transformation": f"Step {n} description",
        "operation": "UPDATE",
        "output_value": out,
        "row_count": 1 if out is not None else 0,
        "formula": "",
        "source_ref": source,
        "query": sql,
    }


def test_generate_step_verification_uses_bind_vars():
    gen = VerificationSQLGenerator()
    sql = (
        "SELECT N_EOP_BAL, FIC_MIS_DATE\n"
        "FROM STG_OPS_RISK_DATA\n"
        "WHERE FIC_MIS_DATE = :mis_date AND V_ACCOUNT_NUMBER = :account_number\n"
        "FETCH FIRST 100 ROWS ONLY"
    )
    step = _step(1, sql, out=1000.0)
    filters = {"mis_date": "2025-12-31", "account_number": "LD1", "lob_code": None}

    result = gen.generate_step_verification(step, filters)
    assert ":mis_date" in result["sql"]
    assert ":account_number" in result["sql"]
    assert result["bind_params"] == {"mis_date": "2025-12-31", "account_number": "LD1"}
    assert result["expected_result"].startswith("~")


def test_generate_full_script_all_steps():
    gen = VerificationSQLGenerator()
    proof_chain = {
        "target_variable": "N_EOP_BAL",
        "steps": [
            _step(1, "SELECT 1 FROM DUAL", out=100.0),
            _step(2, "SELECT 2 FROM DUAL", out=200.0),
            _step(3, "SELECT 3 FROM DUAL", out=300.0),
            _step(4, "SELECT 4 FROM DUAL", out=400.0),
        ],
    }
    filters = {"mis_date": "2025-12-31", "account_number": "LD1"}

    script = gen.generate_full_verification_script(proof_chain, filters)
    # Header present
    assert "RTIE VERIFICATION SCRIPT" in script
    assert "N_EOP_BAL" in script
    assert ":mis_date" in script or "mis_date" in script
    # Each step present
    for i in range(1, 5):
        assert f"-- Step {i}" in script
    # Every SQL terminates with semicolon
    assert script.count(";") >= 4


def test_expected_result_for_empty_step():
    gen = VerificationSQLGenerator()
    step = _step(1, "SELECT 1 FROM DUAL", out=None)
    result = gen.generate_step_verification(step, {"mis_date": "2025-12-31"})
    assert "0 rows" in result["expected_result"]


def test_full_script_with_empty_chain():
    gen = VerificationSQLGenerator()
    script = gen.generate_full_verification_script(
        {"target_variable": "X", "steps": []},
        {"mis_date": "2025-12-31"},
    )
    assert "no steps resolved" in script
