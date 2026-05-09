"""W76 — named-function anchor pre-rule.

When the raw query starts with "In <FunctionName>, ..." (or Inside /
Within / possessive variants), the orchestrator must anchor the
asked-about object on that function regardless of what the classifier
returned for ``target_variable`` / ``object_name``.

Defends against the v2 benchmark Run 7 B1 regression: prompt
"In CS_Deferred_Tax_Asset_Net_of_DTL_Calculation, when does the EXP_11
branch fire?" was misrouted as VARIABLE_TRACE with target=EXP_11 (a
CASE-branch alias literal), causing semantic search to return unrelated
functions and the body to fire NAMED_FUNCTION_NOT_RETRIEVED.

Two composed mechanisms:
  Mechanism 1 — explicit prefix anchor ("In <Function>, ...").
  Mechanism 2 — alias-literal in target_variable + real function
                candidate elsewhere in the query.
"""

from __future__ import annotations

from src.agents.orchestrator import (
    Orchestrator,
    detect_named_function_anchor,
)


# ---------------------------------------------------------------------------
# detect_named_function_anchor — pure regex + W58 exclusion gates
# ---------------------------------------------------------------------------


class TestDetectNamedFunctionAnchor:
    """The pure-function anchor detector: prefix patterns + W58 gates."""

    def test_in_prefix_with_comma_b1_canary(self):
        """The exact B1 prompt from Run 7 — must anchor on the named
        function, not the EXP_11 alias literal that follows."""
        assert detect_named_function_anchor(
            "In CS_Deferred_Tax_Asset_Net_of_DTL_Calculation, when does "
            "the EXP_11 branch fire, and what does it set?"
        ) == "CS_Deferred_Tax_Asset_Net_of_DTL_Calculation"

    def test_within_prefix_with_comma(self):
        assert detect_named_function_anchor(
            "Within FN_LOAD_OPS_RISK_DATA, how is N_EOP_BAL set?"
        ) == "FN_LOAD_OPS_RISK_DATA"

    def test_inside_prefix_with_comma(self):
        assert detect_named_function_anchor(
            "Inside ABL_CAP_MITIGANT_DATA_POPULATION, what does EXP_11 represent?"
        ) == "ABL_CAP_MITIGANT_DATA_POPULATION"

    def test_in_the_function_form(self):
        assert detect_named_function_anchor(
            "In the function FN_LOAD_OPS_RISK_DATA, when does EXP_11 fire?"
        ) == "FN_LOAD_OPS_RISK_DATA"

    def test_within_the_function_form(self):
        assert detect_named_function_anchor(
            "Within the function FN_LOAD_OPS_RISK_DATA, how is N_EOP_BAL set?"
        ) == "FN_LOAD_OPS_RISK_DATA"

    def test_possessive_form(self):
        """<NAME>'s ... — "CS_Goodwill_Calculation's EXP_11 branch"."""
        assert detect_named_function_anchor(
            "CS_Goodwill_Calculation's EXP_11 branch"
        ) == "CS_Goodwill_Calculation"

    def test_question_word_suffix_no_comma(self):
        """No comma but a question word follows — still a clear anchor."""
        assert detect_named_function_anchor(
            "Within FN_LOAD_OPS_RISK_DATA when does EXP_11 fire?"
        ) == "FN_LOAD_OPS_RISK_DATA"

    def test_case_insensitive_keyword(self):
        """The trigger keyword is case-insensitive."""
        assert detect_named_function_anchor(
            "IN FN_LOAD_OPS_RISK_DATA, how is N_EOP_BAL set?"
        ) == "FN_LOAD_OPS_RISK_DATA"
        assert detect_named_function_anchor(
            "within FN_LOAD_OPS_RISK_DATA, how is N_EOP_BAL set?"
        ) == "FN_LOAD_OPS_RISK_DATA"

    def test_t2t_function_passes_alias_filter(self):
        """T2T_* function names start with letters+digit — must not be
        caught by the W58.b T_<digit> internal-alias regex."""
        assert detect_named_function_anchor(
            "In T2T_FCT_CCP_DETAILS_STD_ACCT_HEAD_POP, what gets populated?"
        ) == "T2T_FCT_CCP_DETAILS_STD_ACCT_HEAD_POP"

    # --- Negative cases — anchor must NOT fire --------------------------

    def test_normal_query_no_prefix_returns_none(self):
        """No 'In/Within/Inside' prefix — normal classifier path applies."""
        assert detect_named_function_anchor(
            "How does FN_LOAD_OPS_RISK_DATA work?"
        ) is None

    def test_excludes_table_prefix_w58a(self):
        """FCT_/STG_/DIM_/FSI_/SETUP_/AAI_ are tables — W58.a exclusion."""
        assert detect_named_function_anchor(
            "In FCT_OPS_RISK_DATA, what columns are populated?"
        ) is None

    def test_excludes_alias_literal_w58b(self):
        """EXP_<digit> is an internal alias — W58.b exclusion."""
        assert detect_named_function_anchor(
            "In EXP_11, when does it fire?"
        ) is None

    def test_excludes_cond_alias_literal_w58b(self):
        assert detect_named_function_anchor(
            "Inside COND_10, what is the predicate?"
        ) is None

    def test_excludes_column_prefix_w58c(self):
        """N_/V_/F_/D_ prefixed names are columns — W58.c exclusion."""
        assert detect_named_function_anchor(
            "In N_ANNUAL_GROSS_INCOME, what is the value?"
        ) is None

    def test_excludes_short_token_no_underscore(self):
        """Bare uppercase keywords (no underscore) never name a function."""
        assert detect_named_function_anchor(
            "In SELECTOR, what does it do?"
        ) is None

    def test_empty_query_returns_none(self):
        assert detect_named_function_anchor("") is None

    def test_in_mid_sentence_does_not_anchor(self):
        """The anchor pattern is start-of-query only. A mid-sentence 'in
        X' should not trigger — that's the classifier's territory."""
        assert detect_named_function_anchor(
            "How does it work in FN_LOAD_OPS_RISK_DATA, exactly?"
        ) is None


