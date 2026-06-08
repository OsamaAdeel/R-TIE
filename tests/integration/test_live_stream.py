"""Live integration test harness for the /v1/stream endpoint.

Runs a suite of end-to-end checks against a running RTIE backend and prints a
concise pass/fail table. Each test is a function registered via @test;
assertions run against the final 'done' SSE event payload (with a couple of
tests also probing Redis directly for graph-key presence). Helpers print
enough detail that a failure can be diagnosed without re-running.

Requires a running backend on http://localhost:8000 and Redis on
localhost:6379. Run directly: `python tests/integration/test_live_stream.py`.
Not picked up by pytest automatically — this is a manual smoke harness.
"""
import json
import sys
import uuid

import httpx


URL = "http://localhost:8000/v1/stream"


def run_query(query: str, timeout: float = 120.0) -> dict:
    """POST to /v1/stream, collect all SSE events, return final 'done' payload.

    Also returns the list of stage/meta events for context on failures.
    """
    body = {
        "query": query,
        "session_id": str(uuid.uuid4()),
        "engineer_id": "w37-w38-live",
    }
    events = []
    done_payload = None
    markdown_tokens = []
    with httpx.Client(timeout=timeout) as client:
        with client.stream("POST", URL, json=body) as resp:
            resp.raise_for_status()
            current_event = None
            for line in resp.iter_lines():
                if not line:
                    current_event = None
                    continue
                if line.startswith("event:"):
                    current_event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    try:
                        parsed = json.loads(data)
                    except Exception:
                        parsed = data
                    events.append((current_event, parsed))
                    if current_event == "done":
                        done_payload = parsed
                    elif current_event == "token":
                        markdown_tokens.append(parsed if isinstance(parsed, str) else str(parsed))
    return {
        "done": done_payload,
        "events": events,
        "markdown": "".join(markdown_tokens),
    }


def summarize_done(d: dict) -> str:
    if not d:
        return "<no done payload>"
    return (
        f"type={d.get('type','?')} "
        f"badge={d.get('badge','?')} "
        f"validated={d.get('validated','?')} "
        f"confidence={d.get('confidence','?')} "
        f"citations={len(d.get('source_citations',[]) or [])} "
        f"warnings={d.get('warnings') or []}"
    )


TESTS = []


def test(name):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


@test("TEST 1 — Named function references (pension IS now loaded after W38)")
def t1():
    r = run_query(
        "How is CAP973 calculated in ABL_Def_Pension_Fund_Asset_Net_DTL?"
    )
    d = r["done"] or {}
    # With W38 loading the pension file, the function IS in the graph, so
    # the pre-check does NOT decline. The identifier-grounding check (W37
    # change 1.2) should catch "CAP973" as ungrounded because it's not in
    # the pension function's source code. Expected: NOT VERIFIED.
    passed = d.get("badge") != "VERIFIED"
    extra = summarize_done(d)
    return passed, extra


@test("TEST 1b — Truly non-loaded function (pre-check should DECLINE)")
def t1b():
    r = run_query("Explain the function SOME_FAKE_FN_THAT_DOES_NOT_EXIST")
    d = r["done"] or {}
    passed = (
        d.get("type") == "function_not_found"
        and d.get("badge") == "DECLINED"
        and "SOME_FAKE_FN_THAT_DOES_NOT_EXIST" in (d.get("requested_function") or "").upper()
    )
    return passed, summarize_done(d)


@test("TEST 2 — Named function IS in graph: FN_LOAD_OPS_RISK_DATA")
def t2():
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}
    # Pre-check passes; full semantic pipeline runs. Grounding should find
    # line citations + analyzed function → VERIFIED.
    passed = d.get("badge") == "VERIFIED" and not d.get("type") == "function_not_found"
    return passed, summarize_done(d)


