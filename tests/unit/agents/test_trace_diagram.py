"""Unit tests for the W151 Phase 1 trace-diagram assembler.

Covers the per-element grounding rules (node / edge / render-ceiling /
DECLINED-None), the citation atom (sliced from multi_source, never markdown),
and assembly of both trace shapes (column fan-in + CAP-code derivation DAG).
"""

from src.agents.trace_diagram import (
    build_trace_diagram,
    diagram_from_bi_routing,
    fan_in_steps_from_tagged_lines,
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------
def _ms(function, line_texts, schema="OFSERM"):
    """multi_source with one function whose source_code is {lineno: text}."""
    return {
        function: {
            "source_code": [
                {"line": n, "text": t} for n, t in sorted(line_texts.items())
            ],
            "schema": schema,
            "description": "",
            "tables_read": "",
            "tables_written": "",
            "score": 0.9,
        }
    }


VERIFIED_BODY = {"badge": "VERIFIED", "confidence": 0.95, "warnings": []}
UNVERIFIED_BODY = {"badge": "UNVERIFIED", "confidence": 0.2, "warnings": ["W"]}
DECLINED_BODY = {"badge": "DECLINED", "confidence": 0.0, "warnings": []}


def _node(diagram, node_id):
    return next(n for n in diagram["nodes"] if n["id"] == node_id)


def _edge_from(diagram, node_id):
    return next(e for e in diagram["edges"] if e["from"] == node_id)


# ---------------------------------------------------------------------------
# Rule 1 — NODE grounding
# ---------------------------------------------------------------------------
def test_node_verified_when_member_and_span():
    ms = _ms("FN_A", {40: "INSERT INTO T ...", 41: "SELECT v ..."})
    steps = [{"node_id": "W", "function": "FN_A", "operation": "INSERT",
              "line_start": 40, "line_end": 41}]
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source=ms,
                            grounding=VERIFIED_BODY, fan_in_steps=steps)
    w = _node(d, "W")
    assert w["citation"]["grounding"] == "VERIFIED"
    assert w["citation"]["lines"] == [40, 41]
    assert "INSERT INTO T" in w["citation"]["text"]


def test_node_unverified_when_not_a_member():
    # function not present in multi_source → UNVERIFIED, empty excerpt.
    steps = [{"node_id": "W", "function": "FN_MISSING", "operation": "INSERT",
              "line_start": 40, "line_end": 41}]
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source={},
                            grounding=VERIFIED_BODY, fan_in_steps=steps)
    w = _node(d, "W")
    assert w["citation"]["grounding"] == "UNVERIFIED"
    assert w["citation"]["text"] == ""


def test_node_unverified_when_no_real_span():
    # in multi_source but [0,0] span resolves no lines → UNVERIFIED.
    ms = _ms("FN_A", {40: "x"})
    steps = [{"node_id": "W", "function": "FN_A", "line_start": 0, "line_end": 0}]
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source=ms,
                            grounding=VERIFIED_BODY, fan_in_steps=steps)
    assert _node(d, "W")["citation"]["grounding"] == "UNVERIFIED"


# ---------------------------------------------------------------------------
# Rule 2 — EDGE grounding (flow claim)
# ---------------------------------------------------------------------------
def test_edge_unverified_in_alternative_group_despite_valid_citation():
    ms = _ms("VW_A", {12: "FROM SRC", 30: "aggregate"})
    steps = [{"node_id": "A_OUT", "function": "VW_A", "operation": "INSERT",
              "line_start": 12, "line_end": 30, "successor": "TARGET"}]
    alts = [{"label": "alt", "members": ["A_OUT"],
             "candidates": [{"label": "A", "nodes": ["A_OUT"]}],
             "divergence": {"between": ["A_OUT"], "note": "x"}}]
    d = build_trace_diagram(target="TARGET", trace_kind="fan-in", multi_source=ms,
                            grounding=VERIFIED_BODY, fan_in_steps=steps,
                            alternatives=alts)
    # The NODE is still VERIFIED (member + span); only the FLOW edge is forced down.
    assert _node(d, "A_OUT")["citation"]["grounding"] == "VERIFIED"
    edge = _edge_from(d, "A_OUT")
    assert edge["grounding"] == "UNVERIFIED"
    assert edge["citation"]["grounding"] == "UNVERIFIED"


