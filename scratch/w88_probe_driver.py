"""
W88 diagnostic — probe driver for named regulatory computations.

Phase 1: direct Oracle probes via schema_tools / SqlGuardian (read-only).
Phase 2: /v1/stream NL probes — capture the `done` SSE event.

Saves output to scratch/w88_probes/<id>.json.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx

SCRATCH_DIR = Path(__file__).resolve().parent / "w88_probes"
SCRATCH_DIR.mkdir(exist_ok=True)


# ---- Phase 1: direct Oracle probes ----------------------------------------
async def oracle_probes():
    """Probe local Oracle for reachability of canonical fact tables."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    from dotenv import load_dotenv
    load_dotenv(".env.dev")

    from src.tools.schema_tools import SchemaTools

    st = SchemaTools(
        host=os.getenv("ORACLE_HOST"),
        port=int(os.getenv("ORACLE_PORT", "1521")),
        sid=os.getenv("ORACLE_SID"),
        user=os.getenv("ORACLE_USER"),
        password=os.getenv("ORACLE_PASSWORD"),
    )
    await st.initialize()

    probes = [
        # name, sql, notes
        ("ofserm_fct_ops_risk_data_count",
         "SELECT COUNT(*) FROM OFSERM.FCT_OPS_RISK_DATA",
         "Total rows in OFSERM.FCT_OPS_RISK_DATA"),

        ("ofserm_fct_ops_risk_data_methods",
         "SELECT N_BASEL_METHOD_SKEY, COUNT(*) C FROM OFSERM.FCT_OPS_RISK_DATA "
         "GROUP BY N_BASEL_METHOD_SKEY ORDER BY C DESC",
         "Methodology distribution on FCT_OPS_RISK_DATA"),

        ("dim_basel_methodology_inventory",
         "SELECT N_BASEL_METHOD_SKEY, V_BASEL_METHOD_CODE, V_BASEL_METHOD_DESC, "
         "V_BASEL_RISK_TYPE_ID, V_BASEL_APPROACH_TYPE_ID "
         "FROM OFSERM.DIM_BASEL_METHODOLOGY "
         "WHERE F_LATEST_RECORD_INDICATOR = 'Y' ORDER BY N_BASEL_METHOD_SKEY",
         "Method SKEY → name mapping"),

        ("ofserm_fct_ops_risk_data_bia_sample",
         "SELECT N_RUN_SKEY, N_MIS_DATE_SKEY, N_BASEL_METHOD_SKEY, "
         "N_CAPITAL_CHARGE, N_RWA_AMT, N_ANNUAL_GROSS_INCOME "
         "FROM OFSERM.FCT_OPS_RISK_DATA WHERE N_BASEL_METHOD_SKEY = 115 "
         "FETCH FIRST 5 ROWS ONLY",
         "Sample BIA rows"),

        ("ofserm_fct_ops_risk_data_bia_totals",
         "SELECT SUM(N_CAPITAL_CHARGE) CAP_CHG, SUM(N_RWA_AMT) RWA "
         "FROM OFSERM.FCT_OPS_RISK_DATA WHERE N_BASEL_METHOD_SKEY = 115",
         "BIA capital charge + RWA totals"),

        ("ofserm_fct_ops_risk_summary_count",
         "SELECT COUNT(*) FROM OFSERM.FCT_OPS_RISK_SUMMARY", ""),

        ("ofserm_fct_market_risk_summary_count",
         "SELECT COUNT(*) FROM OFSERM.FCT_MARKET_RISK_SUMMARY", ""),

        ("ofserm_fct_market_risk_summary_methods",
         "SELECT N_BASEL_METHOD_SKEY, COUNT(*) C FROM OFSERM.FCT_MARKET_RISK_SUMMARY "
         "GROUP BY N_BASEL_METHOD_SKEY ORDER BY C DESC", ""),

        ("ofserm_fct_mr_var_data_count",
         "SELECT COUNT(*) FROM OFSERM.FCT_MR_VAR_DATA", ""),

        ("ofserm_fct_standard_acct_head_count",
         "SELECT COUNT(*) FROM OFSERM.FCT_STANDARD_ACCT_HEAD", ""),

        ("ofserm_fct_std_acct_head_by_capcode",
         "SELECT DSA.V_STD_ACCT_HEAD_ID, COUNT(*) C, "
         "SUM(FSA.N_STD_ACCT_HEAD_AMT) AMT "
         "FROM OFSERM.FCT_STANDARD_ACCT_HEAD FSA "
         "JOIN OFSERM.DIM_STANDARD_ACCT_HEAD DSA "
         "ON FSA.N_STD_ACCT_HEAD_SKEY = DSA.N_STD_ACCT_HEAD_SKEY "
         "WHERE DSA.V_STD_ACCT_HEAD_ID IN "
         "('CAP090','CAP170','CAP192','CAP210','CAP214','CAP838','CAP841','CAP935','CAP959','CAP960','CAP1923') "
         "GROUP BY DSA.V_STD_ACCT_HEAD_ID ORDER BY DSA.V_STD_ACCT_HEAD_ID",
         "Capital ratio + RWA targets"),

        ("ofserm_fct_non_sec_exposures_exists",
         "SELECT COUNT(*) FROM OFSERM.FCT_NON_SEC_EXPOSURES "
         "FETCH FIRST 1 ROWS ONLY", "Credit risk RWA source"),

        ("ofserm_fct_sec_exposures_exists",
         "SELECT COUNT(*) FROM OFSERM.FCT_SEC_EXPOSURES "
         "FETCH FIRST 1 ROWS ONLY", ""),

        ("ofsmdm_abl_ops_risk_data_count",
         "SELECT COUNT(*) FROM OFSMDM.ABL_OPS_RISK_DATA", ""),

        ("user_tables_lcr_nsfr_leverage",
         "SELECT OWNER, TABLE_NAME FROM ALL_TABLES "
         "WHERE OWNER IN ('OFSERM','OFSMDM') AND "
         "(TABLE_NAME LIKE '%LCR%' OR TABLE_NAME LIKE '%NSFR%' "
         "OR TABLE_NAME LIKE '%LEVERAGE%' OR TABLE_NAME LIKE '%LIQUIDITY%') "
         "ORDER BY OWNER, TABLE_NAME", ""),

        ("user_tables_fct_credit",
         "SELECT OWNER, TABLE_NAME FROM ALL_TABLES "
         "WHERE OWNER IN ('OFSERM','OFSMDM') AND "
         "(TABLE_NAME LIKE 'FCT_%CR%' OR TABLE_NAME LIKE 'FCT_NON_SEC%' "
         "OR TABLE_NAME LIKE 'FCT_SEC_%' OR TABLE_NAME LIKE '%CREDIT%') "
         "ORDER BY OWNER, TABLE_NAME", ""),
    ]

    results = []
    for name, sql, notes in probes:
        out = {"name": name, "sql": sql, "notes": notes}
        try:
            rows = await st.execute_raw(sql)
            out["rows"] = [list(r) for r in rows[:50]]
            out["row_count"] = len(rows)
            out["status"] = "OK"
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            out["status"] = "FAIL"
        results.append(out)
        print(f"[{out['status']}] {name}: {out.get('row_count','-')} rows "
              f"{'/ ' + out.get('error','') if 'error' in out else ''}")

    (SCRATCH_DIR / "oracle_probes.json").write_text(
        json.dumps(results, indent=2, default=str))
    await st.close()
    return results