# ---------------------------------------------------------------------------
# apply_named_function_anchor — Mechanism 1 (prefix anchor)
# ---------------------------------------------------------------------------


def _baseline_state(raw_query: str, **overrides) -> dict:
    """Build a minimal state dict in the shape apply_named_function_anchor
    reads. Mimics what classify_query would have populated."""
    state = {
        "raw_query": raw_query,
        "query_type": "VARIABLE_TRACE",
        "object_name": "enriched query blob ...",
        "target_variable": "EXP_11",
        "schema": "OFSMDM",
    }
    state.update(overrides)
    return state


class TestApplyNamedFunctionAnchorMechanism1:
    """Mechanism 1: explicit "In <Function>, ..." prefix overrides the
    classifier's verdict."""

    def test_b1_canary_overrides_object_name_and_query_type(self):
        """B1 from Run 7: classifier returned VARIABLE_TRACE with
        target=EXP_11. W76 must override to anchor on the named function."""
        orch = Orchestrator()
        state = _baseline_state(
            "In CS_Deferred_Tax_Asset_Net_of_DTL_Calculation, when does "
            "the EXP_11 branch fire, and what does it set?"
        )
        orch.apply_named_function_anchor(state)

        assert state["object_name"] == "CS_Deferred_Tax_Asset_Net_of_DTL_Calculation"
        assert state["query_type"] == "COLUMN_LOGIC"
        # Alias literal cleared — it's a sub-target inside the function
        # body, not a top-level column to chase globally.
        assert state["target_variable"] == ""

        # Diagnostic stamp.
        anchor = state["w76_anchor"]
        assert anchor["function"] == \
            "CS_Deferred_Tax_Asset_Net_of_DTL_Calculation"
        assert anchor["source"] == "prefix"
        assert anchor["original_query_type"] == "VARIABLE_TRACE"
        assert anchor["original_target_variable"] == "EXP_11"

    def test_preserves_column_logic_query_type(self):
        """When the classifier already returned COLUMN_LOGIC, leave it
        alone — only override object_name."""
        orch = Orchestrator()
        state = _baseline_state(
            "In CS_Goodwill_Calculation, what does it set?",
            query_type="COLUMN_LOGIC",
            target_variable="",
        )
        orch.apply_named_function_anchor(state)
        assert state["object_name"] == "CS_Goodwill_Calculation"
        assert state["query_type"] == "COLUMN_LOGIC"

    def test_preserves_function_logic_query_type(self):
        """FUNCTION_LOGIC (forward-compatible alias) is also left alone."""
        orch = Orchestrator()
        state = _baseline_state(
            "In FN_LOAD_OPS_RISK_DATA, what does it set?",
            query_type="FUNCTION_LOGIC",
            target_variable="",
        )
        orch.apply_named_function_anchor(state)
        assert state["object_name"] == "FN_LOAD_OPS_RISK_DATA"
        assert state["query_type"] == "FUNCTION_LOGIC"

    def test_does_not_clear_real_column_target_variable(self):
        """Don't clear target_variable when it isn't an alias literal —
        a real column reference should be preserved as a sub-target."""
        orch = Orchestrator()
        state = _baseline_state(
            "Within FN_LOAD_OPS_RISK_DATA, how is N_EOP_BAL set?",
            target_variable="N_EOP_BAL",
        )
        orch.apply_named_function_anchor(state)
        assert state["object_name"] == "FN_LOAD_OPS_RISK_DATA"
        assert state["target_variable"] == "N_EOP_BAL"

    def test_no_op_for_normal_function_query(self):
        """Queries without an anchor prefix go through unchanged. The
        classifier's output is left as-is so the normal routing path
        applies."""
        orch = Orchestrator()
        state = _baseline_state(
            "How does FN_LOAD_OPS_RISK_DATA work?",
            query_type="COLUMN_LOGIC",
            object_name="enriched ...",
            target_variable="",
        )
        orch.apply_named_function_anchor(state)
        assert state["object_name"] == "enriched ..."
        assert state["query_type"] == "COLUMN_LOGIC"
        assert state["target_variable"] == ""
        assert "w76_anchor" not in state


