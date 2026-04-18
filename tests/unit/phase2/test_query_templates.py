"""Unit tests for src.phase2.query_templates."""

import pytest

from src.phase2.query_templates import (
    determine_template,
    generate_query,
)


def _make_insert_node():
    return {
        "id": "FN_X_N1",
        "type": "INSERT",
        "target_table": "STG_OPS_RISK_DATA",
        "source_tables": ["ABL_OPS_RISK_DATA"],
        "column_maps": {
            "columns": [],
            "values": [],
            "mapping": {"N_ANNUAL_GROSS_INCOME": "N_ANNUAL_GROSS_INCOME"},
        },
        "calculation": [],
        "conditions": [],
    }


def _make_update_node():
    return {
        "id": "FN_X_N2",
        "type": "UPDATE",
        "target_table": "STG_OPS_RISK_DATA",
        "source_tables": [],
        "column_maps": {
            "assignments": [["N_ANNUAL_GROSS_INCOME", "CASE WHEN OPS.V_LOB_CODE = 'CBA' THEN 100 ELSE 0 END"]],
        },
        "calculation": [],
        "conditions": ["OPS.V_LOB_CODE IN ('CBA', 'RBA')"],
    }


def _make_scalar_node():
    return {
        "id": "FN_X_N3",
        "type": "SCALAR_COMPUTE",
        "target_table": None,
        "source_tables": ["STG_GL_DATA"],
        "column_maps": {},
        "calculation": [
            {
                "column": "CBA_DEDUCTION",
                "type": "DIRECT",
                "expression": "SUM(GD.N_AMOUNT_ACY)",
            }
        ],
        "conditions": ["GD.V_LV_CODE = 'ABL'"],
        "output_variable": "CBA_DEDUCTION",
    }


def test_determine_template_insert_node():
    assert determine_template(_make_insert_node(), "trace_final") == "INSERT_TARGET"


def test_determine_template_update_node():
    assert determine_template(_make_update_node(), "trace_final") == "UPDATE_TARGET"


def test_determine_template_scalar_compute():
    assert determine_template(_make_scalar_node(), "trace_final") == "SCALAR_COMPUTE"


def test_determine_template_source_intent():
    # trace_source wins regardless of node type
    assert determine_template(_make_insert_node(), "trace_source") == "UPSTREAM_SOURCE"


def test_generate_query_uses_bind_variables_only():
    """Filter values must go into bind_params, never into the SQL string."""
    node = _make_insert_node()
    filters = {
        "mis_date": "2025-12-31",
        "account_number": "LD1323300008",
        "lob_code": "CBA",
    }
    sql, params = generate_query(node, filters, template_name="INSERT_TARGET",
                                  target_column="N_ANNUAL_GROSS_INCOME")

    # Filter values are NOT interpolated into SQL.
    assert "2025-12-31" not in sql
    assert "LD1323300008" not in sql
    assert "'CBA'" not in sql

    # Placeholders ARE present.
    assert ":mis_date" in sql
    assert ":account_number" in sql
    assert ":lob_code" in sql

    # Bind params carry the values.
    assert params["mis_date"] == "2025-12-31"
    assert params["account_number"] == "LD1323300008"
    assert params["lob_code"] == "CBA"


def test_generate_query_omits_missing_filters():
    """Missing filter values are simply skipped; their placeholders must
    not appear in SQL or params."""
    node = _make_insert_node()
    sql, params = generate_query(node, {"mis_date": "2025-12-31"},
                                  template_name="INSERT_TARGET",
                                  target_column="N_ANNUAL_GROSS_INCOME")
    assert ":account_number" not in sql
    assert "account_number" not in params


def test_generate_query_update_target():
    node = _make_update_node()
    sql, params = generate_query(node, {"mis_date": "2025-12-31"},
                                  template_name="UPDATE_TARGET",
                                  target_column="N_ANNUAL_GROSS_INCOME")
    assert "STG_OPS_RISK_DATA" in sql
    assert "N_ANNUAL_GROSS_INCOME" in sql
    assert ":mis_date" in sql


def test_generate_query_scalar_compute_uses_expression():
    node = _make_scalar_node()
    sql, params = generate_query(node, {"mis_date": "2025-12-31", "lv_code": "ABL"},
                                  template_name="SCALAR_COMPUTE")
    assert "SUM(GD.N_AMOUNT_ACY)" in sql
    assert "CBA_DEDUCTION" in sql
    assert "STG_GL_DATA" in sql
    assert params["mis_date"] == "2025-12-31"
    assert params["lv_code"] == "ABL"


def test_generate_query_rejects_invalid_identifier():
    """Malformed identifiers on a node are rejected rather than interpolated."""
    bad_node = {
        "id": "X",
        "type": "INSERT",
        "target_table": "STG; DROP TABLE X",
        "source_tables": [],
        "column_maps": {},
        "calculation": [],
        "conditions": [],
    }
    with pytest.raises(ValueError):
        generate_query(bad_node, {"mis_date": "2025-12-31"},
                       template_name="INSERT_TARGET",
                       target_column="N_EOP_BAL")


def test_generate_query_fetch_first_limit():
    node = _make_insert_node()
    sql, _ = generate_query(node, {"mis_date": "2025-12-31"},
                             template_name="INSERT_TARGET",
                             target_column="N_EOP_BAL",
                             fetch_limit=50)
    assert "FETCH FIRST 50 ROWS ONLY" in sql
