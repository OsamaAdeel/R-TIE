"""W169: VARIABLE_TRACE framing-drift / scope-violation gate.

The defect (Q12/Q16/Q48): a query naming a function as scope at the end
("how is N_BASEL_ASSET_CLASS_SKEY updated in FN_G_TEST_CSTM") returns
VERIFIED prose attributing the substantive write to a DIFFERENT function
(ABL_INV_ASSET_CLASS_RECLASS), while the correct answer — FN_G_TEST_CSTM's
own writer spans — sits in the same payload's diagram. Check 3a's frequency
heuristic is degenerate in this sparse-citation regime (1-1 tie), so the new
predicate keys on SCOPE (named-set membership against attested writer spans),
not frequency.

Fixtures use the REAL Q12 run-1 prose and the REAL attested writer spans
captured live from the Q12 diagram (W169 diagnostic Probe 2): FN_G_TEST_CSTM
writes the column at [505-598]/[742-841]/[875-915]/[920-963];
ABL_INV_ASSET_CLASS_RECLASS at [32-125]; FN_COR_RW_U2_CSTM at [16-42] — all
legitimate corpus-wide writers. The defect is scope, not fabrication.
"""

from src.agents.logic_explainer import (
    _w169_attribution_drift,
    _w169_cited_line_ranges,
    _w57_check_anchoring,
)
from src.agents.trace_diagram import (
    attested_writers_for_target,
    fan_in_steps_from_tagged_lines,
)


# Real attested-writer spans for N_BASEL_ASSET_CLASS_SKEY (Probe 2 live dump).
Q12_WRITERS = {
    "FN_G_TEST_CSTM": [(505, 598), (742, 841), (875, 915), (920, 963)],
    "ABL_INV_ASSET_CLASS_RECLASS": [(32, 125)],
    "FN_COR_RW_U2_CSTM": [(16, 42)],
}

# Real Q12 run-1 prose (the VERIFIED-0.95 defect output).
Q12_PROSE = (
    "In the function FN_G_TEST_CSTM, the variable N_BASEL_ASSET_CLASS_SKEY is "
    "updated through a MERGE operation in the ABL_INV_ASSET_CLASS_RECLASS step. "
    "Specifically, the value is determined by a CASE statement that checks the "
    "condition `COND_2498138752299_10`. If this condition equals 12, "
    "N_BASEL_ASSET_CLASS_SKEY is set to the value of `EXP_2498138752299_12`; if "
    "it equals 13, it is set to `EXP_2498138752299_13`; otherwise, it defaults "
    "to `EXP_2498138752299_18` (Lines 32-125)."
)


# ===========================================================================
# _w169_attribution_drift — the pure predicate
# ===========================================================================

def test_w169_fires_on_real_q12_scope_drift():
    """Q12: scope=FN_G_TEST_CSTM (an attested writer), prose attributes the
    write to ABL_INV_ASSET_CLASS_RECLASS via 'Lines 32-125' → fire."""
    warning = _w169_attribution_drift(
        ["FN_G_TEST_CSTM"], Q12_WRITERS, Q12_PROSE, {},
    )
    assert warning is not None
    assert "GROUNDING-ANCHOR-SCOPE-DRIFT-HIGH" in warning
    assert "ABL_INV_ASSET_CLASS_RECLASS" in warning
    assert "FN_G_TEST_CSTM" in warning


def test_w169_silent_on_legit_two_function_comparison():
    """Legit comparison: both functions named (in scope), prose cites each
    one's own writer span → no outside-scope attribution → None.
    (Probe 2 §2 confirmed FP-safe.)"""
    prose = (
        "FN_G_TEST_CSTM performs the granularity reclass (Lines 505-598), "
        "while FN_COR_RW_U2_CSTM updates the risk weight (Lines 16-42)."
    )
    assert _w169_attribution_drift(
        ["FN_G_TEST_CSTM", "FN_COR_RW_U2_CSTM"], Q12_WRITERS, prose, {},
    ) is None


def test_w169_silent_on_fan_in_no_named_scope():
    """C12-shape fan-in: no scope named (asked empty) → (i) fails → None."""
    assert _w169_attribution_drift([], Q12_WRITERS, Q12_PROSE, {}) is None


def test_w169_silent_when_named_scope_is_not_a_writer():
    """Named scope X has no attested target-write → (ii) fails → None.
    (That is NAMED_FUNCTION_NOT_RETRIEVED territory, not W169.)"""
    writers = {"ABL_INV_ASSET_CLASS_RECLASS": [(32, 125)]}
    assert _w169_attribution_drift(
        ["FN_G_TEST_CSTM"], writers, Q12_PROSE, {},
    ) is None


def test_w169_line_range_inside_anchor_span_is_on_scope():
    """Prose cites a range INSIDE the anchor's own writer span → owner is the
    named scope → no drift → None."""
    prose = "In FN_G_TEST_CSTM the value is set by the MERGE (Lines 510-540)."
    assert _w169_attribution_drift(
        ["FN_G_TEST_CSTM"], Q12_WRITERS, prose, {},
    ) is None


def test_w169_line_range_inside_outside_span_fires():
    """Prose cites a range INSIDE an outside-scope writer's span → fire."""
    prose = "The update happens here (Lines 40-50)."
    warning = _w169_attribution_drift(
        ["FN_G_TEST_CSTM"], Q12_WRITERS, prose, {},
    )
    assert warning is not None
    assert "ABL_INV_ASSET_CLASS_RECLASS" in warning


