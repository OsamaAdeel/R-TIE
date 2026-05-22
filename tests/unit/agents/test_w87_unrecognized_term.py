"""W87: orchestrator unrecognized-term gate.

Pin the W87 gate behavior. The detector fires UNRECOGNIZED_TERM when the
classifier routed the query as entity-seeking (FUNCTION_LOGIC / COLUMN_LOGIC
/ VARIABLE_TRACE) but every orchestrator-stage resolver failed — no function
name extracted, no BI routing, no W76 anchor, and no target_variable column
resolution. Without W87 the pipeline feeds the concatenated enriched_query
blob (orchestrator.py:669) into semantic search, which fabricates an anchor
on a name-similar but unrelated function. The stakeholder-test-1 Q11
"G Test" query is the canonical failure case.
"""

import pytest

from src.agents.orchestrator import (
    _detect_unrecognized_term_query,
    _extract_unrecognized_term,
    _generate_term_variations,
    build_unrecognized_term_response,
)


# ---------------------------------------------------------------------------
# Helper — build a minimal state dict for the detector
# ---------------------------------------------------------------------------

def _make_state(**overrides):
    state = {
        "query_type": "COLUMN_LOGIC",
        "object_name": "",
        "schema": "",
        "target_variable": "",
        "warnings": [],
        "partial_flag": False,
        "bi_routing": None,
        "w76_anchor": None,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# POSITIVE cases — W87 fires and returns the term
# ---------------------------------------------------------------------------

def test_fires_on_g_test_query_with_target_variable():
    """Stakeholder-test-1 Q11: classifier put 'G_Test' in target_variable but
    nothing else resolved.

    W121-broad-2: 'G_Test' is rejected by the priority-1 synthesis check
    (classifier joined 'G Test' prose into the underscored form, same
    pattern as 'LVE cap' → 'LVE_CAP'). Extraction falls through to
    priority 3 which returns the un-joined 'G Test' — the term the user
    actually typed. End-user-visible W87 decline shape is unchanged.
    """
    state = _make_state(
        query_type="COLUMN_LOGIC",
        target_variable="G_Test",
    )
    term = _detect_unrecognized_term_query(
        state, "what is the threshold value for G Test", redis_client=None,
    )
    assert term == "G Test"


def test_fires_on_g_test_query_no_target_variable():
    """Same Q11 query but classifier didn't stamp target_variable.

    Detector falls back to raw-query heuristics and isolates 'G Test'
    via the multi-word-capitalized run.
    """
    state = _make_state(query_type="COLUMN_LOGIC", target_variable="")
    term = _detect_unrecognized_term_query(
        state, "what is the threshold value for G Test", redis_client=None,
    )
    assert term == "G Test"


def test_fires_on_unfindable_business_concept():
    state = _make_state(
        query_type="FUNCTION_LOGIC",
        target_variable="",
    )
    term = _detect_unrecognized_term_query(
        state, "explain Hypothetical Calculation", redis_client=None,
    )
    assert term == "Hypothetical Calculation"


def test_fires_on_quoted_phrase():
    """Quoted phrases beat the capitalized-run heuristic."""
    state = _make_state(query_type="COLUMN_LOGIC")
    term = _detect_unrecognized_term_query(
        state, 'what is the "Granularity Adjustment" value', redis_client=None,
    )
    assert term == "Granularity Adjustment"


def test_fires_on_variable_trace_query_type():
    """W87 covers VARIABLE_TRACE in addition to FUNCTION_LOGIC / COLUMN_LOGIC.

    Use a no-underscore token so ``extract_function_candidates`` (which
    requires at least one underscore) does NOT match — otherwise the W37
    function precheck would have caught it and this test wouldn't be
    exercising the W87 gate.
    """
    state = _make_state(
        query_type="VARIABLE_TRACE",
        target_variable="MysteryField",
    )
    term = _detect_unrecognized_term_query(
        state, "where does MysteryField come from", redis_client=None,
    )
    assert term == "MysteryField"


# ---------------------------------------------------------------------------
# NEGATIVE cases — W87 does NOT fire
# ---------------------------------------------------------------------------

def test_no_fire_on_known_function():
    """When the query names a function the W37 pre-check / normal pipeline
    handles, W87 stays silent — extract_function_candidates returns
    non-empty, so the gate's condition (b) fails."""
    state = _make_state(query_type="FUNCTION_LOGIC")
    term = _detect_unrecognized_term_query(
        state, "How does FN_LOAD_OPS_RISK_DATA work?", redis_client=None,
    )
    assert term is None


def test_no_fire_on_cap_code_bi_routing():
    """When BI routing resolved a CAP-code, W87 stays silent."""
    state = _make_state(
        query_type="COLUMN_LOGIC",
        bi_routing={
            "identifier": "CAP973",
            "function": "TLX_PROV_AMT_FOR_CAP973",
            "schema": "OFSERM",
            "role": "case_when_target",
            "derivation": None,
        },
    )
    term = _detect_unrecognized_term_query(
        state, "How is CAP973 calculated?", redis_client=None,
    )
    assert term is None


def test_no_fire_on_w76_anchor():
    """When W76 anchored on an explicit function, W87 stays silent."""
    state = _make_state(
        query_type="COLUMN_LOGIC",
        w76_anchor={
            "function": "CS_THRESHOLD_TREATMENT",
            "source": "prefix",
            "original_query_type": "VARIABLE_TRACE",
            "original_target_variable": "",
        },
    )
    term = _detect_unrecognized_term_query(
        state, "In CS_Threshold_Treatment, what is the threshold?",
        redis_client=None,
    )
    assert term is None


def test_no_fire_on_w76_anchor_empty_function():
    """W76 may write an anchor record with function=''; this means the
    alias-literal-fallback fired but found nothing — should NOT block
    W87. The detector treats empty-function W76 records as 'did not
    anchor'."""
    state = _make_state(
        query_type="COLUMN_LOGIC",
        target_variable="UnknownThing",
        w76_anchor={
            "function": "",
            "alias_literal_cleared": "EXP_11",
            "reason": "alias literal with no enclosing function",
            "source": "alias_fallback_no_function",
        },
    )
    term = _detect_unrecognized_term_query(
        state, "what is UnknownThing", redis_client=None,
    )
    assert term == "UnknownThing"


def test_no_fire_on_data_query():
    state = _make_state(query_type="DATA_QUERY")
    term = _detect_unrecognized_term_query(
        state, "what is the total N_EOP_BAL on 2025-12-31",
        redis_client=None,
    )
    assert term is None


def test_no_fire_on_unsupported():
    state = _make_state(query_type="UNSUPPORTED")
    term = _detect_unrecognized_term_query(
        state, "predict next quarter's losses", redis_client=None,
    )
    assert term is None


def test_no_fire_on_value_trace():
    """VALUE_TRACE routes through Phase 2; W87 must not preempt it."""
    state = _make_state(query_type="VALUE_TRACE")
    term = _detect_unrecognized_term_query(
        state, "what is N_EOP_BAL for account 12345", redis_client=None,
    )
    assert term is None


def test_no_fire_on_empty_query_type():
    state = _make_state(query_type="")
    term = _detect_unrecognized_term_query(
        state, "anything", redis_client=None,
    )
    assert term is None


def test_no_fire_on_mixed_query_with_function_resolved():
    """When the query mentions both an extractable function and an unknown
    term, the function-name path satisfies the gate's condition (b) and
    W87 stays silent — downstream W37 / W45 will handle the rest."""
    state = _make_state(
        query_type="COLUMN_LOGIC",
        target_variable="G_Test",
    )
    term = _detect_unrecognized_term_query(
        state, "How does CS_Threshold_Treatment relate to G Test?",
        redis_client=None,
    )
    assert term is None


def test_no_fire_when_column_resolves(monkeypatch):
    """When target_variable resolves to a real indexed column, W87 stays
    silent — the column-index lookup is condition (e)."""
    state = _make_state(
        query_type="VARIABLE_TRACE",
        target_variable="N_EOP_BAL",
    )

    import src.parsing.schema_discovery as schema_disc
    monkeypatch.setattr(
        schema_disc, "schemas_for_column",
        lambda col, redis, schemas=None: ["OFSMDM"],
    )
    # Sentinel non-None redis so the column-resolution branch executes.
    sentinel_redis = object()
    term = _detect_unrecognized_term_query(
        state, "what writes N_EOP_BAL", redis_client=sentinel_redis,
    )
    assert term is None


def test_no_fire_when_column_check_raises(monkeypatch):
    """schemas_for_column raising must not crash the gate. The detector
    logs and treats the column as unresolved."""
    state = _make_state(
        query_type="VARIABLE_TRACE",
        target_variable="MYSTERY",
    )

    import src.parsing.schema_discovery as schema_disc

    def _boom(*_a, **_kw):
        raise RuntimeError("redis down")

    monkeypatch.setattr(schema_disc, "schemas_for_column", _boom)
    sentinel_redis = object()
    term = _detect_unrecognized_term_query(
        state, "what is MYSTERY", redis_client=sentinel_redis,
    )
    # column-resolution failed -> treated as unresolved -> term returned
    assert term == "MYSTERY"


# ---------------------------------------------------------------------------
# Term-extraction edge cases
# ---------------------------------------------------------------------------

def test_extract_prefers_verbatim_target_variable_over_heuristic():
    """Priority 1 wins when target_variable appears verbatim (case-
    insensitively) in the raw query — the user typed the joined form,
    so the classifier's extraction is not a synthesis. W121-broad-2
    only rejects target_variables that look invented from prose."""
    assert _extract_unrecognized_term(
        "what is the threshold for G_TEST_FN", "G_TEST_FN",
    ) == "G_TEST_FN"


def test_extract_falls_through_to_multiword_when_no_target_variable():
    assert _extract_unrecognized_term(
        "what is the threshold value for G Test", "",
    ) == "G Test"


def test_extract_returns_quoted_phrase_when_no_target_variable():
    assert _extract_unrecognized_term(
        'what is "Hypothetical Calculation" defined as', "",
    ) == "Hypothetical Calculation"


def test_extract_returns_none_on_unparseable_query():
    """No quoted phrase, no capitalized word, no target_variable — the
    detector returns None so W87 falls through to the existing
    clarification path."""
    assert _extract_unrecognized_term("is it working", "") is None


def test_extract_returns_longest_capitalized_run():
    """When multiple multi-word runs exist, pick the longest."""
    term = _extract_unrecognized_term(
        "explain Granularity Adjustment Cap vs Hypothetical Calculation", "",
    )
    # "Granularity Adjustment Cap" (3 words) is longer than
    # "Hypothetical Calculation" (2 words)
    assert term == "Granularity Adjustment Cap"


# ---------------------------------------------------------------------------
# W127 — calendar terms are stopwords, must not be extracted as identifiers
# ---------------------------------------------------------------------------

def test_w127_december_is_stopword():
    """December should not be extracted as an unrecognized term — quality
    harness baseline C3: 'Where is the December-only execution gate set?'
    was previously declined with requested_term=December."""
    result = _extract_unrecognized_term(
        "Where is the December-only execution gate set?", "",
    )
    assert result is None or result.upper() != "DECEMBER"


def test_w127_friday_is_stopword():
    result = _extract_unrecognized_term(
        "Which functions ran on Friday?", "",
    )
    assert result is None or result.upper() != "FRIDAY"


def test_w127_quarterly_is_stopword():
    result = _extract_unrecognized_term("What runs Quarterly?", "")
    assert result is None or result.upper() != "QUARTERLY"


def test_w127_q3_is_stopword():
    result = _extract_unrecognized_term(
        "Which batches failed in Q3?", "",
    )
    assert result is None or result.upper() != "Q3"


def test_w127_preserves_named_function_extraction():
    """Adding calendar stopwords must not block legitimate identifiers.
    FN_LOAD_OPS_RISK_DATA still extracts via the priority-4 single-token
    regex (capitalized first letter + `[A-Za-z0-9_]{2,}`)."""
    result = _extract_unrecognized_term(
        "How does FN_LOAD_OPS_RISK_DATA work?", "",
    )
    assert result == "FN_LOAD_OPS_RISK_DATA"


def test_w127_preserves_cap_code_extraction():
    """CAP-codes (CAP973) must still extract — the W127 stopword
    additions must not regress business-identifier surfaces."""
    result = _extract_unrecognized_term(
        "How is CAP973 calculated?", "",
    )
    assert result == "CAP973"


# ---------------------------------------------------------------------------
# W121-broad-2 — priority 1 sanity check against LLM-synthesized
# compound target_variables (test-first pins; REJECT cases fail pre-fix)
# ---------------------------------------------------------------------------
# Background: scratch/w121b_empirical_finding.md
#
# Rejection rule (all must hold):
#   (a) target_variable contains underscore
#   (b) target_variable does NOT appear (case-insensitive) in raw_query
#   (c) every underscore-split token of target_variable appears as a
#       standalone word in raw_query (case-insensitive)
# On rejection, priority 1 returns None and extraction falls through to
# priorities 2-4.

def test_w121b2_lve_cap_synthesis_rejected():
    """A4 baseline: LLM classifier joined 'LVE cap' prose → 'LVE_CAP'.
    Priority 1 should reject and fall through. Priority 4 returns 'LVE'."""
    result = _extract_unrecognized_term(
        "What's the LVE cap?", "LVE_CAP",
    )
    assert result != "LVE_CAP"
    # Acceptable fall-through: priority 4 returns 'LVE'.
    assert result == "LVE" or result is None


def test_w121b2_rrp_eligibility_synthesis_rejected():
    """B1 baseline: LLM classifier joined 'RRP eligibility' prose →
    'RRP_ELIGIBILITY'. Priority 1 rejects; priority 4 returns 'RRP'."""
    result = _extract_unrecognized_term(
        "What enforces RRP eligibility?", "RRP_ELIGIBILITY",
    )
    assert result != "RRP_ELIGIBILITY"
    assert result == "RRP" or result is None


def test_w121b2_n_eop_bal_verbatim_accepted():
    """User typed N_EOP_BAL verbatim — priority 1 must accept and return."""
    result = _extract_unrecognized_term(
        "What is the total N_EOP_BAL for V_LV_CODE='ABL' on 2025-12-31?",
        "N_EOP_BAL",
    )
    assert result == "N_EOP_BAL"


def test_w121b2_fn_load_ops_risk_data_verbatim_accepted():
    """User typed function name verbatim — priority 1 accepts."""
    result = _extract_unrecognized_term(
        "How does FN_LOAD_OPS_RISK_DATA work?",
        "FN_LOAD_OPS_RISK_DATA",
    )
    assert result == "FN_LOAD_OPS_RISK_DATA"


def test_w121b2_ead_amount_verbatim_accepted():
    """User typed EAD_AMOUNT verbatim — priority 1 accepts."""
    result = _extract_unrecognized_term(
        "Where is EAD_AMOUNT computed?",
        "EAD_AMOUNT",
    )
    assert result == "EAD_AMOUNT"


def test_w121b2_lowercase_query_uppercase_target_accepted():
    """EDGE case: user typed n_eop_bal lowercase; classifier returned
    uppercase N_EOP_BAL. The underscore form appears in the raw query
    case-insensitively, so (b) is false → accept."""
    result = _extract_unrecognized_term(
        "what is the total n_eop_bal for V_LV_CODE='ABL' on 2025-12-31?",
        "N_EOP_BAL",
    )
    assert result == "N_EOP_BAL"


def test_w121b2_no_underscore_target_accepted():
    """Targets without underscore can't trigger synthesis-rejection
    rule (a). FAKECOL999 / CAP999 / MYSTERY-style single tokens must
    pass through priority 1 unaffected."""
    assert _extract_unrecognized_term(
        "How is FAKECOL999 calculated?", "FAKECOL999",
    ) == "FAKECOL999"
    assert _extract_unrecognized_term(
        "How is CAP999 calculated?", "CAP999",
    ) == "CAP999"


# ---------------------------------------------------------------------------
# Variation-generation
# ---------------------------------------------------------------------------

def test_variations_space_to_underscore():
    variations = _generate_term_variations("G Test")
    assert "G_Test" in variations


def test_variations_underscore_to_space():
    variations = _generate_term_variations("G_Test")
    assert "G Test" in variations


def test_variations_collapsed():
    variations = _generate_term_variations("G Test")
    # collapsed form (no separators) included
    assert "GTest" in variations


def test_variations_capped_at_five():
    variations = _generate_term_variations("a b c d e f g")
    assert len(variations) <= 5


def test_variations_dedup_input_term():
    variations = _generate_term_variations("G Test")
    assert "G Test" not in variations  # input term itself excluded


def test_variations_empty_term():
    assert _generate_term_variations("") == []


# ---------------------------------------------------------------------------
# Response-builder shape and content
# ---------------------------------------------------------------------------

def test_response_includes_term_in_header():
    payload = build_unrecognized_term_response(
        term="G Test",
        similar_functions=[],
        schemas_loaded=["OFSERM", "OFSMDM"],
        correlation_id="corr-1",
    )
    assert '## Unrecognized Term: "G Test"' in payload["message"]


def test_response_badge_unverified():
    payload = build_unrecognized_term_response(
        term="G Test",
        similar_functions=[],
        schemas_loaded=["OFSERM"],
        correlation_id="corr-2",
    )
    assert payload["badge"] == "UNVERIFIED"
    assert payload["validated"] is False
    assert 0.0 < payload["confidence"] < 1.0


def test_response_warnings_contains_unrecognized_term():
    payload = build_unrecognized_term_response(
        term="G Test",
        similar_functions=[],
        schemas_loaded=[],
        correlation_id="corr-3",
    )
    assert payload["warnings"] == [
        "UNRECOGNIZED_TERM: 'G Test' not in indexed corpus"
    ]


def test_response_lists_neighbors_with_not_the_answer_label():
    payload = build_unrecognized_term_response(
        term="G Test",
        similar_functions=[
            "CS_THRESHOLD_TREATMENT",
            "GENERAL_LEDGER_LOAD",
            "GOODWILL_CALC",
        ],
        schemas_loaded=["OFSERM"],
        correlation_id="corr-4",
    )
    msg = payload["message"]
    assert "CS_THRESHOLD_TREATMENT" in msg
    assert "GENERAL_LEDGER_LOAD" in msg
    assert "GOODWILL_CALC" in msg
    # Each neighbor must be labelled as not-the-answer.
    assert "name-similarity only" in msg
    assert "NOT the answer" in msg


def test_response_omits_neighbor_block_when_empty():
    payload = build_unrecognized_term_response(
        term="G Test",
        similar_functions=[],
        schemas_loaded=["OFSERM"],
        correlation_id="corr-5",
    )
    assert "No close name-similarity matches found." in payload["message"]


def test_response_type_and_status():
    payload = build_unrecognized_term_response(
        term="G Test",
        similar_functions=[],
        schemas_loaded=[],
        correlation_id="corr-6",
    )
    assert payload["type"] == "unrecognized_term"
    assert payload["status"] == "unverified"
    assert payload["requested_term"] == "G Test"


def test_response_message_names_what_was_searched():
    """Response must honestly enumerate the indices RTIE actually consults
    — function names, column index, BI literal index, W76 anchor patterns.
    Must NOT claim to search indices RTIE doesn't have (e.g. Oracle
    all_source / DIM_STANDARD_ACCT_HEAD)."""
    payload = build_unrecognized_term_response(
        term="G Test",
        similar_functions=[],
        schemas_loaded=["OFSERM", "OFSMDM"],
        correlation_id="corr-7",
    )
    msg = payload["message"]
    assert "Loaded function names" in msg
    assert "Column indexes" in msg
    assert "Business-identifier" in msg
    assert "OFSERM" in msg and "OFSMDM" in msg
    # Honest about what RTIE does NOT search — these should not appear.
    assert "all_source" not in msg
    assert "DIM_STANDARD_ACCT_HEAD" not in msg


def test_response_explanation_markdown_matches_message():
    """The done event payload exposes both `message` and
    `explanation.markdown`; downstream consumers / tests assert on the
    structured field, so they must stay in sync."""
    payload = build_unrecognized_term_response(
        term="G Test",
        similar_functions=[],
        schemas_loaded=[],
        correlation_id="corr-8",
    )
    assert payload["explanation"]["markdown"] == payload["message"]
    assert payload["explanation"]["summary"] == payload["message"][:200]
