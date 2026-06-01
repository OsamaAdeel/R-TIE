"""W147 E3 live validation — runs the false-positive query against the
running backend and reports the done-payload signals that matter."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests", "integration"))

from test_live_stream import run_query, summarize_done  # noqa: E402

QUERY = "What feeds data into FN_G_TEST_CSTM?"

res = run_query(QUERY)
done = res["done"] or {}
warnings = done.get("warnings") or []
fa = done.get("functions_analyzed") or []

print("QUERY:", QUERY)
print("SUMMARY:", summarize_done(done))
print()
print("badge            :", done.get("badge"))
print("validated        :", done.get("validated"))
print("confidence       :", done.get("confidence"))
print("type             :", done.get("type"))
print("functions_analyzed:", fa)
print("PARTIAL_SOURCE_INDEXED present:", any("PARTIAL_SOURCE_INDEXED" in w for w in warnings))
print("warnings         :", warnings)
print()
md = res["markdown"]
print("FN_G_TEST_CSTM named in body:", "FN_G_TEST_CSTM" in md.upper())
print("markdown length  :", len(md))
print("--- first 800 chars of body ---")
print(md[:800])