def test_edge_unverified_when_ungrounded_gap_despite_valid_citation():
    ms = _ms("FN_A", {12: "FROM SRC", 30: "agg"})
    steps = [{"node_id": "GAP", "function": "FN_A", "operation": "INSERT",
              "line_start": 12, "line_end": 30, "successor": "T",
              "ungrounded_gap": True}]
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source=ms,
                            grounding=VERIFIED_BODY, fan_in_steps=steps)
    edge = _edge_from(d, "GAP")
    assert edge["ungroundedGap"] is True
    assert edge["grounding"] == "UNVERIFIED"


def test_edge_verified_only_when_member_span_not_alt_not_gap():
    ms = _ms("FN_W", {40: "INSERT INTO T", 58: "v"})
    steps = [{"node_id": "W", "function": "FN_W", "operation": "INSERT",
              "line_start": 40, "line_end": 58, "successor": "T"}]
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source=ms,
                            grounding=VERIFIED_BODY, fan_in_steps=steps)
    edge = _edge_from(d, "W")
    assert edge["grounding"] == "VERIFIED"
    assert edge["ungroundedGap"] is False


# ---------------------------------------------------------------------------
# Rule 3 — render ceiling
# ---------------------------------------------------------------------------
def test_ceiling_downgrades_everything_when_body_unverified():
    ms = _ms("FN_W", {40: "INSERT INTO T", 58: "v"})
    steps = [{"node_id": "W", "function": "FN_W", "operation": "INSERT",
              "line_start": 40, "line_end": 58, "successor": "T"}]
    # Intrinsically the node + edge would be VERIFIED, but the body is UNVERIFIED.
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source=ms,
                            grounding=UNVERIFIED_BODY, fan_in_steps=steps)
    assert d["diagram_grounding"] == "UNVERIFIED"
    assert all(n["citation"]["grounding"] == "UNVERIFIED" for n in d["nodes"])
    assert all(e["grounding"] == "UNVERIFIED" for e in d["edges"])
    assert all(e["citation"]["grounding"] == "UNVERIFIED" for e in d["edges"])


def test_aggregate_equals_body_badge_by_construction():
    ms = _ms("FN_W", {40: "INSERT INTO T", 58: "v"})
    steps = [{"node_id": "W", "function": "FN_W", "operation": "INSERT",
              "line_start": 40, "line_end": 58, "successor": "T"}]
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source=ms,
                            grounding=VERIFIED_BODY, fan_in_steps=steps)
    assert d["diagram_grounding"] == "VERIFIED"


# ---------------------------------------------------------------------------
# Rule 4 — DECLINED returns None
# ---------------------------------------------------------------------------
def test_declined_badge_returns_none():
    ms = _ms("FN_W", {40: "INSERT INTO T"})
    steps = [{"node_id": "W", "function": "FN_W", "line_start": 40, "line_end": 40}]
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source=ms,
                            grounding=DECLINED_BODY, fan_in_steps=steps)
    assert d is None


def test_unknown_trace_kind_returns_none():
    d = build_trace_diagram(target="T", trace_kind="function-logic",
                            multi_source={}, grounding=VERIFIED_BODY)
    assert d is None


def test_empty_inputs_return_none():
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source={},
                            grounding=VERIFIED_BODY, fan_in_steps=[])
    assert d is None


# ---------------------------------------------------------------------------
# Citation atom — sliced from source_code, never markdown
# ---------------------------------------------------------------------------
def test_citation_excerpt_is_the_source_slice_only():
    ms = _ms("FN_A", {
        10: "line ten",
        11: "line eleven",
        12: "line twelve",
        13: "line thirteen",
    })
    steps = [{"node_id": "W", "function": "FN_A", "line_start": 11, "line_end": 12}]
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source=ms,
                            grounding=VERIFIED_BODY, fan_in_steps=steps)
    cit = _node(d, "W")["citation"]
    # exactly the in-range lines, joined from source_code — nothing else.
    assert cit["text"] == "line eleven\nline twelve"
    assert "line ten" not in cit["text"]
    assert "line thirteen" not in cit["text"]
    assert cit["lines"] == [11, 12]


