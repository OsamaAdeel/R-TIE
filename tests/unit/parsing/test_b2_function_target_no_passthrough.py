"""
B2: assemble_llm_payload must not apply pass-through CONSOLIDATION when the
target is the FUNCTION being walked through (target_is_function=True).

_is_passthrough_node answers a column-trace question ("is the target COLUMN
copied unchanged"); against a function-name target every node degenerates to
True (no assignment column equals a function name), so pre-B2 the whole
function flattened to one "[PASS-THROUGH] ... not transformed ..." block —
the C01/W172 regression (and C19's ~24% UNVERIFIED residual). B2 threads the
binding kind (g_query_type == "function") from main.py into
assemble_llm_payload and skips consolidation for function targets only.
Column-target behavior (colprov / the W175 genuine pass-through case) must
stay byte-identical, including with the default parameter.
"""

from src.parsing.query_engine import assemble_llm_payload

DECEMBER_GATE = {
    "raw_condition": "IF TO_NUMBER (EXTRACT (MONTH FROM TO_DATE (CQD, 'DD-MON-RR'))) = 12",
    "field": "MONTH",
    "value": "12",
    "line_number": 33,
}


def _fn_target_entry(node_id, line_start, line_end, exec_cond=None,
                     node_type="INSERT", conditions=None):
    """C01's real shape: function-target nodes whose column_maps never
    mention the function name -> degenerately pass-through pre-B2."""
    return {
        "function": "FN_GATED",
        "node": {
            "id": node_id,
            "type": node_type,
            "target_table": "STG_OPS_RISK_DATA",
            "source_tables": ["ABL_OPS_RISK_DATA"],
            "column_maps": {},
            "calculation": [],
            "conditions": conditions or [],
            "committed_after": True,
            "line_start": line_start,
            "line_end": line_end,
        },
        "execution_condition": exec_cond,
    }


def _column_pt_entry(node_id, line_start, line_end):
    """The W175 colprov shape: flat column_maps copying the column unchanged
    -> genuinely pass-through w.r.t. a COLUMN target (test 15b shape)."""
    return {
        "function": "TLX_OPS_ADJ_MISDATE",
        "node": {
            "id": node_id,
            "type": "INSERT",
            "target_table": "STG_OPS_ADJ_MISDATE_TLX",
            "source_tables": ["STG_OPS_RISK_DATA"],
            "column_maps": {"N_ANNUAL_GROSS_INCOME": "N_ANNUAL_GROSS_INCOME"},
            "calculation": [],
            "conditions": [],
            "committed_after": False,
            "line_start": line_start,
            "line_end": line_end,
        },
        "execution_condition": None,
    }


def _assemble(entries, target, **kwargs):
    return assemble_llm_payload(
        nodes=entries, edges=[], target_variable=target,
        user_query=f"How is {target} handled?", execution_order=entries,
        **kwargs,
    )


def test_function_target_renders_per_node_steps_not_passthrough():
    entries = [
        _fn_target_entry("FN_GATED_N1", 198, 199, node_type="DELETE",
                         conditions=["FIC_MIS_DATE = CQD"]),
        _fn_target_entry("FN_GATED_N2", 203, 222),
    ]
    payload = _assemble(entries, "FN_GATED", target_is_function=True)
    assert "[PASS-THROUGH]" not in payload
    assert "not transformed" not in payload
    assert "date-adjusts historical records" not in payload
    assert payload.count("Operation:") == 2
    assert "Operation: DELETE" in payload
    assert "FIC_MIS_DATE = CQD" in payload


def test_function_target_without_flag_keeps_today_behavior():
    # Callers not passing target_is_function get the pre-B2 (consolidated)
    # output — the default must not shift anything by itself.
    entries = [
        _fn_target_entry("FN_GATED_N1", 198, 199),
        _fn_target_entry("FN_GATED_N2", 203, 222),
    ]
    default_payload = _assemble(entries, "FN_GATED")
    explicit_false = _assemble(entries, "FN_GATED", target_is_function=False)
    assert default_payload == explicit_false
    assert "[PASS-THROUGH]" in default_payload
    assert "not transformed" in default_payload


def test_column_target_genuine_passthrough_still_consolidates():
    # The W175 colprov case: a COLUMN copied unchanged through consecutive
    # same-function nodes must STILL consolidate (byte-identical behavior).
    entries = [
        _column_pt_entry("TLX_N1", 54, 120),
        _column_pt_entry("TLX_N2", 130, 349),
    ]
    payload = _assemble(entries, "N_ANNUAL_GROSS_INCOME")
    assert "[PASS-THROUGH]" in payload
    assert "Copies N_ANNUAL_GROSS_INCOME unchanged through" in payload
    assert "The value is not transformed -- this function date-adjusts historical records." in payload
    assert "lines 54-349" in payload
    assert "Operation:" not in payload


def test_function_target_keeps_b1_execution_condition_line():
    entries = [
        _fn_target_entry("FN_GATED_N1", 198, 199, exec_cond=DECEMBER_GATE),
        _fn_target_entry("FN_GATED_N2", 203, 222, exec_cond=DECEMBER_GATE),
    ]
    payload = _assemble(entries, "FN_GATED", target_is_function=True)
    assert (
        "EXECUTION CONDITION: IF TO_NUMBER (EXTRACT (MONTH FROM TO_DATE (CQD, 'DD-MON-RR'))) = 12"
        in payload
    )
    assert "[PASS-THROUGH]" not in payload