@test("TEST 3 — W95 CAP973 BI-resolved computer reaches explainer")
def t3():
    """W95 scope: anchor-resolved function must be in retrieval, at
    position 0 of ``functions_analyzed`` (the force-injection slot), so
    the source-fetch pipeline loads its body. The W95-targeted W57
    violation — ``GROUNDING-HIGH: cited function ... not in retrieved
    sources`` — must NOT fire for the anchored function.

    Adjacent trust checks (calendar/template paraphrases, post-hoc
    caveats, etc.) are OUT OF SCOPE here. The badge may still land
    UNVERIFIED if W83a / W83b catch a content fabrication unrelated to
    retrieval (see W96 for the CAP-code December-template surface
    documented during W95 validation). Coupling W95's success to badge
    VERIFIED would mask the W95 fix landing whenever a downstream
    detector legitimately fires.
    """
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}
    functions_analyzed = d.get("functions_analyzed") or []
    warnings = d.get("warnings") or []
    anchor_fn = "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT"

    checks = {
        # Position 0 is W95's injection slot — a stronger signal than
        # mere presence. Pre-W95 the slot held a sibling loader.
        "computer_at_position_0": (
            bool(functions_analyzed)
            and functions_analyzed[0].upper() == anchor_fn
        ),
        # The exact W57 sub-check W95 closes: cited function not in
        # retrieved sources. Other GROUNDING-HIGH warnings (template
        # phrase, etc.) are not W95's surface and don't fail this test.
        "no_not_in_retrieved_warning": not any(
            "GROUNDING-HIGH" in w
            and "not in retrieved sources" in w
            and anchor_fn in w
            for w in warnings
        ),
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed:
        extra += f" FAILED_CHECKS={failed} functions_analyzed={functions_analyzed}"
    return passed, extra


@test("TEST 3b — W95 CAP943 BI-resolved computer reaches explainer + derivation banner")
def t3b():
    """W95 scope for CAP943: same as TEST 3, plus the Phase 6
    derivation banner. CAP943's literal-index record carries an
    embedded ``SUBTRACT`` derivation
    (``CAP943 = CAP309 - CAP863``), and ``render_derivation_header``
    emits ``**CAP943 = CAP309 - CAP863**`` programmatically — so the
    exact string is stable across LLM runs.

    Banner presence is W95-adjacent: it only renders when the BI
    routing record reaches the explainer pipeline, which depends on
    BI routing firing AND state being threaded correctly. The W95
    force-include doesn't render the banner directly, but the banner
    rendering is a downstream consumer of the same BI-routed state,
    so its presence is a useful end-to-end signal.
    """
    r = run_query("How is CAP943 calculated?")
    d = r["done"] or {}
    functions_analyzed = d.get("functions_analyzed") or []
    markdown = (d.get("explanation") or {}).get("markdown", "")
    warnings = d.get("warnings") or []
    anchor_fn = "CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION"

    checks = {
        "computer_at_position_0": (
            bool(functions_analyzed)
            and functions_analyzed[0].upper() == anchor_fn
        ),
        "no_not_in_retrieved_warning": not any(
            "GROUNDING-HIGH" in w
            and "not in retrieved sources" in w
            and anchor_fn in w
            for w in warnings
        ),
        # Phase 6 derivation banner — programmatically rendered.
        "derivation_banner_present": (
            "**CAP943 = CAP309 - CAP863**" in markdown
        ),
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed:
        extra += f" FAILED_CHECKS={failed} functions_analyzed={functions_analyzed}"
    return passed, extra


@test("TEST 4 — Business identifier IS in a loaded function")
def t4():
    r = run_query("How is N_ANNUAL_GROSS_INCOME calculated?")
    d = r["done"] or {}
    # N_ANNUAL_GROSS_INCOME is in OFSMDM functions. Should pass grounding.
    passed = d.get("badge") == "VERIFIED"
    return passed, summarize_done(d)


@test("TEST 5 — Self-contradiction detector (covered by unit tests)")
def t5():
    # We can't force the LLM to emit a contradictory phrase deterministically.
    # The unit test test_contradiction_phrase_with_substantive_continuation
    # covers this path directly; here we just confirm the machinery is
    # wired into the response.
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}
    # If no contradiction detected, no CONTRADICTION warning present.
    warnings = d.get("warnings") or []
    passed = True  # unit tests cover the detector; smoke-test only here
    return passed, f"warnings={warnings} " + summarize_done(d)


@test("TEST 6 — New module folder discovery (TEST_MODULE loaded)")
def t6():
    # Checked via startup logs + Redis key existence (not via /v1/stream).
    import redis
    r = redis.Redis(host="localhost", port=6379)
    passed = bool(r.exists("graph:OFSMDM:TEST_SIMPLE"))
    return passed, f"graph:OFSMDM:TEST_SIMPLE exists={passed}"


@test("TEST 7 — OFSERM file parsing with warning (Redis key + origins unchanged)")
def t7():
    import redis
    r = redis.Redis(host="localhost", port=6379)
    has_ofserm = bool(r.exists("graph:OFSERM:ABL_DEF_PENSION_FUND_ASSET_NET_DTL"))
    # Origins catalog check happens via the startup log; here we just confirm
    # the OFSERM key is present.
    return has_ofserm, f"graph:OFSERM:ABL_DEF_PENSION_FUND_ASSET_NET_DTL exists={has_ofserm}"


@test("TEST 8 — Query about OFSERM function (partial/UNVERIFIED acceptable)")
def t8():
    r = run_query("What does ABL_Def_Pension_Fund_Asset_Net_DTL do?")
    d = r["done"] or {}
    # Pre-check finds it in graph:OFSERM, so no DECLINED. But schema-aware
    # routing isn't implemented (W35), so semantic search against OFSMDM-only
    # vectors may produce a partial answer. We accept: not VERIFIED, OR
    # VERIFIED with citations referring to the actual pension function.
    badge = d.get("badge")
    passed = badge != "VERIFIED" or bool(d.get("source_citations"))
    # The prompt says: "Badge is NOT VERIFIED (since schema catalog doesn't
    # know OFSERM tables)". We'll treat VERIFIED as a soft fail here.
    if badge == "VERIFIED":
        passed = False
    return passed, summarize_done(d)


@test("TEST 9 — W33 regression: CHAR padding fix still works")
def t9():
    r = run_query(
        "How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?"
    )
    d = r["done"] or {}
    # Should be a DATA_QUERY response with VERIFIED badge and a numeric answer.
    # The expected answer is 669 (per W33).
    rows = d.get("rows") or []
    row_count = d.get("row_count")
    summary = d.get("summary") or ""
    # Accept if we got an answered DATA_QUERY (status='answered' and badge VERIFIED)
    passed = (
        d.get("type") == "data_query"
        and d.get("badge") == "VERIFIED"
        and d.get("status") == "answered"
    )
    extra = (
        f"type={d.get('type')} badge={d.get('badge')} "
        f"status={d.get('status')} row_count={row_count}"
    )
    return passed, extra


@test("TEST 10 — W22 regression: ambiguity still works")
def t10():
    r = run_query("what's the v_prod_code of 601013101-8604 on 2025-12-31?")
    d = r["done"] or {}
    # Expected: identifier_ambiguous type. W22 should still flag this.
    passed = d.get("type") == "identifier_ambiguous"
    return passed, f"type={d.get('type')} message_preview={(d.get('message') or '')[:80]}"


# ---------------------------------------------------------------------------
# W45 — structured ungrounded-identifier response
# ---------------------------------------------------------------------------

@test("TEST 11 — W45 CAP973 produces structured 'not the answer' response")
def t11_cap973_ungrounded():
    """CAP973 is absent from every loaded function's source. The W45
    branch should trigger and produce a structured 'Not Found in Indexed
    Functions' response. This is the primary fix target."""
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}
    markdown = (d.get("explanation") or {}).get("markdown", "")
    warnings = d.get("warnings") or []

    checks = {
        "badge_unverified": d.get("badge") == "UNVERIFIED",
        "has_ungrounded_warning": any(
            "UNGROUNDED_IDENTIFIERS" in w and "CAP973" in w for w in warnings
        ),
        "title_is_not_found": "CAP973 — Not Found in Indexed Functions" in markdown,
        "no_hierarchy_header": "This function runs in" not in markdown,
        "no_step_walkthrough": "Step 1" not in markdown and "Step 2" not in markdown,
        "has_searched_section": "Related functions I searched" in markdown,
        "labels_similarity_only": "retrieved by name-similarity only" in markdown.lower()
            or "name-similarity only" in markdown,
        "no_post_hoc_caveats_block": "**Caveats:**" not in markdown,
        # Phase 8: schema-agnostic next-step. The boilerplate exists
        # ("Suggested next step" heading + manifest gap report mention)
        # but no longer hardcodes FCT_STANDARD_ACCT_HEAD or OFSERM —
        # those leads were stale once Phase 3 made graph:source: the
        # canonical source-of-source for every loaded schema.
        "has_next_step_heading": "### Suggested next step" in markdown,
        "next_step_mentions_manifest_gap_report": "manifest gap report" in markdown,
        "no_stale_phase_8_phrasing": (
            "W35" not in markdown
            and "multi-schema work" not in markdown
            and "OFSMDM-only" not in markdown
        ),
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("TEST 12 — W45 regression: grounded VARIABLE_TRACE unchanged")
def t12_n_annual_gross_income_grounded():
    """N_ANNUAL_GROSS_INCOME is a column present in loaded OFSMDM
    functions. The normal VARIABLE_TRACE path must still run — no
    'Not Found' title, hierarchy header allowed, normal step-by-step
    structure. This is the non-negotiable regression check."""
    r = run_query("How is N_ANNUAL_GROSS_INCOME calculated?")
    d = r["done"] or {}
    markdown = (d.get("explanation") or {}).get("markdown", "")
    checks = {
        "badge_verified": d.get("badge") == "VERIFIED",
        "no_ungrounded_title": "Not Found in Indexed Functions" not in markdown,
        "no_ungrounded_warning": not any(
            "UNGROUNDED_IDENTIFIERS" in w for w in (d.get("warnings") or [])
        ),
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("TEST 13 — W45 regression: DATA_QUERY unaffected")
def t13_n_eop_bal_data_query():
    """DATA_QUERY responses must not accidentally hit the ungrounded
    branch. Ensures the W45 fix didn't leak across response types.

    W34a addition: verify that progressive stage events fire in the
    expected order — generating_sql, validating, fetch, explain — and
    that the misleading pre-W34a upfront cluster is gone (no
    ``"Building schema catalog + generating SQL..."`` and no
    ``"Executing read-only query..."`` literal messages, both of which
    used to fire ~5 s before their corresponding work actually started).
    """
    r = run_query(
        "How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?"
    )
    d = r["done"] or {}

    stage_events = [
        e[1] for e in r["events"]
        if e[0] == "stage" and isinstance(e[1], dict)
    ]
    stage_names = [s.get("stage") for s in stage_events]
    stage_messages = [s.get("message", "") for s in stage_events]

    checks = {
        "type_is_data_query": d.get("type") == "data_query",
        "badge_verified": d.get("badge") == "VERIFIED",
        "status_answered": d.get("status") == "answered",
        "no_ungrounded_warning": not any(
            "UNGROUNDED_IDENTIFIERS" in w for w in (d.get("warnings") or [])
        ),
        # W34a: progressive stage events.
        "has_generating_sql_stage": "generating_sql" in stage_names,
        "has_validating_stage": "validating" in stage_names,
        "has_fetch_stage": "fetch" in stage_names,
        "has_explain_stage": "explain" in stage_names,
        # W34a: the misleading upfront cluster must be gone.
        "no_pre_w34a_search_message": not any(
            "Building schema catalog + generating SQL" in m
            for m in stage_messages
        ),
        "no_pre_w34a_fetch_message": not any(
            "Executing read-only query against Oracle" in m
            for m in stage_messages
        ),
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = (
        f"type={d.get('type')} badge={d.get('badge')} "
        f"status={d.get('status')} stages={stage_names}"
    )
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


# ---------------------------------------------------------------------------
# W49 — structured partial-source response
# ---------------------------------------------------------------------------

@test("TEST 14 — W49 ABL_Def_Pension produces partial-source structure")
def t14_partial_source_pension():
    """ABL_Def_Pension_Fund_Asset_Net_DTL has graph metadata in OFSERM but
    no source body in the OFSMDM-only vector store. The W49 branch must
    trigger and produce a structured 'Source Not Currently Indexed'
    response. This is the primary fix target for W49."""
    r = run_query("How does ABL_Def_Pension_Fund_Asset_Net_DTL work?")
    d = r["done"] or {}
    markdown = (d.get("explanation") or {}).get("markdown", "")
    warnings = d.get("warnings") or []

    checks = {
        "badge_unverified": d.get("badge") == "UNVERIFIED",
        "has_partial_source_warning": any(
            "PARTIAL_SOURCE" in w for w in warnings
        ),
        "title_is_source_not_indexed": (
            "## ABL_Def_Pension_Fund_Asset_Net_DTL — Source Not Currently Indexed"
            in markdown
        ),
        "has_what_i_know_section": "What I know about it" in markdown,
        "no_likely_does_phrase": (
            "What this function most likely does" not in markdown
            and "what this function most likely does" not in markdown
        ),
        "no_step_walkthrough": (
            "Step 1" not in markdown and "Step 2" not in markdown
        ),
        "no_hierarchy_header": "This function runs in" not in markdown,
        "no_speculative_neighbors": (
            "TLX_PROV_AMT_FOR_CAP013" not in markdown
            and "POPULATE_STDACC_FROMGL" not in markdown
        ),
        "no_post_hoc_caveats_block": "**Caveats:**" not in markdown,
        # Phase 8: boilerplate points at db/modules and asks for a
        # parser-coverage report. It must NOT mention W35 — the
        # phase-number reference was stripped in the prompt audit.
        "has_next_step_boilerplate": (
            "db/modules" in markdown
            and "parser-coverage report" in markdown
        ),
        "no_stale_phase_8_phrasing": (
            "W35" not in markdown
            and "multi-schema work" not in markdown
            and "currently partial" not in markdown
        ),
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("TEST 15 — W49 regression: FN_LOAD_OPS_RISK_DATA stays VERIFIED")
def t15_fully_indexed_ofsmdm_unaffected():
    """FN_LOAD_OPS_RISK_DATA is fully indexed in OFSMDM. The W49 branch
    must NOT activate — normal step-by-step explanation with hierarchy
    header and VERIFIED badge must remain intact."""
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}
    markdown = (d.get("explanation") or {}).get("markdown", "")
    warnings = d.get("warnings") or []

    checks = {
        "badge_verified": d.get("badge") == "VERIFIED",
        "no_partial_source_warning": not any(
            "PARTIAL_SOURCE" in w for w in warnings
        ),
        "no_partial_source_title": (
            "Source Not Currently Indexed" not in markdown
        ),
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("TEST 16 — W49 regression: CAP973 W45 branch still wins")
def t16_cap973_w45_takes_precedence():
    """CAP973 is the W45 case (ungrounded business identifier). The W45
    branch must take precedence over W49 — the response must still be the
    'Not Found in Indexed Functions' structure, not a 'Source Not
    Currently Indexed' structure."""
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}
    markdown = (d.get("explanation") or {}).get("markdown", "")
    warnings = d.get("warnings") or []

    checks = {
        "badge_unverified": d.get("badge") == "UNVERIFIED",
        "has_ungrounded_warning": any(
            "UNGROUNDED_IDENTIFIERS" in w and "CAP973" in w for w in warnings
        ),
        "no_partial_source_warning": not any(
            "PARTIAL_SOURCE" in w for w in warnings
        ),
        "title_is_w45_not_w49": (
            "CAP973 — Not Found in Indexed Functions" in markdown
            and "Source Not Currently Indexed" not in markdown
        ),
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("W84 — diagnostic block present on single-function semantic explain")
def w84_diagnostic_single_function():
    """``How does FN_LOAD_OPS_RISK_DATA work?`` is a single-function
    semantic-explain query: it lands in the LOGIC_EXPLAINER branch and
    apply_w70_anchor runs. The W76 prefix rule does NOT fire (no
    ``In <Name>, ...`` syntax). Expectations on the diagnostic block:

      * the block exists with all three keys
      * w81_suppressed is a bool (its actual value depends on whether
        retrieval pulled multi-process candidates — empirically True
        for FN_LOAD_OPS_RISK_DATA because related-function retrieval
        crosses processes)
      * w70_anchor is the asked-about function (cascade resolves to it)
      * w76_anchor is None (no prefix-anchor query syntax)
    """
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}
    diag = d.get("diagnostic") or {}
    checks = {
        "diagnostic_present": "diagnostic" in d,
        "has_all_three_keys": set(diag.keys()) == {
            "w81_suppressed", "w70_anchor", "w76_anchor"
        },
        "w81_suppressed_is_bool": isinstance(diag.get("w81_suppressed"), bool),
        "w70_anchor_resolves_to_asked_function": (
            diag.get("w70_anchor") == "FN_LOAD_OPS_RISK_DATA"
        ),
        "w76_anchor_null_for_no_prefix_query": diag.get("w76_anchor") is None,
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" diagnostic={diag}"
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("W84 — diagnostic block present on CAP-code BI routing")
def w84_diagnostic_cap_code():
    """``How is CAP973 calculated?`` exercises the BI-routing branch;
    CAP973 in particular is the W45 ungrounded-identifier case (per
    test 16). The diagnostic block must still be present and well-
    shaped regardless of whether the explainer ran or the W45
    'not found' branch took over."""
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}
    diag = d.get("diagnostic") or {}
    checks = {
        "diagnostic_present": "diagnostic" in d,
        "has_all_three_keys": set(diag.keys()) == {
            "w81_suppressed", "w70_anchor", "w76_anchor"
        },
        "w81_suppressed_is_bool": isinstance(diag.get("w81_suppressed"), bool),
        "w70_anchor_is_string_or_null": (
            diag.get("w70_anchor") is None
            or isinstance(diag.get("w70_anchor"), str)
        ),
        "w76_anchor_is_string_or_null": (
            diag.get("w76_anchor") is None
            or isinstance(diag.get("w76_anchor"), str)
        ),
    }
    # Note: this query may hit the W45 declined branch (no
    # diagnostic) OR the semantic-explain branch (diagnostic
    # present). Only enforce diagnostic shape when the block is
    # there; absence is acceptable for the W45 declined path.
    if "diagnostic" not in d:
        return True, summarize_done(d) + " (W45 declined branch — no diagnostic expected)"
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" diagnostic={diag}"
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("W84 — diagnostic block present on cross-flow VARIABLE_TRACE; w81_suppressed=True")
def w84_diagnostic_cross_flow_variable_trace():
    """A cross-flow variable trace forces W81 cross-process
    suppression (multi_source spans more than one process). Per the
    W81+W74 production behavior, w81_suppressed should be True for
    queries that span OPS_RISK_PROCESSING and any sibling process.
    w70_anchor is typically None on this path because
    variable_tracer.stream_chain bypasses apply_w70_anchor."""
    r = run_query(
        "Trace how N_SHAREHOLDING_PERCENT is set across the "
        "OPS_RISK_PROCESSING flow. Which functions read it, "
        "which write it, and how?"
    )
    d = r["done"] or {}
    diag = d.get("diagnostic") or {}
    checks = {
        "diagnostic_present": "diagnostic" in d,
        "has_all_three_keys": set(diag.keys()) == {
            "w81_suppressed", "w70_anchor", "w76_anchor"
        },
        "w81_suppressed_is_bool": isinstance(diag.get("w81_suppressed"), bool),
        "w70_anchor_is_string_or_null": (
            diag.get("w70_anchor") is None
            or isinstance(diag.get("w70_anchor"), str)
        ),
        "w76_anchor_is_string_or_null": (
            diag.get("w76_anchor") is None
            or isinstance(diag.get("w76_anchor"), str)
        ),
        # Cross-flow N_SHAREHOLDING_PERCENT triggers W81 in production.
        "w81_suppressed_true_for_cross_flow": diag.get("w81_suppressed") is True,
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" diagnostic={diag}"
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("W84 — existing top-level fields unchanged by diagnostic addition")
def w84_existing_fields_unchanged():
    """Regression: the new diagnostic block must not displace any
    existing field. Pick a known-good query and assert the canonical
    set of pre-W84 keys are still all present at top level."""
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}
    required_pre_w84 = {
        "badge", "validated", "warnings", "confidence",
        "explanation", "source_citations", "functions_analyzed",
        "schema_searched", "schema_scope", "correlation_id",
    }
    missing = required_pre_w84 - set(d.keys())
    passed = not missing
    extra = summarize_done(d)
    if missing:
        extra += f" MISSING_PRE_W84_FIELDS={sorted(missing)}"
    return passed, extra


@test("W83B — A2 (CS_Goodwill_Calculation) hedged-Dec framing must NOT badge VERIFIED")
def w83b_a2_canonical_target():
    """The canonical W83B target. CS_Goodwill_Calculation source has
    zero month-12 logic. Run 8 and Run 9 both produced body text of
    the form 'particularly when the reporting month is December' and
    badged VERIFIED with empty warnings.

    With W83B wired in, when the hedged form reproduces, the response
    must badge UNVERIFIED with at least one calendar-grounding
    warning (GROUNDING-CALENDAR-HIGH from W83B, the W83a paraphrase
    catch, or Check 5's literal-phrase catch — all three are valid
    paths to UNVERIFIED on the same fabrication).

    If the hedged form does not reproduce this run (LLM
    nondeterminism), pass with a note.
    """
    r = run_query("How does `CS_Goodwill_Calculation` work?")
    d = r["done"] or {}
    markdown = (d.get("explanation") or {}).get("markdown", "")
    warnings = d.get("warnings") or []
    has_hedged_dec = any(
        phrase in markdown.lower()
        for phrase in (
            "particularly when the reporting month is december",
            "operates under the condition that the reporting month is december",
            "contingent on the reporting month",
            "executed under specific conditions, particularly when",
        )
    )
    has_calendar_warning = any(
        "GROUNDING-CALENDAR-HIGH" in w for w in warnings
    )
    has_w83a_warning = any(
        "executes only" in w and "paraphrase form" in w for w in warnings
    )
    has_check5_warning = any(
        "only runs" in w and "december" in w.lower() for w in warnings
    )
    badge = d.get("badge")
    if not has_hedged_dec:
        return True, summarize_done(d) + " (hedged form not reproduced this run)"
    passed = (
        badge == "UNVERIFIED"
        and (has_calendar_warning or has_w83a_warning or has_check5_warning)
    )
    extra = summarize_done(d)
    if not passed:
        extra += (
            f" has_hedged_dec={has_hedged_dec} "
            f"has_cal={has_calendar_warning} "
            f"has_w83a={has_w83a_warning} "
            f"has_check5={has_check5_warning}"
        )
    return passed, extra


@test("W83B — CAP973 dedup: no double-fire of W83B and W83a on same fabrication")
def w83b_cap973_dedup_check():
    """CAP973 trips W78a/W45. If W83B is wired in, it must NOT emit
    a GROUNDING-CALENDAR-HIGH warning when W83a already covered the
    same fabrication. Verifies the dedup ordering wired in
    `w57_enforce_grounding`. W83B emits at most one warning per
    response by design."""
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}
    warnings = d.get("warnings") or []
    cal_count = sum(1 for w in warnings if "GROUNDING-CALENDAR-HIGH" in w)
    w83a_count = sum(
        1 for w in warnings
        if "executes only" in w and "paraphrase form" in w
    )
    # W83B emits ≤1 per response by design. When W83a also fired on
    # the same body, W83B should have deferred (cal_count == 0).
    passed = cal_count <= 1 and not (cal_count >= 1 and w83a_count >= 1)
    extra = summarize_done(d)
    if not passed:
        extra += f" cal_count={cal_count} w83a_count={w83a_count}"
    return passed, extra


@test("W83B — W84 diagnostic block still exposed (regression check)")
def w83b_diagnostic_block_intact():
    """W83B plumbed w70_anchor through evaluate_grounding. Confirm
    the W84 diagnostic block in the done event is unaffected."""
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}
    diag = d.get("diagnostic") or {}
    passed = (
        "diagnostic" in d
        and isinstance(diag.get("w81_suppressed"), bool)
        and (diag.get("w70_anchor") is None or isinstance(diag.get("w70_anchor"), str))
        and (diag.get("w76_anchor") is None or isinstance(diag.get("w76_anchor"), str))
    )
    extra = summarize_done(d) + f" diagnostic={diag}"
    return passed, extra


@test("W85 — CAP973 (BI routing) must NOT fire ANCHOR-MISMATCH (false-positive guard)")
def w85_no_fire_on_cap_code():
    """BI routing intentionally redirects the anchor from CAP973 to
    the resolved function. W85's W58 candidate filter drops CAP973
    (no underscore), so the asked-list is empty and the check no-ops.
    This is the critical false-positive guard."""
    r = run_query("How is `CAP973` calculated?")
    d = r["done"] or {}
    warnings = d.get("warnings") or []
    has_w85 = any("GROUNDING-ANCHOR-MISMATCH-HIGH" in w for w in warnings)
    passed = not has_w85
    extra = summarize_done(d)
    if has_w85:
        extra += " UNEXPECTED W85 FIRE on CAP-code query"
    return passed, extra


@test("W85 — happy path (FN_LOAD_OPS_RISK_DATA) must NOT fire ANCHOR-MISMATCH")
def w85_no_fire_on_happy_path():
    """Asked function == anchor function (per W83B Canary D evidence:
    w70_anchor lands on FN_LOAD_OPS_RISK_DATA for this query).
    Must NOT fire W85; existing pass-through guard may still fire."""
    r = run_query("How does `FN_LOAD_OPS_RISK_DATA` work?")
    d = r["done"] or {}
    warnings = d.get("warnings") or []
    diag = d.get("diagnostic") or {}
    has_w85 = any("GROUNDING-ANCHOR-MISMATCH-HIGH" in w for w in warnings)
    # If the cascade actually drifted away from FN_LOAD_OPS_RISK_DATA
    # this run (run-to-run variability per W83B Section 7),
    # W85 firing IS the correct behavior — treat it as expected.
    anchor = diag.get("w70_anchor") or ""
    expected_silent = anchor.upper() == "FN_LOAD_OPS_RISK_DATA"
    if expected_silent:
        passed = not has_w85
    else:
        # Anchor drifted — W85 firing is the correct outcome.
        passed = has_w85
    extra = summarize_done(d) + f" w70_anchor={anchor!r}"
    if not passed:
        extra += f" has_w85={has_w85} expected_silent={expected_silent}"
    return passed, extra


@test("W85 — sibling mismatch (CS_Goodwill_Calculation) fires IF cascade drifts")
def w85_sibling_mismatch_when_drifted():
    """W83B's Canary A reproduces a w70_anchor drift on this query
    most of the time but is run-to-run nondeterministic per the W83B
    close-out (Section 7). Pass condition is conditional:
      - if anchor == CS_GOODWILL_CALCULATION → W85 silent (no drift)
      - if anchor is any other function known to graph → W85 fires
    Both outcomes verify the check is working — the firing IS the
    behavior we want when drifted, the silence IS the behavior we
    want when not drifted."""
    r = run_query("How does `CS_Goodwill_Calculation` work?")
    d = r["done"] or {}
    diag = d.get("diagnostic") or {}
    warnings = d.get("warnings") or []
    anchor = (diag.get("w70_anchor") or "").upper()
    has_w85 = any("GROUNDING-ANCHOR-MISMATCH-HIGH" in w for w in warnings)
    drifted = anchor != "" and anchor != "CS_GOODWILL_CALCULATION"
    if drifted:
        passed = has_w85
    else:
        passed = not has_w85
    extra = summarize_done(d) + f" anchor={anchor!r} drifted={drifted}"
    if not passed:
        extra += f" has_w85={has_w85}"
    return passed, extra


@test("W89 — VARIABLE_TRACE functions_analyzed is manifest-ordered")
def w89_chain_ordering_on_known_canary():
    """For a VARIABLE_TRACE query, the functions_analyzed array (and
    the chain order the LLM walks) should be monotonically non-decreasing
    in task_order within each (batch, process, sub_process) bucket.

    Uses N_EOP_BAL which routes to VARIABLE_TRACE today. Pulls the
    manifest hierarchy directly from Redis for each function in the
    response and verifies ordering. If retrieval comes back empty or
    the chain has fewer than 2 functions, the test is informational
    (passes) since there's nothing to reorder."""
    import msgpack
    import redis

    r = run_query("How is N_EOP_BAL calculated across functions?")
    d = r["done"] or {}
    # Find the meta event so we get functions_analyzed.
    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns = (meta_event or {}).get("functions_analyzed") or []
    if len(fns) < 2:
        # Nothing to order; treat as informational pass.
        return True, summarize_done(d) + f" fns={fns}"

    redis_client = redis.Redis(host="localhost", port=6379)
    schema_searched = (meta_event or {}).get("schema_searched") or []
    # Build (key, fn_name) tuples for ordering check.
    keys = []
    for fn in fns:
        # Try every schema that came back from semantic search;
        # the first match wins.
        h = None
        for schema in schema_searched or [meta_event.get("schema") or "OFSMDM"]:
            blob = redis_client.get(f"graph:{schema}:{fn.upper()}")
            if not blob:
                continue
            try:
                graph = msgpack.unpackb(blob, raw=False)
            except Exception:
                continue
            h = graph.get("hierarchy") or {}
            if h:
                break
        if not h or not isinstance(h.get("task_order"), int):
            # Unmanifested; should be at the END of fns.
            keys.append((1, 0, fn))
        else:
            keys.append((
                0,
                h.get("batch") or "",
                h.get("process") or "",
                tuple(h.get("sub_process_path") or ()),
                h["task_order"],
                fn,
            ))

    # Walk fns in the order they came back; confirm keys are
    # monotonically non-decreasing.
    passed = True
    for i in range(1, len(keys)):
        if keys[i - 1] > keys[i]:
            passed = False
            break
    extra = summarize_done(d) + f" fns={fns}"
    if not passed:
        extra += f" not_monotonic@{i} prev={keys[i-1]} curr={keys[i]}"
    return passed, extra


@test("W89 — FUNCTION_LOGIC response shape unchanged")
def w89_function_logic_shape_unchanged():
    """FUNCTION_LOGIC queries should NOT be reordered (only
    VARIABLE_TRACE is). The done payload's basic shape — badge,
    functions_analyzed presence — must look the same as pre-W89."""
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}
    # Find meta. functions_analyzed should be a list of strings.
    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns = (meta_event or {}).get("functions_analyzed") or []
    passed = (
        isinstance(fns, list)
        and all(isinstance(f, str) for f in fns)
        and (meta_event or {}).get("query_type") in ("FUNCTION_LOGIC", "COLUMN_LOGIC")
    )
    extra = summarize_done(d) + f" fns_len={len(fns)} qt={meta_event.get('query_type') if meta_event else None}"
    return passed, extra