# ---- Phase 2: /v1/stream classifier probes --------------------------------
PROBES = [
    ("c01_bia",
     "What is the operational risk capital charge under Basic Indicator "
     "Approach on 2026-03-31?"),
    ("c02_sa_op",
     "What is the operational risk capital charge under the Standardised "
     "Approach (TSA) on 2026-03-31?"),
    ("c03_asa",
     "What is the operational risk capital charge under the Alternative "
     "Standardised Approach (ASA) on 2026-03-31?"),
    ("c04_ama",
     "What is the operational risk capital charge under the Advanced "
     "Measurement Approach (AMA) on 2026-03-31?"),
    ("c05_credit_sa",
     "What is the credit risk RWA under the Standardised Approach on 2026-03-31?"),
    ("c06_credit_irb_f",
     "What is the credit risk RWA under IRB Foundation on 2026-03-31?"),
    ("c07_credit_irb_a",
     "What is the credit risk RWA under IRB Advanced on 2026-03-31?"),
    ("c08_market_std",
     "What is the market risk RWA under the Standardised Approach on 2026-03-31?"),
    ("c09_market_im",
     "What is the market risk RWA under Internal Models (VaR-based) on 2026-03-31?"),
    ("c10_cet1",
     "What is the CET1 ratio for run 870 on 2026-03-31?"),
    ("c11_tier1",
     "What is the Tier 1 capital ratio for run 870 on 2026-03-31?"),
    ("c12_total_cap",
     "What is the Total Capital ratio (CAR) for run 870 on 2026-03-31?"),
    ("c13_leverage",
     "What is the Leverage Ratio on 2026-03-31?"),
    ("c14_lcr",
     "What is the Liquidity Coverage Ratio (LCR) on 2026-03-31?"),
    ("c15_nsfr",
     "What is the Net Stable Funding Ratio (NSFR) on 2026-03-31?"),
]


async def stream_one(client, probe_id, query):
    """Send one NL query, return the parsed `done` event payload."""
    payload = {
        "query": query,
        "session_id": f"w88-{probe_id}",
        "engineer_id": "w88-diag",
    }
    done = None
    stages = []
    started = time.monotonic()
    try:
        async with client.stream("POST", "http://localhost:8000/v1/stream",
                                 json=payload, timeout=120.0) as resp:
            event = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data = line[5:].strip()
                    if event == "stage":
                        stages.append(data)
                    elif event == "done":
                        try:
                            done = json.loads(data)
                        except Exception:
                            done = {"raw": data}
                        break
    except Exception as e:
        return {"probe_id": probe_id, "query": query,
                "error": f"{type(e).__name__}: {e}",
                "stages": stages,
                "elapsed_s": round(time.monotonic() - started, 2)}
    return {"probe_id": probe_id, "query": query, "stages": stages,
            "done": done, "elapsed_s": round(time.monotonic() - started, 2)}


async def stream_probes():
    async with httpx.AsyncClient() as client:
        results = []
        for pid, q in PROBES:
            out_path = SCRATCH_DIR / f"{pid}.json"
            if out_path.exists():
                results.append(json.loads(out_path.read_text()))
                print(f"[CACHED] {pid}")
                continue
            r = await stream_one(client, pid, q)
            out_path.write_text(json.dumps(r, indent=2, default=str))
            results.append(r)
            done = r.get("done") or {}
            qtype = done.get("type", "?")
            badge = done.get("badge", "?")
            schemas = done.get("schema_searched", [])
            sql_short = (done.get("sql") or "")[:80]
            print(f"[{qtype} | {badge} | {schemas}] {pid}: {sql_short}")
        return results


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("oracle", "all"):
        print("=== Phase 1: Oracle probes ===")
        await oracle_probes()
    if mode in ("stream", "all"):
        print("\n=== Phase 2: /v1/stream NL probes ===")
        await stream_probes()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