def test_citation_truncates_and_marks_overflow():
    big = {n: f"src line {n}" for n in range(1, 200)}  # 199 lines
    ms = _ms("FN_BIG", big)
    steps = [{"node_id": "W", "function": "FN_BIG", "line_start": 1, "line_end": 199}]
    d = build_trace_diagram(target="T", trace_kind="fan-in", multi_source=ms,
                            grounding=VERIFIED_BODY, fan_in_steps=steps)
    cit = _node(d, "W")["citation"]
    assert cit.get("truncated") is True
    assert len(cit["text"].splitlines()) == 80  # TEXT_LINE_CAP


# ---------------------------------------------------------------------------
# Both shapes assemble
# ---------------------------------------------------------------------------
def test_fan_in_shape_assembles_full_case():
    ms = {}
    ms.update(_ms("fn_calc", {40: "INSERT INTO STG (N_STD_ACCT_HEAD_AMT)", 58: "v"}))
    ms.update(_ms("vw_a", {12: "FROM SRC WHERE V_LV_CODE='ABL'", 30: "agg"}))
    ms.update(_ms("vw_b", {9: "FROM SRC", 22: "agg"}))
    steps = [
        {"node_id": "CITED_WRITER", "function": "fn_calc", "operation": "INSERT",
         "line_start": 40, "line_end": 58, "successor": "N_STD_ACCT_HEAD_AMT"},
        {"node_id": "A_OUT", "function": "vw_a", "operation": "INSERT",
         "line_start": 12, "line_end": 30, "successor": "N_STD_ACCT_HEAD_AMT"},
        {"node_id": "B_OUT", "function": "vw_b", "operation": "INSERT",
         "line_start": 9, "line_end": 22, "successor": "N_STD_ACCT_HEAD_AMT"},
        {"node_id": "GAP_SRC", "function": "unknown_feed", "operation": "READ",
         "line_start": 0, "line_end": 0, "successor": "CITED_WRITER",
         "ungrounded_gap": True},
    ]
    alts = [{"label": "ALTERNATIVE WRITERS", "members": ["A_OUT", "B_OUT"],
             "candidates": [{"label": "A", "nodes": ["A_OUT"]},
                            {"label": "B", "nodes": ["B_OUT"]}],
             "divergence": {"between": ["A_OUT", "B_OUT"], "note": "filter"}}]
    d = build_trace_diagram(target="N_STD_ACCT_HEAD_AMT", trace_kind="fan-in",
                            multi_source=ms, grounding=VERIFIED_BODY,
                            fan_in_steps=steps, alternatives=alts)
    assert d is not None
    assert d["trace_kind"] == "fan-in"
    # target sink node was synthesized
    assert _node(d, "N_STD_ACCT_HEAD_AMT")["kind"] == "target-column"
    # the cited writer's flow is the only VERIFIED edge
    assert _edge_from(d, "CITED_WRITER")["grounding"] == "VERIFIED"
    # competing writers are UNVERIFIED (alternative group)
    assert _edge_from(d, "A_OUT")["grounding"] == "UNVERIFIED"
    assert _edge_from(d, "B_OUT")["grounding"] == "UNVERIFIED"
    # the prose-only feed is a dashed ungrounded gap
    gap = _edge_from(d, "GAP_SRC")
    assert gap["ungroundedGap"] is True and gap["grounding"] == "UNVERIFIED"
    # group + divergence preserved
    assert len(d["groups"]) == 1
    assert _node(d, "A_OUT").get("isDivergence") is True


