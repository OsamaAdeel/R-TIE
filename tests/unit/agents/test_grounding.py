"""Unit tests for W37 grounding logic: function-name pre-check (orchestrator)
and post-explanation grounding evaluation (logic_explainer)."""

from unittest.mock import MagicMock

import pytest

from src.agents.orchestrator import (
    extract_function_candidates,
    function_exists_in_graph,
    find_similar_function_names,
    build_function_not_found_response,
)
from src.agents.logic_explainer import (
    detect_partial_source_function,
    detect_ungrounded_identifiers,
    evaluate_grounding,
)


# ---------------------------------------------------------------------------
# extract_function_candidates
# ---------------------------------------------------------------------------

def test_extract_uppercase_underscore_name():
    cands = extract_function_candidates("Explain FN_LOAD_OPS_RISK_DATA please")
    assert "FN_LOAD_OPS_RISK_DATA" in cands


def test_extract_mixed_case_name():
    cands = extract_function_candidates(
        "How does ABL_Def_Pension_Fund_Asset_Net_DTL work?"
    )
    assert "ABL_Def_Pension_Fund_Asset_Net_DTL" in cands


def test_extract_ignores_short_tokens():
    # "V_LV" is only 4 chars — too short to be a function name.
    cands = extract_function_candidates("what is v_lv code?")
    assert "v_lv" not in cands


def test_extract_ignores_stopword_params():
    # FIC_MIS_DATE is a column name, never a function.
    cands = extract_function_candidates("Filter by FIC_MIS_DATE for 2025-12-31")
    assert "FIC_MIS_DATE" not in cands


def test_extract_ignores_column_type_prefix():
    # OFSAA columns start with single-letter type prefixes (N_, V_, F_).
    # These must not be declined as missing functions.
    cands = extract_function_candidates("How is N_ANNUAL_GROSS_INCOME calculated?")
    assert "N_ANNUAL_GROSS_INCOME" not in cands
    cands = extract_function_candidates("What is V_PROD_CODE for account X?")
    assert "V_PROD_CODE" not in cands
    cands = extract_function_candidates("count where F_EXPOSURE_ENABLED_IND='N'")
    assert "F_EXPOSURE_ENABLED_IND" not in cands


def test_extract_keeps_fn_and_other_function_prefixes():
    # Two-letter prefixes like FN_ should be kept — they're function names.
    cands = extract_function_candidates("explain FN_LOAD_OPS_RISK_DATA")
    assert "FN_LOAD_OPS_RISK_DATA" in cands
    cands = extract_function_candidates("explain TLX_OPS_ADJ_MISDATE")
    assert "TLX_OPS_ADJ_MISDATE" in cands


def test_extract_deduplicates_case_insensitively():
    cands = extract_function_candidates("Call TLX_FOO and tlx_foo")
    assert len(cands) == 1


def test_extract_no_false_positive_on_plain_english():
    cands = extract_function_candidates("what does the function do")
    assert cands == []


# ---------------------------------------------------------------------------
# function_exists_in_graph
# ---------------------------------------------------------------------------

def _mk_redis(present: set[tuple[str, str]]):
    """Build a mock Redis that has a graph key for each (schema, fn) tuple."""
    stored = {
        f"graph:{schema}:{fn}": b"graph-bytes"
        for schema, fn in present
    }
    client = MagicMock()
    client.get.side_effect = lambda k: stored.get(k if isinstance(k, str) else k.decode())
    return client


def test_function_exists_found_in_default_schema(monkeypatch):
    # store.get_function_graph is what orchestrator calls; stub it directly
    # to avoid round-tripping through msgpack.
    from src.agents import orchestrator as orc_mod
    def fake_get(redis_client, schema, fn):
        return {"nodes": []} if (schema, fn) == ("OFSMDM", "FN_LOAD") else None
    monkeypatch.setattr(orc_mod, "get_function_graph", fake_get)
    assert function_exists_in_graph("FN_LOAD", MagicMock()) is True


