"""W98 verification canary battery — run after backend restart.

Four canaries from the W98 prompt's "Restart + Validation" §3:
  a) "How does FN_LOAD_OPS_RISK_DATA work?" — badge VERIFIED, w70_anchor
     == FN_LOAD, functions_analyzed[0] == FN_LOAD, neither GROUNDING-HIGH
     nor GROUNDING-ANCHOR-MISMATCH-HIGH fires.
  b) "How is CAP973 calculated?" — unchanged (W97 contract).
  c) "Trace N_EOP_BAL" — unchanged (W87 short-circuit).
  d) W80c significant-investment regression — recall 5/5 + rerank moved.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.integration.test_live_stream import (  # noqa: E402
    run_query,
    summarize_done,
    W80_SIGNIFICANT_INVESTMENT_PIPELINE,
)


def hr(title):
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _warnings_list(d):
    """Collect warning names from done.warnings (list of {name, ...})."""
    return [w.get("name") for w in (d.get("warnings") or []) if isinstance(w, dict)]


def canary_a_fn_load():
    hr('CANARY A — "How does FN_LOAD_OPS_RISK_DATA work?"')
    r = run_query("How does FN_LOAD_OPS_RISK_DATA work?")
    d = r["done"] or {}
    diag = d.get("diagnostic") or {}

    # functions_analyzed from meta event (W97 canary pattern).
    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns = (meta_event or {}).get("functions_analyzed") or []
    fns_upper = [f.upper() for f in fns]
    warnings = _warnings_list(d)

    print(f"badge:              {d.get('badge')!r}")
    print(f"diag.w70_anchor:    {diag.get('w70_anchor')!r}")
    print(f"diag.w76_anchor:    {diag.get('w76_anchor')!r}")
    print(f"functions_analyzed: {fns}")
    print(f"functions_analyzed[0] (upper): {fns_upper[0] if fns_upper else None!r}")
    print(f"warnings:           {warnings}")

    checks = {
        "badge_VERIFIED": d.get("badge") == "VERIFIED",
        "diag_w70_eq_FN_LOAD": diag.get("w70_anchor") == "FN_LOAD_OPS_RISK_DATA",
        "fns0_eq_FN_LOAD":
            len(fns_upper) > 0 and fns_upper[0] == "FN_LOAD_OPS_RISK_DATA",
        "no_GROUNDING_HIGH": "GROUNDING-HIGH" not in warnings,
        "no_ANCHOR_MISMATCH_HIGH":
            "GROUNDING-ANCHOR-MISMATCH-HIGH" not in warnings,
    }
    print()
    print("checks:")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    passed = all(checks.values())
    print()
    print(f"=> CANARY A {'PASS' if passed else 'FAIL'}")
    return passed


def canary_b_cap973():
    hr('CANARY B — "How is CAP973 calculated?" (W97 contract unchanged)')
    r = run_query("How is CAP973 calculated?")
    d = r["done"] or {}
    diag = d.get("diagnostic") or {}

    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns = (meta_event or {}).get("functions_analyzed") or []
    fns_upper = [f.upper() for f in fns]

    expected = "CS_REGULATORY_ADJUSTMENTS_PHASE_IN_DEDUCTION_AMOUNT"

    print(f"diag.w70_anchor:    {diag.get('w70_anchor')!r}")
    print(f"functions_analyzed[0]: {fns_upper[0] if fns_upper else None!r}")
    print(f"functions_analyzed head: {fns[:3]}")

    checks = {
        "diag_w70_eq_expected": (diag.get("w70_anchor") or "").upper() == expected,
        "fns0_eq_expected":
            len(fns_upper) > 0 and fns_upper[0] == expected,
    }
    print()
    print("checks:")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    passed = all(checks.values())
    print()
    print(f"=> CANARY B {'PASS' if passed else 'FAIL'}")
    return passed


def canary_c_n_eop_bal():
    hr('CANARY C — "Trace N_EOP_BAL" (W87 short-circuit unchanged)')
    r = run_query("Trace N_EOP_BAL")
    d = r["done"] or {}

    print(f"badge:    {d.get('badge')!r}")
    print(f"type:     {d.get('type')!r}")
    print(f"status:   {d.get('status')!r}")
    decline = d.get("decline") or {}
    print(f"decline:  name={decline.get('name')!r} reason={(decline.get('reason') or '')[:80]!r}")
    print(f"summary:  {summarize_done(d)}")

    # W87 short-circuits VARIABLE_TRACE on unrecognized terms with
    # status="unverified" + type="unrecognized_term" + an
    # UNRECOGNIZED_TERM warning. If the column happens to be indexed
    # the trace runs normally (status="answered"). Either shape is
    # the canonical W87 behaviour and counts as unchanged — only a
    # hard error or a missing type field is a real regression.
    status = d.get("status")
    type_ = d.get("type")
    w87_short_circuit = (
        status == "unverified" and type_ == "unrecognized_term"
    )
    normal_trace = status in ("answered", "declined")
    passed = (w87_short_circuit or normal_trace) and type_ is not None
    print()
    print(f"=> CANARY C {'PASS' if passed else 'FAIL'}")
    return passed


def canary_d_w80c():
    hr('CANARY D — W80c significant-investment regression (recall 5/5)')
    r = run_query(
        "summarize the workflow for non-regulated entity investment processing"
    )
    d = r["done"] or {}

    meta_event = None
    for ev_name, payload in r["events"]:
        if ev_name == "meta":
            meta_event = payload
            break
    fns = (meta_event or {}).get("functions_analyzed") or []
    fns_upper = {f.upper() for f in fns}
    matched = fns_upper & W80_SIGNIFICANT_INVESTMENT_PIPELINE

    graph_rerank = (meta_event or {}).get("graph_rerank") or {}
    rerank_status = graph_rerank.get("status", "missing")
    rank_change_count = graph_rerank.get("rank_change_count", 0)

    print(f"matched:            {len(matched)}/5")
    print(f"matched_fns:        {sorted(matched)}")
    print(f"rerank_status:      {rerank_status}")
    print(f"rank_change_count:  {rank_change_count}")

    checks = {
        "recall_5_of_5": len(matched) >= 5,
        "rerank_status_ok": rerank_status == "ok",
        "rerank_moved": rank_change_count > 0,
    }
    print()
    print("checks:")
    for k, v in checks.items():
        print(f"  [{'PASS' if v else 'FAIL'}] {k}")
    passed = all(checks.values())
    print()
    print(f"=> CANARY D {'PASS' if passed else 'FAIL'}")
    return passed


def main():
    results = {
        "A — FN_LOAD": canary_a_fn_load(),
        "B — CAP973":  canary_b_cap973(),
        "C — N_EOP_BAL": canary_c_n_eop_bal(),
        "D — W80c 5/5": canary_d_w80c(),
    }
    hr("SUMMARY")
    for name, passed in results.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    n_pass = sum(1 for v in results.values() if v)
    print(f"\nTotal: {n_pass}/{len(results)}")
    sys.exit(0 if n_pass == len(results) else 1)


if __name__ == "__main__":
    main()
