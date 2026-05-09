"""W76b — downstream evaluators consult W76 anchor + W58 filters.

W76 (merge 248f4ca) overrode orchestrator state when prompts matched
"In <FunctionName>, ..." patterns. But two downstream surfaces in
:mod:`src.agents.logic_explainer` had their own raw_query extraction:

  1. :func:`evaluate_grounding`'s NAMED_FUNCTION_NOT_RETRIEVED check
     emitted phantom warnings citing CASE-branch alias literals
     (``EXP_11``) even when the real anchored function was in
     ``functions_analyzed``.
  2. The companion ``sanity_messages`` entry (the post-hoc Caveats
     appender) said "may describe functions related to EXP_11" even
     when the body correctly described the anchored function.

W76b adds anchor consultation + W58 alias-pattern filtering at both
surfaces via the shared :func:`_resolve_asked_about_functions` helper.

Tests cover four state shapes:
  * anchor-set positive (anchored function IS in functions_analyzed)
  * anchor-set negative (anchored function NOT in functions_analyzed)
  * no-anchor + W58-eligible token (alias literal / table prefix)
  * no-anchor + real-but-missing function (regression baseline)
"""

from __future__ import annotations

from src.agents.logic_explainer import (
    _extract_function_candidates_local,
    _resolve_asked_about_functions,
    _w57_check_anchoring,
    evaluate_grounding,
)


# ---------------------------------------------------------------------------
# _extract_function_candidates_local — W58 filter integration
# ---------------------------------------------------------------------------


class TestExtractCandidatesAppliesW58Filters:
    """W76b backfilled the W58 exclusion gate (table prefixes, alias
    literals, column prefixes, manifest process names) into the local
    extractor so all three callers — the NAMED_FUNCTION_NOT_RETRIEVED
    check, _w57_check_anchoring, and w57_enforce_grounding's Check 5
    — agree with the orchestrator on what counts as a function name."""

    def test_excludes_w58a_table_prefix(self):
        """FCT_/STG_/DIM_/FSI_/SETUP_/AAI_ are tables, never functions."""
        assert "FCT_OPS_RISK_DATA" not in _extract_function_candidates_local(
            "What columns does FCT_OPS_RISK_DATA have?"
        )
        assert "STG_GL_DATA" not in _extract_function_candidates_local(
            "Trace from STG_GL_DATA to FCT_STANDARD_ACCT_HEAD"
        )

    def test_excludes_w58b_alias_literals(self):
        """EXP_<digit> / COND_<digit> / T_<digit> / SS_* / TT_* are
        OFSAA-generated CASE / MERGE labels, never function names."""
        assert "EXP_11" not in _extract_function_candidates_local(
            "When does the EXP_11 branch fire?"
        )
        assert "COND_10" not in _extract_function_candidates_local(
            "What is COND_10?"
        )
        assert "T_1470990981178_0" not in _extract_function_candidates_local(
            "Trace T_1470990981178_0 in the merge"
        )

    def test_excludes_w58c_column_prefix(self):
        assert "N_ANNUAL_GROSS_INCOME" not in _extract_function_candidates_local(
            "How is N_ANNUAL_GROSS_INCOME calculated?"
        )

    def test_extracts_real_function_unaffected(self):
        """The W58 filters must not catch real function names."""
        assert "FN_LOAD_OPS_RISK_DATA" in _extract_function_candidates_local(
            "How does FN_LOAD_OPS_RISK_DATA work?"
        )
        assert "ABL_CAP_MITIGANT_DATA_POPULATION" in _extract_function_candidates_local(
            "How does ABL_CAP_MITIGANT_DATA_POPULATION work?"
        )

    def test_t2t_function_passes_alias_filter(self):
        """T2T_* function names start with letters+digit — must not be
        caught by the W58.b T_<digit> internal-alias regex."""
        assert "T2T_FCT_CCP_DETAILS_STD_ACCT_HEAD_POP" in _extract_function_candidates_local(
            "How does T2T_FCT_CCP_DETAILS_STD_ACCT_HEAD_POP work?"
        )


# ---------------------------------------------------------------------------
# _resolve_asked_about_functions — anchor-first dispatcher
# ---------------------------------------------------------------------------