def test_w169_fail_safe_silence_no_structured_attribution():
    """Prose attributes to nothing structured (no line range, no prose fn
    name) → fail-safe None rather than a guess."""
    prose = "The column is updated somewhere during processing."
    assert _w169_attribution_drift(
        ["FN_G_TEST_CSTM"], Q12_WRITERS, prose, {},
    ) is None


def test_w169_prose_name_fallback_fires():
    """No usable line range, but the prose names an outside-scope attested
    writer in a form the structured extractor captures (markdown heading) →
    priority-2 fallback fires."""
    prose = (
        "### ABL_INV_ASSET_CLASS_RECLASS\n"
        "The column is handled here during the reclassification."
    )
    warning = _w169_attribution_drift(
        ["FN_G_TEST_CSTM"], Q12_WRITERS, prose, {},
    )
    assert warning is not None
    assert "ABL_INV_ASSET_CLASS_RECLASS" in warning


def test_w169_empty_attested_writers_is_noop():
    """No structured signal (non-VARIABLE_TRACE) → None."""
    assert _w169_attribution_drift(["FN_G_TEST_CSTM"], {}, Q12_PROSE, {}) is None


# ===========================================================================
# _w169_cited_line_ranges
# ===========================================================================

def test_cited_line_ranges_parses_range_and_single():
    md = "see (Lines 32-125) and also Line 47 and L505."
    ranges = _w169_cited_line_ranges(md)
    assert (32, 125) in ranges
    assert (47, 47) in ranges
    assert (505, 505) in ranges


def test_cited_line_ranges_drops_degenerate():
    md = "reversed (Lines 200-100) and huge (Lines 1-9000)."
    assert _w169_cited_line_ranges(md) == []


# ===========================================================================
# _w57_check_anchoring route-split
# ===========================================================================

def test_3a_variable_trace_uses_w169_predicate():
    """With attested_writers present (VARIABLE_TRACE), 3a fires W169 on the
    Q12 scope-drift even though the frequency rule would NOT (1-1 tie)."""
    warnings = _w57_check_anchoring(
        "how is N_BASEL_ASSET_CLASS_SKEY updated in FN_G_TEST_CSTM",
        list(Q12_WRITERS.keys()),  # functions_analyzed cohort (multi-fn)
        Q12_PROSE,
        attested_writers=Q12_WRITERS,
    )
    assert any("GROUNDING-ANCHOR-SCOPE-DRIFT-HIGH" in w for w in warnings)


def test_3a_without_attested_writers_keeps_frequency_rule():
    """No attested_writers (FUNCTION_LOGIC/COLUMN_LOGIC): the legacy
    frequency dominance rule still fires (Run-3 C2 protection intact)."""
    md = " ".join(["CAP_CONSL_EFFECTIVE"] * 100) + " OPS_RISK_DATA_POPULATION_CSTM"
    warnings = _w57_check_anchoring(
        "How does OPS_RISK_DATA_POPULATION_CSTM work?",
        ["OPS_RISK_DATA_POPULATION_CSTM", "CAP_CONSL_EFFECTIVE"],
        md,
    )
    assert any("primarily cites" in w for w in warnings)


def test_3a_variable_trace_silent_on_legit_comparison():
    """VARIABLE_TRACE with attested_writers: a legit two-function comparison
    does NOT fire W169 (both in scope)."""
    prose = (
        "FN_G_TEST_CSTM performs the granularity reclass (Lines 505-598), "
        "while FN_COR_RW_U2_CSTM updates the risk weight (Lines 16-42)."
    )
    warnings = _w57_check_anchoring(
        "compare FN_G_TEST_CSTM and FN_COR_RW_U2_CSTM",
        list(Q12_WRITERS.keys()),
        prose,
        attested_writers=Q12_WRITERS,
    )
    assert warnings == []


# ===========================================================================
# attested_writers_for_target accessor (graph-first, tagged fallback)
# ===========================================================================

def test_accessor_empty_when_no_sources():
    assert attested_writers_for_target("N_X", None, None, None) == {}


def test_accessor_from_tagged_lines_fallback():
    """No graph; tagged lines carry a MERGE writer → mapped to its span."""
    tagged = [
        {"function": "FN_A", "line": 10, "text": "MERGE INTO T",
         "operation": "MERGE", "aliases_matched": ["N_X"], "commented": False},
        {"function": "FN_A", "line": 11, "text": "N_X = 1",
         "operation": "MERGE", "aliases_matched": ["N_X"], "commented": False},
    ]
    writers = attested_writers_for_target("N_X", None, tagged, None)
    assert "FN_A" in writers
    # coalesced into one span by the gap rule
    assert writers["FN_A"][0][0] == 10


def test_accessor_graph_takes_precedence_over_tagged():
    """When vt_graph yields steps, the tagged fallback is not consulted."""
    graph_nodes = [
        {"function": "FN_G", "node": {
            "id": "N1", "type": "MERGE", "line_start": 505, "line_end": 598,
            "column_maps": {"mapping": {"N_X": "expr"}}}},
    ]
    tagged = [
        {"function": "FN_T", "line": 10, "text": "MERGE", "operation": "MERGE",
         "aliases_matched": ["N_X"], "commented": False},
    ]
    writers = attested_writers_for_target(
        "N_X", graph_nodes, tagged, None,  # multi_source=None → no cohort filter
    )
    assert "FN_G" in writers
    assert "FN_T" not in writers
    assert writers["FN_G"] == [(505, 598)]