def test_derivation_dag_shape_assembles():
    ms = _ms("CS_FN", {24: "MERGE INTO T USING ... CAP943 = MAX(CASE CAP309) - "
                            "MAX(CASE CAP863)"})
    recs = [{
        "target_literal": "CAP943",
        "target_column": "N_STD_ACCT_HEAD_AMT",
        "source_literals": ["CAP309", "CAP863"],
        "operation": "SUBTRACT",
        "operands": [{"literal": "CAP309", "amount_column": "A"},
                     {"literal": "CAP863", "amount_column": "B"}],
        "function": "CS_FN",
        "line_range": [24, 24],
    }]
    d = build_trace_diagram(target="CAP943", trace_kind="derivation-dag",
                            multi_source=ms, grounding=VERIFIED_BODY,
                            derivation_records=recs)
    assert d is not None
    ids = {n["id"] for n in d["nodes"]}
    assert {"CAP943", "CAP309", "CAP863"} <= ids
    # two operand edges into the target, signed + / −
    into_target = [e for e in d["edges"] if e["to"] == "CAP943"]
    assert len(into_target) == 2
    assert {e["label"] for e in into_target} == {"+", "−"}
    assert all(e["kind"] == "subtract-operand" for e in into_target)
    # grounded: function in multi_source with the resolved span
    assert all(e["grounding"] == "VERIFIED" for e in into_target)
    assert "CAP943" in _node(d, "CAP943")["citation"]["text"]


def test_derivation_dag_respects_ceiling():
    ms = _ms("CS_FN", {24: "CAP943 = MAX(CAP309) - MAX(CAP863)"})
    recs = [{"target_literal": "CAP943", "source_literals": ["CAP309", "CAP863"],
             "operation": "SUBTRACT",
             "operands": [{"literal": "CAP309"}, {"literal": "CAP863"}],
             "function": "CS_FN", "line_range": [24, 24]}]
    d = build_trace_diagram(target="CAP943", trace_kind="derivation-dag",
                            multi_source=ms, grounding=UNVERIFIED_BODY,
                            derivation_records=recs)
    assert d["diagram_grounding"] == "UNVERIFIED"
    assert all(e["grounding"] == "UNVERIFIED" for e in d["edges"])
    assert all(n["citation"]["grounding"] == "UNVERIFIED" for n in d["nodes"])


# ---------------------------------------------------------------------------
# diagram_from_bi_routing — Phase 3 stream orchestration (real CAP943 shape)
# ---------------------------------------------------------------------------
# Mirrors the live record in graph:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_
# CALCULATION["derivations"]: CAP943 = CAP309 - CAP863, line_range [24,24].
_CAP_FN = "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"
_CAP_BI = {"identifier": "CAP943", "function": _CAP_FN, "schema": "OFSERM",
           "role": "case_when_target",
           "derivation": {"operation": "SUBTRACT", "source_literals": ["CAP309", "CAP863"]}}
_CAP_GRAPH = {
    "nodes": [], "edges": [],
    "derivations": [{
        "target_literal": "CAP943", "target_column": "N_CARRYING_AMOUNT",
        "source_literals": ["CAP309", "CAP863"], "operation": "SUBTRACT",
        "operands": [{"literal": "CAP309", "amount_column": "A"},
                     {"literal": "CAP863", "amount_column": "B"}],
        "function": _CAP_FN, "line_range": [24, 24],
    }],
}


def _lookup_ok(schema, function):
    return _CAP_GRAPH if function == _CAP_FN else None


def test_bi_routing_builds_derivation_dag():
    ms = _ms(_CAP_FN, {24: "MERGE ... CAP943 = MAX(CASE CAP309) - MAX(CASE CAP863)"})
    d = diagram_from_bi_routing(_CAP_BI, ms, VERIFIED_BODY, _lookup_ok)
    assert d is not None
    assert d["trace_kind"] == "derivation-dag"
    assert d["target"] == "CAP943"
    ids = {n["id"] for n in d["nodes"]}
    assert {"CAP943", "CAP309", "CAP863"} <= ids
    into = [e for e in d["edges"] if e["to"] == "CAP943"]
    assert {e["label"] for e in into} == {"+", "−"}
    # function in multi_source + resolved span → solid (the happy path)
    assert all(e["grounding"] == "VERIFIED" for e in into)