class TestResolveAskedAboutFunctions:
    """The anchor-first helper — returns the W76-anchored function
    when set, otherwise falls back to W58-filtered raw_query
    extraction."""

    def test_anchor_wins_over_raw_query(self):
        """When state["w76_anchor"] carries a function, raw_query is
        ignored — even when raw_query mentions a different real function."""
        anchor = {"function": "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"}
        result = _resolve_asked_about_functions(
            "When does EXP_11 fire in FN_LOAD_OPS_RISK_DATA?",
            w76_anchor=anchor,
        )
        assert result == ["CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"]

    def test_no_anchor_falls_back_to_raw_query(self):
        result = _resolve_asked_about_functions(
            "How does FN_LOAD_OPS_RISK_DATA work?",
            w76_anchor=None,
        )
        assert "FN_LOAD_OPS_RISK_DATA" in result

    def test_empty_anchor_dict_falls_back_to_raw_query(self):
        """An empty anchor dict (the default state shape) still
        triggers raw_query extraction."""
        result = _resolve_asked_about_functions(
            "How does FN_LOAD_OPS_RISK_DATA work?",
            w76_anchor={},
        )
        assert "FN_LOAD_OPS_RISK_DATA" in result

    def test_anchor_with_empty_function_falls_back(self):
        """W76 M2's no-recovery path stamps an anchor with function=""
        and alias_literal_cleared; that should not short-circuit
        — fall back to raw_query so legitimate downstream warnings
        can still fire."""
        anchor = {
            "function": "",
            "alias_literal_cleared": "EXP_11",
        }
        result = _resolve_asked_about_functions(
            "When does EXP_11 fire?",
            w76_anchor=anchor,
        )
        # EXP_11 filtered by W58.b → empty (no phantom warning)
        assert result == []

    def test_no_anchor_alias_literal_in_query_filtered(self):
        """No anchor, raw_query has only an alias literal → W58.b
        filters it → empty result → no phantom warning fires."""
        result = _resolve_asked_about_functions(
            "When does EXP_11 fire?",
            w76_anchor=None,
        )
        assert result == []

    def test_no_anchor_table_prefix_in_query_filtered(self):
        """No anchor, raw_query has a table prefix → W58.a filters it."""
        result = _resolve_asked_about_functions(
            "What columns does FCT_OPS_RISK_DATA have?",
            w76_anchor=None,
        )
        assert "FCT_OPS_RISK_DATA" not in result


# ---------------------------------------------------------------------------
# evaluate_grounding — NAMED_FUNCTION_NOT_RETRIEVED + sanity_messages
# ---------------------------------------------------------------------------