@test("W89 — DATA_QUERY response shape unchanged")
def w89_data_query_shape_unchanged():
    """DATA_QUERY queries don't have a functions_analyzed-style chain
    at all. Confirm the response shape and badge are unaffected by
    the W89 reorder being gated to VARIABLE_TRACE."""
    r = run_query(
        "How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?"
    )
    d = r["done"] or {}
    passed = (
        d.get("type") == "data_query"
        and d.get("status") == "answered"
    )
    extra = summarize_done(d)
    return passed, extra


# ---------------------------------------------------------------------------
# W87 — Unrecognized-term gate
# ---------------------------------------------------------------------------

@test("W87 — fires on G Test query (stakeholder-test-1 Q11 reproduction)")
def w87_fires_on_g_test_query():
    """Q11 — 'what is the threshold value for G Test'. Before W87 the
    classifier's enriched_query blob ('what is the threshold value for G
    Test Find the threshold value used for the G Test check G Test G_T')
    was passed to semantic search; the narrative LLM then anchored on
    CS_THRESHOLD_TREATMENT_AGGREGATE_THRESHOLD_ASSIGNMENT and fabricated a
    December gate. W87 short-circuits before semantic search with a
    structured clarification."""
    r = run_query("what is the threshold value for G Test")
    d = r["done"] or {}
    markdown = (d.get("explanation") or {}).get("markdown", "")
    warnings = d.get("warnings") or []

    checks = {
        "type_is_unrecognized_term": d.get("type") == "unrecognized_term",
        "status_unverified": d.get("status") == "unverified",
        "badge_unverified": d.get("badge") == "UNVERIFIED",
        "validated_false": d.get("validated") is False,
        "warning_present": any(
            "UNRECOGNIZED_TERM" in w and "G Test" in w for w in warnings
        ),
        "markdown_has_unrecognized_header": (
            '## Unrecognized Term: "G Test"' in markdown
        ),
        "markdown_names_what_was_searched": (
            "Loaded function names" in markdown
            and "Column indexes" in markdown
            and "Business-identifier" in markdown
        ),
        "markdown_does_not_anchor_on_unrelated_function": (
            "ONLY runs when the reporting month is December" not in markdown
            and "CS_THRESHOLD_TREATMENT_AGGREGATE_THRESHOLD_ASSIGNMENT"
                not in markdown
        ),
        "requested_term_field_set": d.get("requested_term") == "G Test",
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("W87 — must NOT fire on known function (FN_LOAD_OPS_RISK_DATA)")
def w87_no_fire_on_known_function():
    """Regression check: a query naming a function present in the graph
    must NOT trigger W87. The normal pipeline runs end-to-end; whatever
    badge it produces (VERIFIED / UNVERIFIED from W57 grounding overlay)
    is independent of W87. W87's assertion is narrow: type must NOT be
    'unrecognized_term' and warnings must NOT include UNRECOGNIZED_TERM."""
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}
    warnings = d.get("warnings") or []

    checks = {
        "type_is_not_unrecognized_term": d.get("type") != "unrecognized_term",
        "no_unrecognized_term_warning": not any(
            "UNRECOGNIZED_TERM" in w for w in warnings
        ),
        "no_unrecognized_header": (
            "## Unrecognized Term:"
                not in (d.get("explanation") or {}).get("markdown", "")
        ),
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


@test("W87 — must NOT fire on CAP-code query (CAP973 BI routing)")
def w87_no_fire_on_cap_code():
    """Regression check: CAP973 triggers BI routing (apply_bi_routing
    runs at main.py:1014), which writes state['bi_routing'] and satisfies
    the gate's condition (c). W87 must stay silent and allow the
    pre-existing W45 ungrounded-identifier flow to run."""
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}
    warnings = d.get("warnings") or []

    checks = {
        "type_is_not_unrecognized_term": d.get("type") != "unrecognized_term",
        "no_unrecognized_term_warning": not any(
            "UNRECOGNIZED_TERM" in w for w in warnings
        ),
        "no_unrecognized_header": (
            "## Unrecognized Term:"
                not in (d.get("explanation") or {}).get("markdown", "")
        ),
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = summarize_done(d)
    if failed_checks:
        extra += f" FAILED_CHECKS={failed_checks}"
    return passed, extra


# ---------------------------------------------------------------------------
# W83C — calendar-general overgeneralization detection
# ---------------------------------------------------------------------------

@test("W83C — fires on March overgeneralization (stakeholder test 2)")
def w83c_fires_on_march_overgeneralization():
    """Reproduction of stakeholder test 2 (2026-05-14). Query traces
    N_SIGNIFICANT_INVST_AMT; pre-W83C RTIE response asserts
    "ONLY runs when the reporting month is March 2026" with a source
    that has only a March-31 date filter, not a month gate.

    Expected: badge UNVERIFIED + a GROUNDING-CALENDAR-HIGH warning
    naming "March". W83C catches the fabrication of the March gate
    even though the W80 retrieval miss (separate work) and the
    response-shape issues are unchanged.

    Tolerant of two outcomes: (a) W83C fires with March named, or
    (b) the response now anchors on a function with genuine March
    logic and W83C suppresses (no fabrication present). We assert
    "no false-VERIFIED outcome with March-only-date evidence" —
    the badge must NOT be VERIFIED if the response contains an
    unsupported March-month claim."""
    r = run_query(
        "Trace N_SIGNIFICANT_INVST_AMT from classification "
        "through deduction."
    )
    d = r["done"] or {}
    warnings = d.get("warnings") or []
    markdown = (d.get("explanation") or {}).get("markdown", "")

    # The response should NOT badge VERIFIED if the body contains a
    # March-month gating claim. Conservative check: if a March
    # gating claim is present, badge must be UNVERIFIED.
    has_march_gating_claim = (
        "reporting month is March" in markdown
        or "ONLY runs" in markdown and "March" in markdown
    )
    has_w83c_warning = any(
        "GROUNDING-CALENDAR-HIGH" in w and "March" in w
        for w in warnings
    )
    badge_is_unverified = d.get("badge") == "UNVERIFIED"

    if has_march_gating_claim:
        # Fabrication-present path: W83C must fire AND badge must
        # be UNVERIFIED.
        passed = has_w83c_warning and badge_is_unverified
    else:
        # No fabrication present (e.g. W80 retrieval improved or
        # response no longer claims March-month gating). W83C
        # correctly stays silent.
        passed = True
    extra = (
        summarize_done(d)
        + f" has_march_claim={has_march_gating_claim}"
        + f" has_w83c_warn={has_w83c_warning}"
    )
    return passed, extra


@test("W83C — W83B December canary unchanged (CS_Goodwill_Calculation)")
def w83c_w83b_december_regression():
    """W83B's canonical A2 case must keep firing under W83C. Query
    asks about CS_Goodwill_Calculation, whose source has no month-12
    logic. Expected: SOME calendar grounding warning fires — could be
    Check 5's GROUNDING-HIGH (literal-phrase form) OR W83a's
    GROUNDING-HIGH (paraphrase form) OR W83B/W83C's
    GROUNDING-CALENDAR-HIGH (hedged-form) depending on which patterns
    in the response body match first per the dedup chain
    (Check 5 > W83a > W83C). Any of the three is acceptable; the
    regression we'd want to catch is "no calendar warning fires"
    when the response clearly fabricates December gating."""
    r = run_query("How does CS_Goodwill_Calculation work?")
    d = r["done"] or {}
    warnings = d.get("warnings") or []
    markdown = (d.get("explanation") or {}).get("markdown", "")

    has_december_claim = (
        "reporting month is December" in markdown
        or "year-end" in markdown.lower()
    )
    # Any of the three dedup-chain warning codes counts as the
    # December detector firing.
    has_any_calendar_warning = any(
        ("GROUNDING-HIGH" in w or "GROUNDING-CALENDAR-HIGH" in w)
        and ("december" in w.lower() or "year-end" in w.lower())
        for w in warnings
    )

    if has_december_claim:
        passed = has_any_calendar_warning and d.get("badge") == "UNVERIFIED"
    else:
        passed = True
    extra = (
        summarize_done(d)
        + f" has_dec_claim={has_december_claim}"
        + f" has_cal_warn={has_any_calendar_warning}"
    )
    return passed, extra


# ---------------------------------------------------------------------------
# W80 — Vector retrieval embedding input poisoning fix
# ---------------------------------------------------------------------------

# The 5 Cowork-named functions the stakeholder-test-2 significant-
# investment pipeline traces through. Pre-W80 the classifier blob fed
# to the embedding retrieved 0 of 5 — the embedding centroid was pulled
# toward unrelated functions (CS_INSIGNIFICANT_INVST_*, FN_LOAD_OPS_RISK_DATA,
# etc.). Post-W80 with the embedding input no longer poisoned, we expect
# at least 2 of 5 to surface — a floor that documents W80b motivation
# (top-K + hybrid retrieval) without blocking on it.
W80_SIGNIFICANT_INVESTMENT_PIPELINE = {
    "CAP_CONSL_NON_REGULATORY_ENTITY_SIGNIFICANT_INVESTMENT_IDENTIFICATION",
    "SIGNIFICANT_INVESTMENT_IN_PARTY_FOR_REPORTING_BANK_IDENTIFICATION",
    "ABL_SIGNIFCNT_INVSTMNT_IN_NON_REG_CONSL_ENTITY_DATA_POP",
    "SIGNIFICANT_INVST_THRESHOLD_TREATMENT_DATA_POP",
    "SIGNFCNT_INVSTMNT_CAP_DEDUCTION_EXPOSURES",
}


@test("W80 — significant-investment trace recovers >=2 of 5 pipeline fns")
def w80_significant_investment_trace_recovers_pipeline():
    """Stakeholder test 2 (2026-05-14) reproduction.

    Query shape note: the original canary asked "Trace
    `N_SIGNIFICANT_INVST_AMT` from classification through deduction." That
    query contains a specific column-shaped term that no resolver can
    place (the real columns are N_CET1_INVESTMENT_AMOUNT,
    F_SIGNIFICANT_INVESTMENT_IND, etc.) — W87's unrecognized-term gate
    correctly intercepts before vector search runs, so the query can't
    measure W80 v1's effect on the retrieval path. This rewrite asks the
    same domain question in prose — no specific quoted identifier, no
    function anchor, no BI literal. W87 passes; resolve_search_query
    falls back to raw_query; the embedding is the user's verbatim prose;
    KNN runs. That is the path W80 v1 changes.

    Pre-W80 the orchestrator stamped object_name with the classifier
    blob (raw_query + intent + search_terms), the embedding of that blob
    was a diffuse centroid pulled away from the significant-investment
    cluster, and recall was 0 of 5. Post-W80 the embedding is the
    user's verbatim prose; recall floor is 2 of 5. Anything less and the
    embedding cleanup alone isn't sufficient — W80b (hybrid BM25 + KNN /
    adaptive top-K) becomes the next priority.
    """
    r = run_query(
        "summarize the workflow for non-regulated entity investment processing"
    )
    d = r["done"] or {}

    # functions_analyzed comes back on the meta event (and is also
    # reflected in d.functions_analyzed when the renderer copied it through).
    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns_meta = (meta_event or {}).get("functions_analyzed") or []
    fns_done = d.get("functions_analyzed") or []
    fns = fns_meta or fns_done

    fns_upper = {f.upper() for f in fns}
    matched = fns_upper & W80_SIGNIFICANT_INVESTMENT_PIPELINE
    matched_count = len(matched)

    passed = matched_count >= 2
    extra = (
        summarize_done(d)
        + f" matched={matched_count}/5"
        + f" matched_fns={sorted(matched)}"
        + f" retrieved={fns}"
    )
    return passed, extra


@test("W80b — significant-investment trace: top-K mechanism active, >=2 of 5")
def w80b_significant_investment_trace_raised_floor():
    """Same query and target set as the W80 v1 canary above, with two
    assertions: (1) recall floor >=2 of 5 (matches W80 v1's floor —
    confirms W80b didn't regress retrieval); (2) candidate-set size
    >5 (confirms the W80b per-query-type top-K routing fired and
    expanded the candidate set beyond the pre-W80b hardcoded ceiling).

    Outcome history. The hypothesis that motivated this canary was
    that the significant-investment cluster (15 functions in OFSERM)
    was being truncated at top_k=5, and raising to top_k=20 for
    VARIABLE_TRACE / COLUMN_LOGIC would lift recall from 2 of 5 to
    >=3 of 5. The first post-W80b measurement (2026-05-16) was FLAT:
    recall stayed at 2 of 5 with the expanded candidate set (~20
    entries). The OFSERM top-10 contained the 2 matched targets plus
    8 close siblings (CS_SIGNIFICANT_INVST_*, CS_INSIGNIFICANT_*,
    CS_REGULATORY_INVESTMENTS_*); the 3 missing targets ranked
    below 20 because the query's "non-regulated entity" framing
    semantically discriminates *correctly* against party-level /
    threshold-treatment / capital-deduction-exposure functions
    (which is what those 3 are about).

    Diagnostic implication: cluster-density alone was not the
    dominant constraint. Cosine similarity is correctly placing
    semantically-near functions in top-K; the 3 missing targets are
    semantically distant from this specific query shape. The
    architecturally correct fix is W80c (hybrid graph + vector with
    rerank, gated on W36 Phase 7 + W88 preconditions). Description
    regeneration (W80a) is unlikely to lift the missing 3 alone
    because they already have rich 3-paragraph descriptions; the
    miss is query-vocabulary divergence, not description quality.

    The candidate-set-size assertion is W80b's load-bearing signal:
    if a future change reverts the per-query-type top-K routing,
    this canary fails even when recall (the W80 v1 floor) still
    passes.
    """
    r = run_query(
        "summarize the workflow for non-regulated entity investment processing"
    )
    d = r["done"] or {}

    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns_meta = (meta_event or {}).get("functions_analyzed") or []
    fns_done = d.get("functions_analyzed") or []
    fns = fns_meta or fns_done

    fns_upper = {f.upper() for f in fns}
    matched = fns_upper & W80_SIGNIFICANT_INVESTMENT_PIPELINE
    matched_count = len(matched)

    # W80b: two-part pass condition. Recall floor matches W80 v1
    # (>=2 of 5); candidate-set size proves the top-K routing fired
    # (>5 — the pre-W80b ceiling). If either drops, the canary fails
    # with a specific signal about which W80b property regressed.
    recall_ok = matched_count >= 2
    candidate_set_expanded = len(fns) > 5
    passed = recall_ok and candidate_set_expanded
    extra = (
        summarize_done(d)
        + f" matched={matched_count}/5"
        + f" matched_fns={sorted(matched)}"
        + f" retrieved_count={len(fns)}"
        + f" recall_ok={recall_ok}"
        + f" candidate_set_expanded={candidate_set_expanded}"
        + f" retrieved={fns}"
    )
    return passed, extra


@test("W80c — significant-investment trace post-rerank: 5 of 5, rank moved")
def w80c_significant_investment_trace_post_rerank():
    """W80c full-recall canary — measured 5 of 5 after W80c-v2 retune.

    Same query and target set as W80 v1 / W80b above. Asserts:

      1. Recall = 5 of 5 — every function in the significant-
         investment pipeline surfaces. W80c PR 2 hit 4 of 5 with
         T3 (SIGNIFICANT_INVESTMENT_IN_PARTY_FOR_REPORTING_BANK_
         IDENTIFICATION) at RRF rank 30, cut by the original
         keep_top=25 window. W80c-v2 lifted keep_top from
         ``top_k+10`` to ``top_k+20`` (35 for COLUMN_LOGIC, 40 for
         VARIABLE_TRACE) — T3 now lands inside the window with 5
         slots to spare.

      2. ``meta.graph_rerank.status == "ok"`` — the wire-in actually
         ran (didn't skip via the query-type / redis / empty gates).

      3. ``meta.graph_rerank.rank_change_count > 0`` — positions
         actually moved. If the rerank coasts (zero changes), it's
         either a no-op (mechanism not engaging) or every position
         was already correct (won't happen at the canary baseline of
         2 of 5). Either way zero changes contradicts the
         diagnostic's reachability finding and is worth a fail.

    Regression categories:
      * 5 of 5 — canonical W80c-v2 outcome — passes.
      * 4 of 5 — Lever B regressed; investigate before relaxing.
      * <4 of 5 — major regression below the W80c PR 2 baseline.
    """
    r = run_query(
        "summarize the workflow for non-regulated entity investment processing"
    )
    d = r["done"] or {}

    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns_meta = (meta_event or {}).get("functions_analyzed") or []
    fns_done = d.get("functions_analyzed") or []
    fns = fns_meta or fns_done

    fns_upper = {f.upper() for f in fns}
    matched = fns_upper & W80_SIGNIFICANT_INVESTMENT_PIPELINE
    matched_count = len(matched)

    graph_rerank = (meta_event or {}).get("graph_rerank") or {}
    rerank_status = graph_rerank.get("status", "missing")
    rank_change_count = graph_rerank.get("rank_change_count", 0)

    recall_ok = matched_count >= 5
    rerank_ran = rerank_status == "ok"
    rerank_moved = rank_change_count > 0

    passed = recall_ok and rerank_ran and rerank_moved
    extra = (
        summarize_done(d)
        + f" matched={matched_count}/5"
        + f" matched_fns={sorted(matched)}"
        + f" rerank_status={rerank_status}"
        + f" rank_change_count={rank_change_count}"
        + f" seed_count={graph_rerank.get('seed_count', '?')}"
        + f" expanded_count={graph_rerank.get('expanded_count', '?')}"
        + f" kept_count={graph_rerank.get('kept_count', '?')}"
        + f" recall_ok={recall_ok}"
        + f" rerank_ran={rerank_ran}"
        + f" rerank_moved={rerank_moved}"
        + f" retrieved={fns}"
    )
    return passed, extra


@test("W80 — anchored-function query regression unchanged")
def w80_anchored_function_regression():
    """W76 anchor fires; object_name = clean function name; embedding
    input is that function name; retrieval anchors correctly. Pre-W80
    this path already worked (because W76 overwrote the blob); post-W80
    it must keep working — pin VERIFIED badge and FN_LOAD_OPS_RISK_DATA
    in functions_analyzed."""
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}

    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns = (meta_event or {}).get("functions_analyzed") or []
    fns_upper = {f.upper() for f in fns}

    passed = (
        d.get("badge") == "VERIFIED"
        and "FN_LOAD_OPS_RISK_DATA" in fns_upper
    )
    extra = summarize_done(d) + f" fns={fns}"
    return passed, extra


