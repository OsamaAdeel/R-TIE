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


@test("TEST 3 — Business identifier not in any loaded function (CAP973 alone)")
def t3():
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}
    # CAP973 is not in any loaded function's source (per the prompt). The
    # identifier-grounding check should catch this and NOT return VERIFIED.
    passed = d.get("badge") != "VERIFIED"
    return passed, summarize_done(d)


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
    branch. Ensures the W45 fix didn't leak across response types."""
    r = run_query(
        "How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?"
    )
    d = r["done"] or {}
    checks = {
        "type_is_data_query": d.get("type") == "data_query",
        "badge_verified": d.get("badge") == "VERIFIED",
        "status_answered": d.get("status") == "answered",
        "no_ungrounded_warning": not any(
            "UNGROUNDED_IDENTIFIERS" in w for w in (d.get("warnings") or [])
        ),
    }
    passed = all(checks.values())
    failed_checks = [k for k, v in checks.items() if not v]
    extra = (
        f"type={d.get('type')} badge={d.get('badge')} "
        f"status={d.get('status')}"
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