class TestEvaluateGroundingAnchorAware:
    """The end-to-end behaviour at the level main.py calls. Tests both
    the warnings array and the sanity_messages array (which becomes
    the post-hoc Caveats block)."""

    def test_b1_anchor_set_function_present_no_warnings_no_caveat(self):
        """B1 design proof at the unit level: W76 anchored on
        CS_DEFERRED_TAX_..., functions_analyzed contains it. No
        NAMED_FUNCTION_NOT_RETRIEVED warning, no "may describe related
        functions" caveat."""
        anchor = {"function": "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"}
        result = evaluate_grounding(
            raw_query=(
                "In CS_Deferred_Tax_Asset_Net_of_DTL_Calculation, when "
                "does the EXP_11 branch fire, and what does it set?"
            ),
            markdown=(
                "## CS_Deferred_Tax_Asset_Net_of_DTL_Calculation\n"
                "EXP_11 sets TT.N_STD_ACCT_HEAD_AMT (Lines 24-24).\n"
            ),
            multi_source={
                "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION": {
                    "source_code": [{"line": i, "text": "x"} for i in range(1, 100)],
                },
            },
            functions_analyzed=["CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"],
            query_type="COLUMN_LOGIC",
            redis_client=None,
            w76_anchor=anchor,
        )
        # Critical: no phantom EXP_11 warning, no phantom "related to" caveat.
        for w in result["warnings"]:
            assert "EXP_11 named in query" not in w
            assert "NAMED_FUNCTION_NOT_RETRIEVED" not in w
        for msg in result["sanity_messages"]:
            assert "may describe functions related to EXP_11" not in msg
            assert "EXP_11" not in msg

    def test_anchor_set_function_missing_warning_cites_anchored(self):
        """W76 anchored on CS_FOO but it's NOT in functions_analyzed —
        the warning should cite CS_FOO (the anchored target), not the
        alias literal in raw_query."""
        anchor = {"function": "CS_FOO"}
        result = evaluate_grounding(
            raw_query="In CS_FOO, when does EXP_11 fire?",
            markdown="Step 1 (Lines 1-5).",
            multi_source={"CS_BAR": {"source_code": [{"line": 1, "text": "x"}]}},
            functions_analyzed=["CS_BAR"],
            query_type="COLUMN_LOGIC",
            redis_client=None,
            w76_anchor=anchor,
        )
        named_warnings = [
            w for w in result["warnings"]
            if "NAMED_FUNCTION_NOT_RETRIEVED" in w
        ]
        assert len(named_warnings) == 1
        assert "CS_FOO" in named_warnings[0]
        assert "EXP_11 named in query" not in named_warnings[0]

    def test_no_anchor_alias_literal_only_no_phantom_warning(self):
        """Pre-W76b regression: raw_query has only EXP_11 (no anchor) →
        W58.b filters it → no phantom NAMED_FUNCTION_NOT_RETRIEVED."""
        result = evaluate_grounding(
            raw_query="When does EXP_11 fire?",
            markdown="Step 1 (Lines 1-5).",
            multi_source={"CS_BAR": {"source_code": [{"line": 1, "text": "x"}]}},
            functions_analyzed=["CS_BAR"],
            query_type="COLUMN_LOGIC",
            redis_client=None,
            w76_anchor=None,
        )
        for w in result["warnings"]:
            assert "EXP_11 named in query" not in w
        for msg in result["sanity_messages"]:
            assert "EXP_11" not in msg

    def test_no_anchor_real_missing_function_still_fires(self):
        """W76b regression baseline: a real function name in raw_query
        that's NOT in functions_analyzed STILL fires the warning. The
        W76b helper must not silence legitimate catches."""
        result = evaluate_grounding(
            raw_query="How does NONEXISTENT_FUNCTION_FOR_TESTING_W76B work?",
            markdown="Step 1 (Lines 1-5).",
            multi_source={"CS_BAR": {"source_code": [{"line": 1, "text": "x"}]}},
            functions_analyzed=["CS_BAR"],
            query_type="COLUMN_LOGIC",
            redis_client=None,
            w76_anchor=None,
        )
        named_warnings = [
            w for w in result["warnings"]
            if "NAMED_FUNCTION_NOT_RETRIEVED" in w
        ]
        assert len(named_warnings) == 1
        assert "NONEXISTENT_FUNCTION_FOR_TESTING_W76B" in named_warnings[0]
        # And the matching caveat still fires.
        related_caveats = [
            m for m in result["sanity_messages"]
            if "may describe functions related to" in m
        ]
        assert len(related_caveats) == 1
        assert "NONEXISTENT_FUNCTION_FOR_TESTING_W76B" in related_caveats[0]

    def test_no_anchor_table_prefix_in_query_no_phantom_warning(self):
        """Pre-W76b: 'How does FCT_OPS_RISK_DATA work?' would emit
        NAMED_FUNCTION_NOT_RETRIEVED on the table name. W76b: filtered
        by W58.a, no warning fires."""
        result = evaluate_grounding(
            raw_query="How does FCT_OPS_RISK_DATA work?",
            markdown="Step 1 (Lines 1-5).",
            multi_source={"CS_BAR": {"source_code": [{"line": 1, "text": "x"}]}},
            functions_analyzed=["CS_BAR"],
            query_type="COLUMN_LOGIC",
            redis_client=None,
            w76_anchor=None,
        )
        for w in result["warnings"]:
            assert "FCT_OPS_RISK_DATA named in query" not in w

    def test_default_w76_anchor_param_omitted_back_compat(self):
        """Callers that omit w76_anchor (legacy) get the same behaviour
        as passing w76_anchor=None — no exception, no surprise. This
        backward-compat check protects existing tests in
        test_w57_grounding.py that call evaluate_grounding without the
        new param."""
        result = evaluate_grounding(
            raw_query="How does FN_LOAD_OPS_RISK_DATA work?",
            markdown="Step 1 (Lines 1-5).",
            multi_source={"FN_LOAD_OPS_RISK_DATA": {
                "source_code": [{"line": i, "text": "x"} for i in range(1, 10)]
            }},
            functions_analyzed=["FN_LOAD_OPS_RISK_DATA"],
            query_type="COLUMN_LOGIC",
            redis_client=None,
            # w76_anchor omitted on purpose
        )
        assert "warnings" in result
        assert "sanity_messages" in result


# ---------------------------------------------------------------------------
# _w57_check_anchoring — anchor-aware
# ---------------------------------------------------------------------------


class TestW57CheckAnchoringAnchorAware:
    """The W57 anchoring sub-check (Check 3a) also goes through
    _resolve_asked_about_functions — so the anchor wins over raw_query
    extraction here too. This keeps Check 5 (template-phrase scope) in
    sync with the NAMED_FUNCTION_NOT_RETRIEVED check above."""

    def test_anchor_wins_over_raw_query(self):
        """Anchor on CS_FOO, raw_query mentions BOTH CS_BAR and EXP_11.
        Check 3a should treat CS_FOO as the asked-about — and since
        CS_FOO is in functions_analyzed, no warning should fire."""
        anchor = {"function": "CS_FOO"}
        result = _w57_check_anchoring(
            raw_query="In CS_BAR, what does EXP_11 fire?",
            functions_analyzed=["CS_FOO"],
            markdown="CS_FOO does X.",
            w76_anchor=anchor,
        )
        # CS_FOO IS analyzed → Check 3a returns []
        assert result == []

    def test_no_anchor_falls_back_to_raw_query_filtered(self):
        """No anchor, raw_query has only EXP_11 → W58.b filters it →
        no anchoring warning."""
        result = _w57_check_anchoring(
            raw_query="When does EXP_11 fire?",
            functions_analyzed=["CS_BAR"],
            markdown="CS_BAR does X.",
            w76_anchor=None,
        )
        assert result == []