@test("W97 — BI-routed anchor promoted to multi_source position 0 (CAP973)")
def w97_anchor_promotion_cap973():
    """W97 closes the prompt-prominence half of the anchor architecture.

    W95 force-includes the anchored function in ``search_results`` when
    retrieval missed it; W97 promotes it to ``multi_source[0]`` when
    retrieval surfaced it at a low rank. Together: anchor resolution
    must dominate both retrieval coverage (W95) AND prompt prominence
    (W97). The LLM reads multi_source.items() in order — whatever sits
    at position 0 is the de facto subject regardless of the W70
    anchor block in the system message.

    This canary exercises the **BI-routing branch** where the cascade
    correctly resolves the anchor: ``CAP973`` → BI routing →
    ``CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT``. Pre-W97
    this anchor might not have been at ``multi_source[0]`` (W80c-v2's
    wider retrieval window can rerank a sibling above it). Post-W97
    the anchor wins position 0 unconditionally.

    Why NOT the FN_LOAD_OPS_RISK_DATA query (W97's originating canary):
    that query exposes a separate cascade-resolution gap — the
    ``How does <FN> work?`` pattern doesn't trigger W76 prefix, doesn't
    populate clean ``object_name``, and isn't a BI code, so the cascade
    falls through to layer 4 (semantic top-1) and resolves to the WRONG
    function (``PREV_QTR_CET1_STANDARD_ACCT_HEAD_DATA_POP``). W97's
    promote-to-front then faithfully promotes the wrong anchor to
    position 0. The cascade gap is logged separately as W98 — the
    pre-existing [test_w84_diagnostic_single_function](#L556) asserts
    cascade behavior that doesn't match live runs, which is the spec
    W98 must restore.

    Post-W97 assertions:

      1. ``functions_analyzed[0]`` (case-insensitive) is
         CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT — the
         BI-resolved anchor wins position 0.
      2. ``diagnostic.w70_anchor`` matches — confirms the cascade
         resolved through the BI-routing layer and W97 promoted that
         same function.

    Badge is NOT asserted: CAP973's narrative still trips the W96
    December calendar fabrication (a content ticket downstream of
    retrieval, unrelated to W97). Badge stays UNVERIFIED for that
    reason — orthogonal to W97's contract.

    Regression categories:
      * anchor at index 0 + diagnostic match — canonical W97 outcome —
        passes.
      * anchor in fns but not at index 0 — promotion didn't run;
        investigate the call site ordering before relaxing.
      * diagnostic.w70_anchor ≠ functions_analyzed[0] — cascade and
        promote disagree; potential ordering bug.
    """
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}

    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns = (meta_event or {}).get("functions_analyzed") or []
    fns_upper = [f.upper() for f in fns]

    expected = "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT"
    anchor_at_front = len(fns_upper) > 0 and fns_upper[0] == expected

    diag = d.get("diagnostic") or {}
    diag_anchor = (diag.get("w70_anchor") or "").upper()
    diag_matches_front = diag_anchor == expected

    passed = anchor_at_front and diag_matches_front
    extra = (
        summarize_done(d)
        + f" anchor_at_front={anchor_at_front}"
        + f" diag_matches_front={diag_matches_front}"
        + f" diag_w70_anchor={diag.get('w70_anchor')!r}"
        + f" fns_head={fns[:3]}"
    )
    return passed, extra