def test_bi_routing_respects_ceiling_unverified_body():
    ms = _ms(_CAP_FN, {24: "MERGE ... CAP943 = MAX(CASE CAP309) - MAX(CASE CAP863)"})
    d = diagram_from_bi_routing(_CAP_BI, ms, UNVERIFIED_BODY, _lookup_ok)
    assert d["diagram_grounding"] == "UNVERIFIED"
    assert all(e["grounding"] == "UNVERIFIED" for e in d["edges"])


def test_bi_routing_dashed_when_function_not_in_multi_source():
    # retrieval gap: body VERIFIED but the cited function isn't in multi_source.
    # Ceiling only downgrades, never upgrades → honest dashed, not a bug.
    d = diagram_from_bi_routing(_CAP_BI, {}, VERIFIED_BODY, _lookup_ok)
    assert d is not None
    assert d["diagram_grounding"] == "VERIFIED"   # aggregate == body badge
    assert all(n["citation"]["grounding"] == "UNVERIFIED" for n in d["nodes"])
    assert all(e["grounding"] == "UNVERIFIED" for e in d["edges"])


def test_bi_routing_none_when_no_routing():
    assert diagram_from_bi_routing(None, {}, VERIFIED_BODY, _lookup_ok) is None
    assert diagram_from_bi_routing({}, {}, VERIFIED_BODY, _lookup_ok) is None


def test_bi_routing_none_when_incomplete():
    incomplete = {"identifier": "CAP943", "schema": "OFSERM"}  # no function
    assert diagram_from_bi_routing(incomplete, {}, VERIFIED_BODY, _lookup_ok) is None


def test_bi_routing_none_when_no_derivations():
    lookup_empty = lambda s, f: {"nodes": [], "edges": []}  # no 'derivations'
    assert diagram_from_bi_routing(_CAP_BI, {}, VERIFIED_BODY, lookup_empty) is None


def test_bi_routing_none_on_declined_badge():
    ms = _ms(_CAP_FN, {24: "x"})
    assert diagram_from_bi_routing(_CAP_BI, ms, DECLINED_BODY, _lookup_ok) is None


# ---------------------------------------------------------------------------
# fan_in_steps_from_tagged_lines — Phase 3.5 adapter (Model A, flat)
# ---------------------------------------------------------------------------
def _tl(function, line, operation, text="x", commented=False):
    return {"function": function, "line": line, "text": text,
            "aliases_matched": ["V"], "operation": operation, "commented": commented}


def test_fanin_two_writer_functions_fan_in_to_sink():
    tl = [_tl("FN_A", 40, "INSERT"), _tl("FN_B", 12, "ASSIGN")]
    steps = fan_in_steps_from_tagged_lines(tl, "TGT")
    assert len(steps) == 2
    assert all(s["successor"] == "TGT" and s["edge_kind"] == "writes" for s in steps)
    assert {s["function"] for s in steps} == {"FN_A", "FN_B"}


def test_fanin_read_attaches_to_own_function_writer_no_cross_function():
    tl = [_tl("FN_A", 40, "INSERT"), _tl("FN_A", 38, "READ"), _tl("FN_B", 12, "ASSIGN")]
    steps = fan_in_steps_from_tagged_lines(tl, "TGT")
    read = next(s for s in steps if s["edge_kind"] == "reads")
    assert read["function"] == "FN_A"
    assert read["successor"] == "FN_A:INSERT:L40"
    # no cross-function edge (FN_A read never points into FN_B)
    assert all(not (s["function"] == "FN_A" and str(s["successor"]).startswith("FN_B"))
               for s in steps)


def test_fanin_read_attaches_to_first_writer_when_multiple():
    tl = [_tl("FN_A", 40, "INSERT"), _tl("FN_A", 60, "UPDATE"), _tl("FN_A", 30, "READ")]
    steps = fan_in_steps_from_tagged_lines(tl, "TGT")
    read = next(s for s in steps if s["edge_kind"] == "reads")
    assert read["successor"] == "FN_A:INSERT:L40"  # first writer by line (40 < 60)