def test_function_exists_is_case_insensitive(monkeypatch):
    from src.agents import orchestrator as orc_mod
    def fake_get(redis_client, schema, fn):
        return {"nodes": []} if fn == "FN_LOAD" else None
    monkeypatch.setattr(orc_mod, "get_function_graph", fake_get)
    assert function_exists_in_graph("fn_load", MagicMock()) is True


def test_function_not_found(monkeypatch):
    from src.agents import orchestrator as orc_mod
    monkeypatch.setattr(orc_mod, "get_function_graph", lambda *a, **kw: None)
    assert function_exists_in_graph("NOT_A_REAL_FN", MagicMock()) is False


def test_function_exists_falls_open_on_none_client():
    assert function_exists_in_graph("ANY", None) is False


# ---------------------------------------------------------------------------
# find_similar_function_names
# ---------------------------------------------------------------------------

def test_similar_names_scans_three_segment_keys():
    client = MagicMock()
    # Only three-segment graph:<schema>:<fn> keys should be considered.
    scan_data = {
        ("OFSMDM", 0): (0, [
            b"graph:OFSMDM:FN_LOAD_OPS_RISK",
            b"graph:OFSMDM:FN_LOAD_OPS_DATA",
            b"graph:source:OFSMDM:FN_LOAD_OPS_RISK",   # four segments, skip
            b"graph:full:OFSMDM",                       # three segments but name='OFSMDM'
        ]),
        ("OFSERM", 0): (0, []),
    }
    def fake_scan(cursor=0, match=None, count=None):
        schema = match.split(":")[1]
        return scan_data.get((schema, cursor), (0, []))
    client.scan.side_effect = fake_scan

    similar = find_similar_function_names("FN_LOAD_OPS_RISKY", client, top_n=3)
    assert "FN_LOAD_OPS_RISK" in similar
    # Four-segment keys must not show up in the candidate list.
    assert all(not s.startswith("OFSMDM") for s in similar)


def test_similar_names_handles_redis_failure():
    client = MagicMock()
    client.scan.side_effect = Exception("redis down")
    assert find_similar_function_names("ANY", client) == []


# ---------------------------------------------------------------------------
# build_function_not_found_response
# ---------------------------------------------------------------------------

def test_decline_payload_shape():
    payload = build_function_not_found_response(
        requested_function="MISSING_FN",
        similar_functions=["CLOSE_FN_1", "CLOSE_FN_2"],
        correlation_id="c-1",
    )
    assert payload["type"] == "function_not_found"
    assert payload["badge"] == "DECLINED"
    assert payload["validated"] is False
    assert payload["confidence"] == 0.0
    assert payload["source_citations"] == []
    assert payload["requested_function"] == "MISSING_FN"
    assert payload["similar_functions"] == ["CLOSE_FN_1", "CLOSE_FN_2"]
    assert "MISSING_FN" in payload["message"]
    assert "CLOSE_FN_1" in payload["message"]


def test_decline_payload_omits_similar_section_when_empty():
    payload = build_function_not_found_response(
        requested_function="MISSING_FN",
        similar_functions=[],
        correlation_id="c-1",
    )
    assert "Did you mean" not in payload["message"]


# ---------------------------------------------------------------------------
# evaluate_grounding — empty-citations rule (CHANGE 1.3)
# ---------------------------------------------------------------------------

def test_empty_citations_forces_unverified_for_column_logic():
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown="This function does things.",  # no line refs
        multi_source={},
        functions_analyzed=[],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "UNVERIFIED"
    assert grounding["confidence"] == 0.0
    assert any("CITATIONS" in w for w in grounding["warnings"])