# ---------------------------------------------------------------------------
# apply_named_function_anchor — Mechanism 2 (alias-literal fallback)
# ---------------------------------------------------------------------------


class TestApplyNamedFunctionAnchorMechanism2:
    """Mechanism 2: no prefix anchor, but the classifier put an alias
    literal in target_variable. Try to recover the enclosing function
    from elsewhere in the query body."""

    def test_recovers_function_from_query_body(self):
        """No 'In <X>, ...' prefix, but the classifier put EXP_11 in
        target_variable AND the user mentioned a real function elsewhere.
        M2 anchors on the function from the query body."""
        orch = Orchestrator()
        state = _baseline_state(
            "When does EXP_11 fire in FN_LOAD_OPS_RISK_DATA?",
            target_variable="EXP_11",
        )
        orch.apply_named_function_anchor(state)
        assert state["object_name"] == "FN_LOAD_OPS_RISK_DATA"
        assert state["query_type"] == "COLUMN_LOGIC"
        assert state["target_variable"] == ""
        assert state["w76_anchor"]["source"] == "alias_fallback"

    def test_clears_alias_when_no_function_in_query(self):
        """Alias literal in target_variable AND no recoverable function —
        clear target_variable so the variable tracer doesn't chase EXP_11
        globally. Don't otherwise rewrite state."""
        orch = Orchestrator()
        state = _baseline_state(
            "When does EXP_11 fire?",
            target_variable="EXP_11",
            object_name="enriched ...",
            query_type="VARIABLE_TRACE",
        )
        orch.apply_named_function_anchor(state)
        assert state["target_variable"] == ""
        # object_name and query_type unchanged — caller decides what to
        # do with the cleared target. W57 catches surface
        # NAMED_FUNCTION_NOT_RETRIEVED downstream.
        assert state["object_name"] == "enriched ..."
        assert state["query_type"] == "VARIABLE_TRACE"
        anchor = state["w76_anchor"]
        assert anchor["function"] == ""
        assert anchor["alias_literal_cleared"] == "EXP_11"

    def test_does_not_fire_for_real_column_target(self):
        """target_variable=N_EOP_BAL is a real column, not an alias —
        don't apply M2. The variable tracer can do its normal work."""
        orch = Orchestrator()
        state = _baseline_state(
            "How is N_EOP_BAL set?",
            target_variable="N_EOP_BAL",
            object_name="enriched ...",
        )
        orch.apply_named_function_anchor(state)
        assert state["target_variable"] == "N_EOP_BAL"
        assert state["object_name"] == "enriched ..."
        assert "w76_anchor" not in state

    def test_no_op_with_empty_raw_query(self):
        orch = Orchestrator()
        state = _baseline_state(
            "", target_variable="", object_name="x", query_type="COLUMN_LOGIC"
        )
        orch.apply_named_function_anchor(state)
        assert state["object_name"] == "x"
        assert "w76_anchor" not in state