def test_fanin_coalesces_multiline_statement():
    tl = [_tl("FN_A", 40, "INSERT"), _tl("FN_A", 41, "INSERT"), _tl("FN_A", 42, "INSERT")]
    steps = fan_in_steps_from_tagged_lines(tl, "TGT")
    assert len(steps) == 1
    assert steps[0]["line_start"] == 40 and steps[0]["line_end"] == 42


def test_fanin_splits_distinct_statements_beyond_gap():
    tl = [_tl("FN_A", 40, "INSERT"), _tl("FN_A", 50, "INSERT")]  # gap 10 > 2
    steps = fan_in_steps_from_tagged_lines(tl, "TGT")
    assert len(steps) == 2


def test_fanin_excludes_commented_out():
    tl = [_tl("FN_A", 40, "INSERT"),
          _tl("FN_A", 41, "COMMENTED_OUT", commented=True)]
    steps = fan_in_steps_from_tagged_lines(tl, "TGT")
    assert len(steps) == 1 and steps[0]["line_start"] == 40


def test_fanin_drops_reads_in_writerless_function():
    tl = [_tl("FN_R", 10, "READ"), _tl("FN_R", 11, "FILTER")]  # no writer
    assert fan_in_steps_from_tagged_lines(tl, "TGT") == []


def test_fanin_transform_and_parameter_are_context_not_writers():
    tl = [_tl("FN_A", 40, "INSERT"), _tl("FN_A", 42, "TRANSFORM"),
          _tl("FN_A", 44, "PARAMETER")]
    steps = fan_in_steps_from_tagged_lines(tl, "TGT")
    writes = [s for s in steps if s["edge_kind"] == "writes"]
    reads = [s for s in steps if s["edge_kind"] == "reads"]
    assert len(writes) == 1                      # only the INSERT writes
    assert len(reads) == 2                       # TRANSFORM + PARAMETER are context
    assert all(r["successor"] == "FN_A:INSERT:L40" for r in reads)


def test_fanin_empty_when_no_target_or_no_active_lines():
    assert fan_in_steps_from_tagged_lines([_tl("FN_A", 40, "INSERT")], "") == []
    assert fan_in_steps_from_tagged_lines([], "TGT") == []


def test_fanin_end_to_end_solid_under_verified_body():
    ms = {}
    ms.update(_ms("FN_A", {38: "SELECT x FROM SRC", 40: "INSERT INTO T (TGT)"}))
    ms.update(_ms("FN_B", {12: "TGT := compute()"}))
    tl = [_tl("FN_A", 40, "INSERT"), _tl("FN_A", 38, "READ"), _tl("FN_B", 12, "ASSIGN")]
    steps = fan_in_steps_from_tagged_lines(tl, "TGT")
    d = build_trace_diagram(target="TGT", trace_kind="fan-in", multi_source=ms,
                            grounding=VERIFIED_BODY, fan_in_steps=steps)
    assert d is not None and d["trace_kind"] == "fan-in"
    writes = [e for e in d["edges"] if e["kind"] == "writes"]
    assert len(writes) == 2 and all(e["grounding"] == "VERIFIED" for e in writes)
    # sink is synthesized + UNVERIFIED (no span of its own) by design
    sink = _node(d, "TGT")
    assert sink["kind"] == "target-column"
    assert sink["citation"]["grounding"] == "UNVERIFIED"


def test_fanin_end_to_end_respects_ceiling():
    ms = _ms("FN_A", {40: "INSERT INTO T (TGT)"})
    tl = [_tl("FN_A", 40, "INSERT")]
    steps = fan_in_steps_from_tagged_lines(tl, "TGT")
    d = build_trace_diagram(target="TGT", trace_kind="fan-in", multi_source=ms,
                            grounding=UNVERIFIED_BODY, fan_in_steps=steps)
    assert d["diagram_grounding"] == "UNVERIFIED"
    assert all(e["grounding"] == "UNVERIFIED" for e in d["edges"])
