import sys
sys.path.insert(0, ".")

from src.parsing.parser import parse_function, clean_source_lines
from src.parsing.builder import build_function_graph

with open("db/modules/OFSDMINFO_ABL_DATA_PREPARATION/functions/FN_LOAD_OPS_RISK_DATA.sql") as f:
    lines = f.readlines()

print("=== TEST 1: clean_source_lines ===")
cleaned, ranges = clean_source_lines(lines)
for i, (raw, clean) in enumerate(zip(lines, cleaned)):
    if 'CFI' in raw:
        print(f"Line {i+1} RAW:     '{raw.rstrip()}'")
        print(f"Line {i+1} CLEANED: '{clean.rstrip()}'")
        print(f"CFI in cleaned: {'CFI' in clean}")

print("\n=== TEST 2: raw_blocks from parser ===")
result = parse_function(lines, "FN_LOAD_OPS_RISK_DATA")
for rb in result["raw_blocks"]:
    if rb["block_type"] == "UPDATE":
        has_cleaned = "cleaned_lines" in rb
        print(f"UPDATE lines {rb['line_start']}-{rb['line_end']}, has cleaned_lines: {has_cleaned}")
        if has_cleaned:
            cfi_found = any('CFI' in cl for cl in rb["cleaned_lines"])
            print(f"  CFI in cleaned_lines: {cfi_found}")

print("\n=== TEST 3: builder output ===")
graph = build_function_graph(lines, "FN_LOAD_OPS_RISK_DATA",
                             "FN_LOAD_OPS_RISK_DATA.sql", "OFSMDM")
for node in graph["nodes"]:
    for calc in node.get("calculation", []):
        if isinstance(calc, dict):
            expr = calc.get("expression", "")
            if "CFI" in expr:
                print(f"CFI in node {node['id']} expression!")

print("\n=== TEST 4: UPDATE node count ===")
update_nodes = [n for n in graph["nodes"] if n["type"] == "UPDATE"]
print(f"Total UPDATE nodes: {len(update_nodes)}")
for un in update_nodes:
    print(f"  {un['id']}: lines {un['line_start']}-{un['line_end']}")

print("\n=== TEST 5: column index ===")
agi = graph["column_index"].get("N_ANNUAL_GROSS_INCOME", [])
print(f"N_ANNUAL_GROSS_INCOME nodes: {agi}")

print("\n=== TEST 6: TLX_OPS_ADJ_MISDATE graph ===")
try:
    with open("db/modules/OFSDMINFO_ABL_DATA_PREPARATION/functions/TLX_OPS_ADJ_MISDATE.sql") as f:
        tlx_lines = f.readlines()
    tlx_graph = build_function_graph(tlx_lines, "TLX_OPS_ADJ_MISDATE",
                                     "TLX_OPS_ADJ_MISDATE.sql", "OFSMDM")
    print(f"TLX nodes: {len(tlx_graph['nodes'])}")
    for n in tlx_graph["nodes"]:
        print(f"  {n['id']}: {n['type']} lines {n['line_start']}-{n['line_end']}")
    tlx_agi = tlx_graph["column_index"].get("N_ANNUAL_GROSS_INCOME", [])
    print(f"N_ANNUAL_GROSS_INCOME in TLX: {tlx_agi}")
except Exception as e:
    print(f"TLX FAILED: {e}")

print("\n=== TEST 7: query engine node resolution ===")
import redis
from src.parsing.query_engine import (
    resolve_query_to_nodes,
    fetch_nodes_by_ids,
    fetch_relevant_edges,
    determine_execution_order,
    assemble_llm_payload,
)

r = redis.Redis(host="localhost", port=6379)

node_ids = resolve_query_to_nodes(
    query_type="variable",
    target_variable="N_ANNUAL_GROSS_INCOME",
    function_name="",
    table_name="",
    schema="OFSMDM",
    redis_client=r,
)

print(f"Total nodes resolved: {len(node_ids)}")
for nid in node_ids:
    print(f"  {nid}")

fn_names = set()
for nid in node_ids:
    fn = nid.split(":")[0] if ":" in nid else nid.rsplit("_N", 1)[0]
    fn_names.add(fn)

print(f"\nFunctions involved: {fn_names}")

assert len(node_ids) <= 10, f"Too many nodes: {len(node_ids)} — should be 6-8"
assert "TLX_LOAD_DELETE_OFSMDM" not in str(node_ids), \
    "TLX_LOAD_DELETE_OFSMDM should not be in results"
assert "TLX_LOB_MAPPING" not in str(node_ids), \
    "TLX_LOB_MAPPING should not be in results"

print("PASS: only relevant nodes returned")

# Test payload assembly
fetched = fetch_nodes_by_ids(node_ids, "OFSMDM", r)
edges = fetch_relevant_edges(node_ids, "OFSMDM", r)
order = determine_execution_order(fetched, edges)
payload = assemble_llm_payload(
    nodes=fetched,
    edges=edges,
    target_variable="N_ANNUAL_GROSS_INCOME",
    user_query="How is N_ANNUAL_GROSS_INCOME calculated?",
    execution_order=order,
)
print(f"\nPayload ({len(payload)} chars):")
print(payload[:2000])

assert "TLX_LOAD_DELETE_OFSMDM" not in payload, "Payload contains irrelevant TLX_LOAD_DELETE"
assert "TLX_LOB_MAPPING" not in payload, "Payload contains irrelevant TLX_LOB_MAPPING"
print("\nPASS: payload contains only relevant functions")

print("\n=== DONE ===")