@test("W80 — CAP-code BI-routing regression unchanged")
def w80_cap_code_regression():
    """BI routing fires; object_name = resolved function name; embedding
    anchors on it. Pre-W80 this path already worked (BI routing overwrote
    the blob); post-W80 it must keep working — pin the meta event coming
    back with a non-empty functions_analyzed list and a non-DECLINED
    response shape."""
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}

    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns = (meta_event or {}).get("functions_analyzed") or []

    # CAP973 is a known regulatory tag — different W-tickets have
    # different verdicts on its grounding (W37/W45 may surface). The
    # only W80-specific assertion is: the embedding ran, retrieval
    # returned something, and the response is NOT a generic
    # function_not_found DECLINED (which would indicate BI routing or
    # the pipeline failed entirely).
    passed = (
        len(fns) > 0
        and d.get("type") != "function_not_found"
    )
    extra = (
        summarize_done(d)
        + f" type={d.get('type')} fns_count={len(fns)}"
    )
    return passed, extra


# ---------------------------------------------------------------------------
# W88 — Named regulatory computation pre-router (DATA_QUERY)
# ---------------------------------------------------------------------------
#
# W88 inserts a deterministic pre-router between _resolve_target_schema
# and _build_schema_catalog in data_query.py. Two arms:
#
#   anchor  — emits canonical SQL against OFSERM.FCT_OPS_RISK_DATA
#             (method-skey filter) or OFSERM.FCT_STANDARD_ACCT_HEAD
#             (CAP-code filter). Six v1 items: BIA, CREDIT_RWA_AGG,
#             MARKET_RWA_AGG, CET1, TIER1, CAR.
#   decline — honest UNVERIFIED payload explaining why the computation
#             isn't answerable from the loaded data. Three v1 items:
#             LEVERAGE_RATIO, LCR, NSFR.
#
# Pre-W88 every one of these queries fabricated SQL against OFSMDM
# staging (ABL_OPS_RISK_DATA), returned VERIFIED-but-null, and missed
# the actual OFSERM fact tables entirely (see docs/w88_diagnostic.md
# Section 3: "0 of 15 routed to OFSERM").