# ---------------------------------------------------------------------------
# Integration — orchestrator-level behaviour the streaming endpoint relies on
# ---------------------------------------------------------------------------


class TestApplyAnchorIntegrationWithClassifier:
    """The streaming endpoint runs classify_query → apply_named_function_anchor
    → apply_bi_routing → date-range / Phase 2 / DATA_QUERY routing →
    function precheck → semantic search. These tests pin the contract at
    the apply_named_function_anchor boundary: regardless of what the
    classifier returned, the W76 hook anchors on the named function."""

    def test_b1_run7_regression_state_is_anchored(self):
        """Whatever the classifier returned for the B1 prompt, W76 must
        leave state with object_name = the named function and the alias
        literal scrubbed from target_variable."""
        orch = Orchestrator()
        # Simulate the worst-case classifier output: VARIABLE_TRACE +
        # alias literal as target. This is exactly what Run 7 saw.
        state = {
            "raw_query": (
                "In CS_Deferred_Tax_Asset_Net_of_DTL_Calculation, when does "
                "the EXP_11 branch fire, and what does it set?"
            ),
            "query_type": "VARIABLE_TRACE",
            "object_name": (
                "In CS_Deferred_Tax_Asset_Net_of_DTL_Calculation, when does "
                "the EXP_11 branch fire ... explain logic CASE EXP_11 ..."
            ),
            "target_variable": "EXP_11",
            "schema": "OFSMDM",
        }
        orch.apply_named_function_anchor(state)

        # After W76: state is shaped for the function-explainer path.
        assert state["object_name"] == \
            "CS_Deferred_Tax_Asset_Net_of_DTL_Calculation"
        assert state["query_type"] == "COLUMN_LOGIC"
        assert state["target_variable"] == ""
        assert state["w76_anchor"]["source"] == "prefix"

    def test_baseline_canary_a_unaffected(self):
        """Standard regression triple, canary A: 'How does
        FN_LOAD_OPS_RISK_DATA work?' must go through unchanged so
        the W57 baseline (UNVERIFIED, COLUMN_LOGIC, GROUNDING-HIGH on
        pass-through) stays intact."""
        orch = Orchestrator()
        state = {
            "raw_query": "How does FN_LOAD_OPS_RISK_DATA work?",
            "query_type": "COLUMN_LOGIC",
            "object_name": "enriched ...",
            "target_variable": "",
            "schema": "OFSMDM",
        }
        orch.apply_named_function_anchor(state)
        assert state["object_name"] == "enriched ..."
        assert state["query_type"] == "COLUMN_LOGIC"
        assert "w76_anchor" not in state

    def test_baseline_canary_c_unaffected(self):
        """Standard regression triple, canary C: 'How is CAP973
        calculated?' must go through unchanged so BI routing
        (Phase 7) can still fire and produce VERIFIED FUNCTION_LOGIC."""
        orch = Orchestrator()
        state = {
            "raw_query": "How is CAP973 calculated?",
            "query_type": "VARIABLE_TRACE",
            "object_name": "enriched ...",
            "target_variable": "CAP973",
            "schema": "OFSMDM",
        }
        orch.apply_named_function_anchor(state)
        # CAP973 is not an internal alias literal — W76 should leave it
        # alone so BI routing can resolve it.
        assert state["target_variable"] == "CAP973"
        assert "w76_anchor" not in state
