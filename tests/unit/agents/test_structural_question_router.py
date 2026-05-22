"""W129 — Tests for the structural-question pre-classifier router.

W129 adds a deterministic pre-classifier detector for structural
questions about CODE (functions / batches / jobs / processes / scripts
that operate on data) that were previously misrouted to DATA_QUERY by
the classifier's heuristic match on date-shaped tokens ("December")
or `FCT_*` table references. Without W129 the user gets a "no MIS date
detected" clarification when the question is about code, not values.

Baseline failures: E1 "What runs only in December?" + E2 "What
functions update FCT_STANDARD_ACCT_HEAD?" of
``scratch/quality_harness_report_baseline.md``.

The pipeline-level wiring lives in :mod:`src.main` (pre-LLM block after
W130's W88 hook). These unit tests pin:

- The detector matches E1 and E2 exactly.
- The detector does NOT match positive-arm canaries that must remain
  on their existing routes (DATA_QUERY aggregations, VARIABLE_TRACE
  structural-target queries, UNSUPPORTED forecasting / reconciliation,
  named-function queries, W88 surfaces, W127 calendar surface,
  W121-broad-2 surfaces).
- Suggested route is "COLUMN_LOGIC" for both patterns (validated by
  C3's W127 post-fix routing; VARIABLE_TRACE requires a target_variable
  W129 queries don't have).
- Cross-fix non-interference: W127 / W121-broad-2 / W130 surfaces all
  produce identical behavior post-W129 (this module is additive — adds
  a new helper + new call site; touches no existing detector).
"""

from __future__ import annotations

import pytest

from src.agents.computation_router import detect_named_computation
from src.agents.orchestrator import _extract_unrecognized_term
from src.agents.structural_question_router import (
    W129StructuralMatch,
    detect_structural_question,
)


# ---------------------------------------------------------------------------
# Primary contract — E1 and E2 match the detector
# ---------------------------------------------------------------------------

class TestW129PrimaryContractMatches:
    def test_e1_what_runs_in_december_matches(self):
        match = detect_structural_question("What runs only in December?")
        assert match is not None
        assert match.suggested_route == "COLUMN_LOGIC"

    def test_e1_without_only_qualifier_matches(self):
        """The 'only' qualifier is optional in P2."""
        match = detect_structural_question("What runs in December?")
        assert match is not None
        assert match.suggested_route == "COLUMN_LOGIC"

    def test_e1_executes_variant_matches(self):
        match = detect_structural_question("What executes during December?")
        assert match is not None

    def test_e1_fires_variant_matches(self):
        match = detect_structural_question("What fires on month-end?")
        assert match is not None

    def test_e2_what_functions_update_table_matches(self):
        match = detect_structural_question(
            "What functions update FCT_STANDARD_ACCT_HEAD?",
        )
        assert match is not None
        assert match.suggested_route == "COLUMN_LOGIC"

    def test_e2_singular_function_matches(self):
        match = detect_structural_question(
            "What function updates FCT_STANDARD_ACCT_HEAD?",
        )
        assert match is not None

    def test_e2_which_functions_write_matches(self):
        match = detect_structural_question(
            "Which functions write to FCT_STANDARD_ACCT_HEAD?",
        )
        assert match is not None

    def test_p1_batches_run_matches(self):
        match = detect_structural_question("What batches run nightly?")
        assert match is not None

    def test_p1_code_populates_matches(self):
        match = detect_structural_question(
            "What code populates FCT_OPS_RISK_DATA?",
        )
        assert match is not None

    def test_p1_scripts_reference_matches(self):
        match = detect_structural_question(
            "Which scripts reference CAP160?",
        )
        assert match is not None


# ---------------------------------------------------------------------------
# Regression guards — positive surfaces must remain UNCAUGHT
# ---------------------------------------------------------------------------

