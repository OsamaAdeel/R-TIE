"""
B1: assemble_llm_payload must render the EXECUTION CONDITION line from the
key the producer actually writes.

The only execution_condition producer (parser.detect_execution_condition,
parser.py:314-319) emits {"raw_condition", "field", "value", "line_number"}.
Both consumer sites in assemble_llm_payload previously read only
plain_text/description — keys no producer emits — so the line never rendered
for any real graph (the C01/W172 diagnostic: FN_LOAD_OPS_RISK_DATA's
December gate was present on all 14 nodes and silently dropped). B1 makes
the consumer read raw_condition first, keeping plain_text/description as
fallback. No Redis needed — assemble_llm_payload is pure.
"""

from src.parsing.query_engine import assemble_llm_payload

# The real producer shape, verbatim from the C01 diagnostic.
DECEMBER_GATE = {
    "raw_condition": "IF TO_NUMBER (EXTRACT (MONTH FROM TO_DATE (CQD, 'DD-MON-RR'))) = 12",
    "field": "MONTH",
    "value": "12",
    "line_number": 33,
}


def _full_step_entry(exec_cond):
    """A non-pass-through node (mapping transforms the target column), so the
    payload renders the per-node STEP path (consumer site 2)."""
    return {
        "function": "FN_GATED",
        "node": {
            "id": "FN_GATED_N1",
            "type": "UPDATE",
            "target_table": "TBL_X",
            "source_tables": ["SRC_A"],
            "column_maps": {"mapping": {"COL1": "UPPER(SRC_A.COL1)"}},
            "calculation": [],
            "conditions": ["SRC_A.FLAG = 'Y'"],
            "committed_after": True,
            "line_start": 40,
            "line_end": 55,
        },
        "execution_condition": exec_cond,
    }


def _passthrough_entry(node_id, line_start, line_end, exec_cond):
    """Flat column_maps -> _is_passthrough_node True; two consecutive such
    nodes consolidate, exercising consumer site 1."""
    return {
        "function": "FN_PT_GATED",
        "node": {
            "id": node_id,
            "type": "INSERT",
            "target_table": "TBL_X",
            "source_tables": ["SRC_A"],
            "column_maps": {"COL1": "SRC_A.COL1"},
            "calculation": [],
            "conditions": [],
            "committed_after": False,
            "line_start": line_start,
            "line_end": line_end,
        },
        "execution_condition": exec_cond,
    }


def test_raw_condition_renders_on_full_step_path():
    entry = _full_step_entry(DECEMBER_GATE)
    payload = assemble_llm_payload(
        nodes=[entry], edges=[], target_variable="COL1",
        user_query="How is COL1 populated?", execution_order=[entry],
    )
    assert (
        "EXECUTION CONDITION: IF TO_NUMBER (EXTRACT (MONTH FROM TO_DATE (CQD, 'DD-MON-RR'))) = 12"
        in payload
    )
    assert "Operation:" in payload  # still the full-step rendering


def test_raw_condition_renders_on_consolidated_passthrough_path():
    e1 = _passthrough_entry("FN_PT_GATED_N1", 10, 20, DECEMBER_GATE)
    e2 = _passthrough_entry("FN_PT_GATED_N2", 25, 30, DECEMBER_GATE)
    payload = assemble_llm_payload(
        nodes=[e1, e2], edges=[], target_variable="COL1",
        user_query="How is COL1 populated?", execution_order=[e1, e2],
    )
    assert (
        "EXECUTION CONDITION: IF TO_NUMBER (EXTRACT (MONTH FROM TO_DATE (CQD, 'DD-MON-RR'))) = 12"
        in payload
    )
    # B1 must not alter the consolidation itself (B2/W175 territory):
    assert "[PASS-THROUGH]" in payload
    assert "lines 10-30" in payload


def test_no_execution_condition_no_line():
    e_none = _full_step_entry(None)
    e_missing = {k: v for k, v in _full_step_entry(None).items()
                 if k != "execution_condition"}
    e_missing["node"] = dict(e_missing["node"], id="FN_GATED_N2")
    payload = assemble_llm_payload(
        nodes=[e_none, e_missing], edges=[], target_variable="COL1",
        user_query="How is COL1 populated?", execution_order=[e_none, e_missing],
    )
    assert "EXECUTION CONDITION" not in payload


def test_legacy_plain_text_fallback_still_renders():
    entry = _full_step_entry({"plain_text": "Only executes in December"})
    payload = assemble_llm_payload(
        nodes=[entry], edges=[], target_variable="COL1",
        user_query="How is COL1 populated?", execution_order=[entry],
    )
    assert "EXECUTION CONDITION: Only executes in December" in payload


def test_raw_condition_wins_over_legacy_keys():
    entry = _full_step_entry(dict(DECEMBER_GATE, plain_text="legacy text"))
    payload = assemble_llm_payload(
        nodes=[entry], edges=[], target_variable="COL1",
        user_query="How is COL1 populated?", execution_order=[entry],
    )
    assert "IF TO_NUMBER (EXTRACT (MONTH" in payload
    assert "legacy text" not in payload