def test_empty_citations_forces_unverified_for_variable_trace():
    grounding = evaluate_grounding(
        raw_query="How is EAD calculated",
        markdown="EAD is calculated somehow.",
        multi_source={},
        functions_analyzed=[],
        query_type="VARIABLE_TRACE",
    )
    assert grounding["badge"] == "UNVERIFIED"


def test_line_references_count_as_citations():
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown="At Line 42 the value is stored.",
        multi_source={"FN_FOO": {"source_code": [{"line": 42, "text": "x := 1;"}]}},
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "VERIFIED"
    assert any(c["line"] == 42 for c in grounding["source_citations"])


def test_functions_analyzed_counts_as_implicit_citation():
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown="This function does things without line references.",
        multi_source={"FN_FOO": {"source_code": [{"line": 1, "text": "BEGIN"}]}},
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    # Even without explicit line numbers, having a real analyzed function
    # is enough for the looser "citations present" check.
    assert grounding["badge"] == "VERIFIED"


# ---------------------------------------------------------------------------
# evaluate_grounding — self-contradiction (CHANGE 1.4)
# ---------------------------------------------------------------------------

def test_contradiction_phrase_with_substantive_continuation_triggers_unverified():
    # 60+ tokens after the forbidden phrase.
    continuation = " ".join(["explanation"] * 60)
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown=f"The source was not provided. {continuation}",
        multi_source={"FN_FOO": {"source_code": [{"line": 1, "text": "x"}]}},
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "UNVERIFIED"
    assert any("CONTRADICTION" in w for w in grounding["warnings"])
    assert any("contradict" in m.lower() for m in grounding["sanity_messages"])