class TestW129DoesNotCatchPositiveArms:
    """Every canary that today produces a correct response must remain
    on its current route. The detector must NOT match any of these.
    """

    # ---- DATA_QUERY positive arm ----

    def test_c05_total_aggregation_with_date_not_caught(self):
        assert detect_structural_question(
            "What is the total N_EOP_BAL for V_LV_CODE='ABL' on 2025-12-31?",
        ) is None

    def test_c06_how_many_aggregation_not_caught(self):
        assert detect_structural_question(
            "How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?",
        ) is None

    def test_c07_single_account_value_lookup_not_caught(self):
        assert detect_structural_question(
            "what's the v_prod_code of 601013101-8604 on 2025-12-31?",
        ) is None

    def test_c11_total_aggregation_fct_table_not_caught(self):
        """C11 explicitly admits the bare-FCT_* DATA_QUERY case per the
        classifier prompt at orchestrator.py:339-355. Must not be re-routed."""
        assert detect_structural_question(
            "What is the total N_STD_ACCT_HEAD_AMT in FCT_STANDARD_ACCT_HEAD on 2025-12-31?",
        ) is None

    # ---- VARIABLE_TRACE structural-with-target ----

    def test_c04_what_writes_column_not_caught(self):
        """C04 'What writes N_EOP_BAL?' is structural BUT names a
        column. Classifier puts N_EOP_BAL in target_variable and routes
        VARIABLE_TRACE. W129 must NOT re-route — patterns require a
        code-noun (functions, batches, ...) between 'what' and the verb."""
        assert detect_structural_question("What writes N_EOP_BAL?") is None

    def test_c12_what_writes_other_column_not_caught(self):
        assert detect_structural_question(
            "What writes N_STD_ACCT_HEAD_AMT?",
        ) is None

    # ---- UNSUPPORTED ----

    def test_c14_cross_table_reconciliation_not_caught(self):
        assert detect_structural_question(
            "Why does FCT_PRODUCT_EXPOSURES differ from "
            "STG_PRODUCT_PROCESSOR for account TF1528012748-T24-COLLBLG "
            "on 2025-12-31?",
        ) is None

    def test_c15_forecasting_not_caught(self):
        """C15 forecasting boundary — 'accounts' is not in the code-noun
        list, so this query falls through to the classifier's UNSUPPORTED
        forecasting rule."""
        assert detect_structural_question(
            "Which accounts are likely to fail next quarter?",
        ) is None

    # ---- FUNCTION_LOGIC with named function ----

    def test_c01_named_function_not_caught(self):
        assert detect_structural_question(
            "How does FN_LOAD_OPS_RISK_DATA work?",
        ) is None

    def test_e3_what_feeds_function_not_caught(self):
        """E3 baseline VERIFIED via W76 named-function anchor."""
        assert detect_structural_question(
            "What feeds data into FN_G_TEST_CSTM?",
        ) is None

    def test_e4_what_runs_after_function_not_caught(self):
        """E4: 'after' is not in P2's preposition list (in|on|during|
        when). The named-function path handles E4 today; W129 leaves
        it alone."""
        assert detect_structural_question(
            "What runs after FN_LOAD_OPS_RISK_DATA?",
        ) is None

    # ---- W88 surface (W130 territory) ----

    def test_f1_lcr_not_caught(self):
        """F1 is W130's territory — pre-classifier W88 hook fires
        before W129. But verify the W129 detector also doesn't match
        LCR text, defense in depth."""
        assert detect_structural_question("How is LCR computed?") is None

    def test_f3_leverage_ratio_not_caught(self):
        assert detect_structural_question(
            "What's the Leverage Ratio for this run?",
        ) is None

    def test_cet1_no_date_not_caught(self):
        """The W88b case W130 closes — W129 must not interfere."""
        assert detect_structural_question(
            "What is the CET1 ratio?",
        ) is None

    # ---- W127 surface ----

    def test_c3_december_only_gate_not_caught(self):
        """C3's W127 surface — 'Where' question, not 'What runs'."""
        assert detect_structural_question(
            "Where is the December-only execution gate set?",
        ) is None

    # ---- W121-broad-2 surface ----

    def test_a4_lve_cap_not_caught(self):
        assert detect_structural_question("What's the LVE cap?") is None

    def test_b1_rrp_eligibility_not_caught(self):
        assert detect_structural_question(
            "What enforces RRP eligibility?",
        ) is None