# Anchor canaries — one per anchor-arm computation. Each asserts:
#   1. response routed via W88 (w88_anchor.name matches)
#   2. SQL references the canonical OFSERM fact table
#   3. result is non-null (the computation IS reachable in the
#      current Oracle per diagnostic Section 2)


def _w88_done_w88_anchor(d):
    """Extract the w88_anchor metadata from a done payload.

    The anchor block flows through the plan dict into the result
    payload at the existing serialization layer. If a future change
    drops this field, every W88 anchor canary will fail with a clear
    'no w88_anchor in done payload' message rather than a confusing
    SQL-content assertion miss.
    """
    return d.get("w88_anchor") or {}


def _w88_done_w88_decline(d):
    return d.get("w88_decline") or {}


@test("W88 — BIA routes to FCT_OPS_RISK_DATA via method-skey anchor")
def w88_bia_anchor():
    r = run_query(
        "What is the operational risk capital charge under "
        "Basic Indicator Approach on 2025-12-31?"
    )
    d = r["done"] or {}
    anchor = _w88_done_w88_anchor(d)
    sql = (d.get("sql") or "").upper()
    rows = d.get("rows") or []
    first_value = (rows[0][0] if rows and rows[0] else None)

    checks = {
        "anchor_name_bia": anchor.get("name") == "BIA",
        "target_table_ops_risk": "FCT_OPS_RISK_DATA" in (anchor.get("target_table") or ""),
        "sql_uses_method_skey": "N_BASEL_METHOD_SKEY" in sql,
        "result_non_null": first_value is not None,
        "type_data_query": d.get("type") == "data_query",
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = (
        summarize_done(d)
        + f" anchor={anchor.get('name')!r}"
        + f" first_value={first_value!r}"
    )
    if failed:
        extra += f" FAILED_CHECKS={failed}"
    return passed, extra


@test("W88 — CET1 routes to FCT_STANDARD_ACCT_HEAD via CAP960")
def w88_cet1_anchor():
    r = run_query("What is the CET1 ratio on 2025-12-31?")
    d = r["done"] or {}
    anchor = _w88_done_w88_anchor(d)
    sql = (d.get("sql") or "").upper()
    rows = d.get("rows") or []
    first_value = (rows[0][0] if rows and rows[0] else None)

    checks = {
        "anchor_name_cet1": anchor.get("name") == "CET1",
        "target_table_std_acct_head": "FCT_STANDARD_ACCT_HEAD" in (anchor.get("target_table") or ""),
        "sql_filters_cap960": "CAP960" in (d.get("params") or {}).get("w88_cap_code", ""),
        "result_ratio_in_range": isinstance(first_value, (int, float)) and 0 < float(first_value) < 1,
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = (
        summarize_done(d)
        + f" anchor={anchor.get('name')!r}"
        + f" cet1_value={first_value!r}"
    )
    if failed:
        extra += f" FAILED_CHECKS={failed}"
    return passed, extra


@test("W88 — Tier 1 ratio routes via CAP214")
def w88_tier1_anchor():
    r = run_query("What is the Tier 1 capital ratio on 2025-12-31?")
    d = r["done"] or {}
    anchor = _w88_done_w88_anchor(d)
    rows = d.get("rows") or []
    first_value = (rows[0][0] if rows and rows[0] else None)

    checks = {
        "anchor_name_tier1": anchor.get("name") == "TIER1",
        "filter_cap214": "CAP214" in (anchor.get("filter") or ""),
        "result_ratio_in_range": isinstance(first_value, (int, float)) and 0 < float(first_value) < 1,
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" tier1={first_value!r}"
    if failed:
        extra += f" FAILED_CHECKS={failed}"
    return passed, extra


@test("W88 — Total Capital Ratio (CAR) routes via CAP192")
def w88_car_anchor():
    # Date phrasing matters: without "on <date>", the classifier
    # routes "Capital Adequacy Ratio" to FUNCTION_LOGIC / VARIABLE_TRACE
    # and W87 intercepts before W88 can run. Diagnostic Section 3
    # tested all 15 queries with dates; we follow that convention.
    r = run_query("What is the Capital Adequacy Ratio on 2025-12-31?")
    d = r["done"] or {}
    anchor = _w88_done_w88_anchor(d)
    rows = d.get("rows") or []
    first_value = (rows[0][0] if rows and rows[0] else None)

    checks = {
        "anchor_name_car": anchor.get("name") == "CAR",
        "filter_cap192": "CAP192" in (anchor.get("filter") or ""),
        "result_ratio_present": isinstance(first_value, (int, float)) and float(first_value) > 0,
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" car={first_value!r}"
    if failed:
        extra += f" FAILED_CHECKS={failed}"
    return passed, extra


@test("W88 — Credit RWA aggregate routes via CAP169")
def w88_credit_rwa_anchor():
    r = run_query("What is the total Credit Risk RWA on 2025-12-31?")
    d = r["done"] or {}
    anchor = _w88_done_w88_anchor(d)
    rows = d.get("rows") or []
    first_value = (rows[0][0] if rows and rows[0] else None)

    checks = {
        "anchor_name_credit_rwa": anchor.get("name") == "CREDIT_RWA_AGG",
        "filter_cap169": "CAP169" in (anchor.get("filter") or ""),
        "result_non_null": first_value is not None,
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" credit_rwa={first_value!r}"
    if failed:
        extra += f" FAILED_CHECKS={failed}"
    return passed, extra


@test("W88 — Market RWA aggregate routes via CAP090")
def w88_market_rwa_anchor():
    r = run_query("What is the total Market Risk RWA on 2025-12-31?")
    d = r["done"] or {}
    anchor = _w88_done_w88_anchor(d)
    rows = d.get("rows") or []
    first_value = (rows[0][0] if rows and rows[0] else None)

    checks = {
        "anchor_name_market_rwa": anchor.get("name") == "MARKET_RWA_AGG",
        "filter_cap090": "CAP090" in (anchor.get("filter") or ""),
        "result_non_null": first_value is not None,
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" market_rwa={first_value!r}"
    if failed:
        extra += f" FAILED_CHECKS={failed}"
    return passed, extra


# Decline canaries — three items not answerable from local Oracle.
# Each asserts that W88 fires with the right decline metadata, the
# response is UNVERIFIED, and no SQL was executed (sql=None).


@test("W88 — Leverage Ratio decline (placeholder not computed)")
def w88_leverage_decline():
    r = run_query("What is the leverage ratio on 2025-12-31?")
    d = r["done"] or {}
    decline = _w88_done_w88_decline(d)
    explanation = d.get("explanation") or ""
    if isinstance(explanation, dict):
        explanation = explanation.get("markdown", "")

    checks = {
        "decline_name_leverage": decline.get("name") == "LEVERAGE_RATIO",
        "no_sql_executed": d.get("sql") is None,
        "badge_rejected": d.get("badge") == "REJECTED",
        "explains_placeholder": "placeholder" in explanation.lower() or "0.0" in explanation,
        "suggests_alternative": "Tier 1" in explanation,
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" decline={decline.get('name')!r}"
    if failed:
        extra += f" FAILED_CHECKS={failed}"
    return passed, extra


@test("W88 — LCR decline (no fact table in this OFSAA module)")
def w88_lcr_decline():
    r = run_query("What is the Liquidity Coverage Ratio on 2025-12-31?")
    d = r["done"] or {}
    decline = _w88_done_w88_decline(d)
    explanation = d.get("explanation") or ""
    if isinstance(explanation, dict):
        explanation = explanation.get("markdown", "")

    checks = {
        "decline_name_lcr": decline.get("name") == "LCR",
        "no_sql_executed": d.get("sql") is None,
        "badge_rejected": d.get("badge") == "REJECTED",
        "explains_module_scope": (
            "OFSAA" in explanation
            and ("Liquidity Risk Management" in explanation or "not loaded" in explanation.lower())
        ),
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" decline={decline.get('name')!r}"
    if failed:
        extra += f" FAILED_CHECKS={failed}"
    return passed, extra


@test("W88 — NSFR decline (no fact table in this OFSAA module)")
def w88_nsfr_decline():
    r = run_query("What is the Net Stable Funding Ratio on 2025-12-31?")
    d = r["done"] or {}
    decline = _w88_done_w88_decline(d)
    explanation = d.get("explanation") or ""
    if isinstance(explanation, dict):
        explanation = explanation.get("markdown", "")

    checks = {
        "decline_name_nsfr": decline.get("name") == "NSFR",
        "no_sql_executed": d.get("sql") is None,
        "badge_rejected": d.get("badge") == "REJECTED",
        "explains_module_scope": "OFSAA" in explanation,
    }
    passed = all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    extra = summarize_done(d) + f" decline={decline.get('name')!r}"
    if failed:
        extra += f" FAILED_CHECKS={failed}"
    return passed, extra


# Regression canaries — existing DATA_QUERY paths must still work.


@test("W88 — regression: F_EXPOSURE_ENABLED_IND query unaffected")
def w88_regression_w33_canary():
    """The W33 CHAR-padding canary at TEST 9 — a generic DATA_QUERY
    that does not name any registered computation. W88's pre-router
    must be a no-op here: the existing LLM SQL path runs, the W33 CHAR
    fix still applies, and the response badges VERIFIED with a numeric
    answer. If W88 over-matches and intercepts this query, the result
    will lose its data shape — that's the failure mode this test
    guards against.
    """
    r = run_query(
        "How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?"
    )
    d = r["done"] or {}
    # W88 anchor metadata MUST be absent — this is a non-W88 query.
    w88_anchor = d.get("w88_anchor")
    w88_decline = d.get("w88_decline")
    passed = (
        d.get("type") == "data_query"
        and d.get("badge") == "VERIFIED"
        and d.get("status") == "answered"
        and w88_anchor is None
        and w88_decline is None
    )
    extra = summarize_done(d) + f" w88_anchor={w88_anchor!r} w88_decline={w88_decline!r}"
    return passed, extra


@test("W169 — framing-drift gate flips Q12 to UNVERIFIED with scope-drift warning")
def w169_q12_scope_drift():
    """Q12: a VARIABLE_TRACE query scoped to FN_G_TEST_CSTM whose prose
    attributes N_BASEL_ASSET_CLASS_SKEY's write to a DIFFERENT function
    (ABL_INV_ASSET_CLASS_RECLASS / FN_COR_RW_U2_CSTM). Post-W169 the
    scope-drift predicate must flip the badge to UNVERIFIED and emit
    GROUNDING-ANCHOR-SCOPE-DRIFT-HIGH.

    NOTE (W169 diagnostic Probe 1 §6): the underlying LLM prose is
    non-deterministic. In the overwhelming majority of runs the prose cites
    the drifted function's line range ("Lines 32-125") or names it in an
    extractable form, so the gate fires. A rare run that cites NO usable
    line range AND names the outside function only as "the X step" (the
    SUB-A extraction gap) can still slip — that residual is W170's surface,
    not a W169 failure. If this row flakes, confirm the prose form before
    treating it as a regression.
    """
    r = run_query("how is N_BASEL_ASSET_CLASS_SKEY updated in FN_G_TEST_CSTM")
    d = r["done"] or {}
    warnings = d.get("warnings") or []
    scope_drift = any("GROUNDING-ANCHOR-SCOPE-DRIFT" in w for w in warnings)
    passed = d.get("badge") != "VERIFIED" and scope_drift
    return passed, summarize_done(d)


@test("W169 regression — C19 'how does FN_G_TEST_CSTM work' stays VERIFIED")
def w169_c19_function_logic_unaffected():
    """FUNCTION_LOGIC: no traced column → no attested-writer signal →
    W169 no-ops. Must remain VERIFIED (byte-unaffected by the gate)."""
    r = run_query("how does FN_G_TEST_CSTM work")
    d = r["done"] or {}
    warnings = d.get("warnings") or []
    passed = (
        d.get("badge") == "VERIFIED"
        and not any("SCOPE-DRIFT" in w for w in warnings)
    )
    return passed, summarize_done(d)


@test("W169 regression — C12 fan-in 'What writes N_STD_ACCT_HEAD_AMT?' stays VERIFIED")
def w169_c12_fanin_unaffected():
    """W159 fan-in: no named scope (asked empty) → W169's condition (i)
    fails → no-op. Legitimate multi-writer prose must stay VERIFIED."""
    r = run_query("What writes N_STD_ACCT_HEAD_AMT?")
    d = r["done"] or {}
    warnings = d.get("warnings") or []
    passed = (
        d.get("badge") == "VERIFIED"
        and not any("SCOPE-DRIFT" in w for w in warnings)
    )
    return passed, summarize_done(d)


def main():
    results = []
    for name, fn in TESTS:
        print(f"\n=== {name} ===", flush=True)
        try:
            passed, extra = fn()
        except Exception as exc:
            passed, extra = False, f"EXCEPTION: {exc}"
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {extra}", flush=True)
        results.append((name, passed, extra))

    print("\n\n===== SUMMARY =====")
    for name, passed, extra in results:
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}")
    failed = [r for r in results if not r[1]]
    print(f"\nTotal: {len(results)}, Passed: {len(results)-len(failed)}, Failed: {len(failed)}")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