def test_short_decline_without_continuation_is_fine():
    grounding = evaluate_grounding(
        raw_query="Explain FN_FOO",
        markdown="The source was not provided. No trace available.",
        multi_source={"FN_FOO": {"source_code": [{"line": 1, "text": "x"}]}},
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    # Short decline messages should NOT trigger the contradiction rule.
    assert not any("CONTRADICTION" in w for w in grounding["warnings"])


# ---------------------------------------------------------------------------
# evaluate_grounding — ungrounded business identifiers (CHANGE 1.2)
# ---------------------------------------------------------------------------

def test_cap_code_not_in_source_triggers_caveat():
    grounding = evaluate_grounding(
        raw_query="How is CAP973 calculated?",
        markdown="The calculation of CAP973 begins at Line 10.",
        multi_source={
            "FN_OTHER": {"source_code": [
                {"line": 1, "text": "INSERT INTO T (CAP013) VALUES (1);"},
            ]},
        },
        functions_analyzed=["FN_OTHER"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "UNVERIFIED"
    assert any("UNGROUNDED_IDENTIFIERS" in w for w in grounding["warnings"])
    assert any("CAP973" in m for m in grounding["sanity_messages"])


def test_cap_code_present_in_source_is_verified():
    grounding = evaluate_grounding(
        raw_query="How is CAP013 calculated?",
        markdown="CAP013 is calculated at Line 10.",
        multi_source={
            "FN_FOO": {"source_code": [
                {"line": 10, "text": "CAP013 := 1;"},
            ]},
        },
        functions_analyzed=["FN_FOO"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "VERIFIED"
    assert not any("UNGROUNDED_IDENTIFIERS" in w for w in grounding["warnings"])


# ---------------------------------------------------------------------------
# detect_ungrounded_identifiers — pre-generation helper (W45)
# ---------------------------------------------------------------------------

def test_detect_ungrounded_identifiers_flags_cap_code():
    # CAP973 absent from all source bodies → returned.
    ungrounded = detect_ungrounded_identifiers(
        raw_query="How is CAP973 calculated?",
        multi_source={
            "TLX_PROV_AMT_FOR_CAP013": {"source_code": [
                {"line": 1, "text": "V_CAP_HEAD_CD = 'ABL_CAP013'"},
            ]},
        },
    )
    assert ungrounded == ["CAP973"]


def test_detect_ungrounded_identifiers_allows_grounded():
    # CAP013 is present in a source body → empty list.
    ungrounded = detect_ungrounded_identifiers(
        raw_query="How is CAP013 calculated?",
        multi_source={
            "FN_FOO": {"source_code": [
                {"line": 10, "text": "V_CAP_HEAD_CD = 'ABL_CAP013';"},
            ]},
        },
    )
    assert ungrounded == []


def test_detect_ungrounded_identifiers_ignores_column_prefixes():
    # OFSAA column names like N_EOP_BAL and V_PROD_CODE don't match the
    # identifier regex (single-letter prefix before the underscore), so they
    # must not be flagged as ungrounded even when absent from sources.
    ungrounded = detect_ungrounded_identifiers(
        raw_query="How is N_EOP_BAL computed and what is V_PROD_CODE?",
        multi_source={
            "FN_EMPTY": {"source_code": [{"line": 1, "text": "BEGIN"}]},
        },
    )
    assert ungrounded == []


def test_detect_ungrounded_identifiers_splits_grounded_from_ungrounded():
    # Query names two identifiers; only one is in the source. The other
    # comes back in the returned list; the grounded one does not.
    ungrounded = detect_ungrounded_identifiers(
        raw_query="Compare CAP013 and CAP973 calculations.",
        multi_source={
            "FN_FOO": {"source_code": [
                {"line": 1, "text": "V_CAP_HEAD_CD = 'ABL_CAP013';"},
            ]},
        },
    )
    assert ungrounded == ["CAP973"]


def test_detect_ungrounded_identifiers_empty_query_returns_empty():
    # No identifiers → empty list, no source scan performed.
    assert detect_ungrounded_identifiers("", {}) == []
    assert detect_ungrounded_identifiers("how does this work", {"FN": {}}) == []


def test_detect_ungrounded_identifiers_empty_multi_source_returns_all():
    # No source at all → every identifier in the query is ungrounded.
    # Regex wants >=2 letters + >=2 digits, so CAP973 and IRB25 qualify.
    ungrounded = detect_ungrounded_identifiers(
        raw_query="Explain CAP973 and IRB25 together",
        multi_source={},
    )
    assert ungrounded == sorted(["CAP973", "IRB25"])


# ---------------------------------------------------------------------------
# evaluate_grounding — named function not retrieved by semantic search
# ---------------------------------------------------------------------------

def test_named_function_not_in_functions_analyzed_triggers_warning():
    """User named ABL_Def_Pension but semantic search returned adjacent
    functions instead. Grounding must catch this and downgrade."""
    grounding = evaluate_grounding(
        raw_query="What does ABL_Def_Pension_Fund_Asset_Net_DTL do?",
        markdown="The function TLX_PROV_AMT_FOR_CAP013 at Line 10 does work.",
        multi_source={
            "TLX_PROV_AMT_FOR_CAP013": {"source_code": [
                {"line": 10, "text": "INSERT INTO T VALUES (1);"},
            ]},
        },
        functions_analyzed=["TLX_PROV_AMT_FOR_CAP013"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "UNVERIFIED"
    assert any(
        "NAMED_FUNCTION_NOT_RETRIEVED" in w for w in grounding["warnings"]
    )
    assert any(
        "ABL_Def_Pension_Fund_Asset_Net_DTL" in m
        for m in grounding["sanity_messages"]
    )


def test_named_function_present_in_functions_analyzed_is_verified():
    grounding = evaluate_grounding(
        raw_query="What does FN_LOAD_OPS_RISK_DATA do?",
        markdown="FN_LOAD_OPS_RISK_DATA at Line 10 loads rows.",
        multi_source={
            "FN_LOAD_OPS_RISK_DATA": {"source_code": [
                {"line": 10, "text": "INSERT INTO T VALUES (1);"},
            ]},
        },
        functions_analyzed=["FN_LOAD_OPS_RISK_DATA"],
        query_type="COLUMN_LOGIC",
    )
    assert grounding["badge"] == "VERIFIED"
    assert not any(
        "NAMED_FUNCTION_NOT_RETRIEVED" in w for w in grounding["warnings"]
    )


# ---------------------------------------------------------------------------
# detect_partial_source_function — pre-generation helper (W49)
# ---------------------------------------------------------------------------

def _stub_redis_with_metadata(metadata_keys: set[tuple[str, str]]):
    """Build a stub Redis whose .get returns msgpacked bytes only for the
    parse_metadata keys present in *metadata_keys* (set of (schema, fn)).
    """
    from src.parsing.serializer import to_msgpack
    stored = {
        f"graph:meta:{schema}:{fn}": to_msgpack(
            {"parsed_at": "2026-04-25T00:00:00+00:00",
             "schema": schema, "function_name": fn,
             "node_count": 1, "edge_count": 0}
        )
        for schema, fn in metadata_keys
    }
    client = MagicMock()
    client.get.side_effect = lambda k: stored.get(
        k if isinstance(k, str) else k.decode()
    )
    return client


def test_partial_source_true_when_metadata_present_and_source_empty():
    redis = _stub_redis_with_metadata(
        {("OFSERM", "ABL_DEF_PENSION_FUND_ASSET_NET_DTL")}
    )
    assert detect_partial_source_function(
        function_name="ABL_Def_Pension_Fund_Asset_Net_DTL",
        schema="OFSERM",
        retrieved_source=None,
        redis_client=redis,
    ) is True


def test_partial_source_false_when_metadata_present_but_source_has_body():
    redis = _stub_redis_with_metadata({("OFSMDM", "FN_LOAD_OPS_RISK_DATA")})
    real_source = [
        {"line": 1, "text": "CREATE OR REPLACE FUNCTION FN_LOAD_OPS_RISK_DATA"},
        {"line": 2, "text": "AS V_X NUMBER; BEGIN INSERT INTO T VALUES (1); END;"},
    ]
    assert detect_partial_source_function(
        function_name="FN_LOAD_OPS_RISK_DATA",
        schema="OFSMDM",
        retrieved_source=real_source,
        redis_client=redis,
    ) is False


def test_partial_source_false_when_metadata_absent():
    # No metadata for any (schema, fn) → not a partial-source case.
    redis = _stub_redis_with_metadata(set())
    assert detect_partial_source_function(
        function_name="UNKNOWN_FN",
        schema="OFSMDM",
        retrieved_source=None,
        redis_client=redis,
    ) is False


def test_partial_source_false_when_source_retrieval_threw_exception():
    # Simulated by handing detect_partial_source_function an empty source
    # AND a Redis whose .get raises — we must not crash, just fall open.
    redis = MagicMock()
    redis.get.side_effect = Exception("redis down")
    assert detect_partial_source_function(
        function_name="ABL_Def_Pension_Fund_Asset_Net_DTL",
        schema="OFSERM",
        retrieved_source=None,
        redis_client=redis,
    ) is False


def test_partial_source_true_when_source_is_whitespace_only():
    # Whitespace-only source bodies should still trigger the partial path.
    redis = _stub_redis_with_metadata(
        {("OFSERM", "ABL_DEF_PENSION_FUND_ASSET_NET_DTL")}
    )
    assert detect_partial_source_function(
        function_name="ABL_Def_Pension_Fund_Asset_Net_DTL",
        schema="OFSERM",
        retrieved_source=[{"line": 1, "text": "   \n"}],
        redis_client=redis,
    ) is True


def test_partial_source_false_when_redis_client_is_none():
    # No Redis → fall open. We never want to false-positive on a
    # misconfigured environment.
    assert detect_partial_source_function(
        function_name="ANY",
        schema="OFSERM",
        retrieved_source=None,
        redis_client=None,
    ) is False