# ---------------------------------------------------------------------------
# Boundary semantics — verb set is narrow, "use" deliberately excluded
# ---------------------------------------------------------------------------

class TestW129VerbSetBoundaries:
    def test_what_functions_use_table_NOT_caught(self):
        """Per Toheed's tightening (Section 1 Ask 1): 'use' was dropped
        from P1's verb list. 'What functions use X?' is too ambiguous
        — might want different routing (FUNCTION_LOGIC for "how is X
        used", VARIABLE_TRACE for "what reads X"). Let classifier
        handle."""
        assert detect_structural_question(
            "What functions use FCT_STANDARD_ACCT_HEAD?",
        ) is None

    def test_what_functions_calls_NOT_caught(self):
        """'calls' is not in the verb list. Function-calling questions
        route via the classifier's normal path."""
        assert detect_structural_question(
            "What functions call FN_LOAD_OPS_RISK_DATA?",
        ) is None


# ---------------------------------------------------------------------------
# Cross-fix non-interference — W127 / W121-broad-2 / W130 surfaces stable
# ---------------------------------------------------------------------------

class TestW129DoesNotRegressPriorFixes:
    """W129 lives in a new module + a new main.py hook. It doesn't
    touch any existing detector. These tests re-pin the prior-PR
    surfaces so future refactor that conflates W129 with W127 /
    W121-broad-2 / W130 logic produces a loud failure.
    """

    def test_w127_december_stopword_unaffected(self):
        result = _extract_unrecognized_term(
            "Where is the December-only execution gate set?", "",
        )
        assert result is None or result.upper() != "DECEMBER"

    def test_w121b2_lve_cap_synthesis_rejection_unaffected(self):
        result = _extract_unrecognized_term(
            "What's the LVE cap?", "LVE_CAP",
        )
        assert result != "LVE_CAP"
        assert result == "LVE" or result is None

    def test_w121b2_verbatim_underscored_target_still_accepted(self):
        result = _extract_unrecognized_term(
            "What is the total N_EOP_BAL for 2025-12-31?", "N_EOP_BAL",
        )
        assert result == "N_EOP_BAL"

    def test_w130_lcr_w88_detection_unaffected(self):
        """W130's W88 detection still matches LCR. W129's detector
        operates in main.py as the else-branch of W130's hook — W88
        wins precedence by construction."""
        match = detect_named_computation(
            raw_query="How is LCR computed?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "LCR"

    def test_w130_cet1_anchor_detection_unaffected(self):
        match = detect_named_computation(
            raw_query="What is the CET1 ratio?",
            query_type="DATA_QUERY",
        )
        assert match is not None
        assert match.definition.name == "CET1"


# ---------------------------------------------------------------------------
# Match-object shape — pin so consumers (logging, telemetry) don't break
# ---------------------------------------------------------------------------

class TestW129MatchShape:
    def test_match_carries_pattern_string(self):
        match = detect_structural_question("What runs only in December?")
        assert match is not None
        assert isinstance(match.pattern, str)
        assert len(match.pattern) > 0

    def test_match_route_is_column_logic(self):
        """Per Section 1 Ask 2: both patterns route to COLUMN_LOGIC."""
        for q in [
            "What runs only in December?",
            "What functions update FCT_STANDARD_ACCT_HEAD?",
        ]:
            match = detect_structural_question(q)
            assert match is not None
            assert match.suggested_route == "COLUMN_LOGIC"

    def test_empty_query_returns_none(self):
        assert detect_structural_question("") is None
        assert detect_structural_question("   ") is None

    def test_non_string_input_returns_none(self):
        assert detect_structural_question(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Frozen dataclass — match is immutable for safety
# ---------------------------------------------------------------------------

class TestW129MatchImmutability:
    def test_match_is_frozen(self):
        match = detect_structural_question("What runs only in December?")
        assert match is not None
        with pytest.raises((AttributeError, Exception)):
            match.suggested_route = "DATA_QUERY"  # type: ignore[misc]
