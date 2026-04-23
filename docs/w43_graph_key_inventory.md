# W43 Diagnostic: Graph Key Inventory

**Branch:** diagnostic/w43-graph-fallback  
**Date:** 2026-04-22  
**Purpose:** Document the Redis key layout and compare it to what `resolve_variable_nodes` / `resolve_function_nodes` expect.

---

## Instructions for Collecting Live Data

Run the following commands against the Redis instance (adjust container name as needed):

```bash
# List all graph keys (first 50)
docker exec <redis_container_name> redis-cli KEYS 'graph:*' | head -50

# Check key type
docker exec <redis_container_name> redis-cli TYPE 'graph:OFSMDM:FN_LOAD_OPS_RISK_DATA'

# Inspect raw bytes (first 200 chars — avoid dumping full payload)
docker exec <redis_container_name> redis-cli GETRANGE 'graph:OFSMDM:FN_LOAD_OPS_RISK_DATA' 0 200

# Check parse metadata (JSON, human-readable)
docker exec <redis_container_name> redis-cli GET 'graph:meta:OFSMDM:FN_LOAD_OPS_RISK_DATA'

# Check column index size
docker exec <redis_container_name> redis-cli STRLEN 'graph:index:OFSMDM'

# Check whether function name appears as a key in the column index
# (would require deserializing the MessagePack blob — not possible from redis-cli directly)

# Check full graph key
docker exec <redis_container_name> redis-cli TYPE 'graph:full:OFSMDM'
docker exec <redis_container_name> redis-cli STRLEN 'graph:full:OFSMDM'
```

*This section should be filled in by running the commands above after the backend restart.*

---

## Redis Key Schema (from src/parsing/store.py:16-25)

| Pattern Name | Key Template | Contains |
|---|---|---|
| `function_graph` | `graph:{schema}:{function_name}` | MessagePack-encoded dict with `nodes`, `edges`, `function`, `execution_condition` |
| `full_graph` | `graph:full:{schema}` | MessagePack-encoded merged graph with all cross-function edges |
| `column_index` | `graph:index:{schema}` | MessagePack dict: `{COLUMN_NAME_UPPER: [node_id, ...]}` |
| `alias_map` | `graph:aliases:{schema}` | MessagePack dict: `{alias_upper: [canonical, ...]}` |
| `raw_source` | `graph:source:{schema}:{function_name}` | MessagePack list of raw SQL line strings |
| `parse_metadata` | `graph:meta:{schema}:{function_name}` | MessagePack dict: `{parsed_at, schema, function_name, node_count, edge_count}` |
| `batch_hierarchy` | `hierarchy:{batch_name}` | JSON-encoded manifest dict |
| `batch_set` | `hierarchy:batches` | Redis Set of known batch names |

**Serialization note:** All `graph:*` keys use MessagePack (`msgpack.packb(..., use_bin_type=True)`). The `hierarchy:*` keys use JSON. Mixing these formats would cause silent deserialization failures — `from_msgpack` would catch the exception and return `None`.

---

## Key Structure Observations

### Three-segment keys (function graphs)
Pattern: `graph:{schema}:{function_name}`  
Example: `graph:OFSMDM:FN_LOAD_OPS_RISK_DATA`

These are the only keys `resolve_function_nodes` ever looks up. The function_name segment must match **exactly** what was stored by `store_function_graph`. The loader (`src/parsing/loader.py`) calls:
```python
store_function_graph(redis_client, schema, function_name, graph)
```
where `function_name` comes from the filename/SQL header. Casing and spelling must match what the user query supplies.

### Four-segment keys (source + metadata)
Pattern: `graph:source:{schema}:{function_name}`, `graph:meta:{schema}:{function_name}`  
These are not used by the graph pipeline but are used by `MetadataInterpreter` for raw-source fallback.

### Structural keys (index, full graph, aliases)
Pattern: `graph:index:{schema}`, `graph:full:{schema}`, `graph:aliases:{schema}`  
These contain aggregated data across all functions. If missing, `resolve_variable_nodes` cannot operate.

---

## What resolve_function_nodes Expects vs. What Is Stored

`resolve_function_nodes` calls:
```python
key = f"graph:{schema}:{function_name}"
data = redis_client.get(key)
```

For the query "How does FN_LOAD_OPS_RISK_DATA work?", the value passed as `function_name` is **`state["object_name"]`** — which contains the enriched semantic search string, not the bare function name.

Actual key attempted: `graph:OFSMDM:How does FN_LOAD_OPS_RISK_DATA work? [intent] [search terms]`  
Key that exists: `graph:OFSMDM:FN_LOAD_OPS_RISK_DATA`

These never match. The Redis `GET` returns `None` every time.

---

## Comparison: Pre-check vs. Graph Pipeline Lookup

| Step | Code Location | Input | Redis Key Attempted | Outcome |
|------|------|------|------|------|
| Pre-check (W37) | `orchestrator.py:464` | `func_upper = "FN_LOAD_OPS_RISK_DATA"` | `graph:OFSMDM:FN_LOAD_OPS_RISK_DATA` | HIT → function exists |
| Graph pipeline | `main.py:768-770` | `g_search_term = state["object_name"]` (enriched string) | `graph:OFSMDM:How does FN_LOAD_OPS_RISK_DATA work? ...` | MISS → None → 0 nodes |

The pre-check correctly finds the function using the raw regex-extracted identifier. The graph pipeline uses a different code path that inadvertently passes the enriched query string instead.

---

## Column Index Structure (Expected)

The column index at `graph:index:OFSMDM` is a dict of the form:
```json
{
  "N_EOP_BAL": ["FN_LOAD_OPS_RISK_DATA_N1", "FN_POPULATE_OPS_N3", ...],
  "N_ANNUAL_GROSS_INCOME": ["FN_OTHER_FUNC_N5"],
  ...
}
```

Function names (like `"FN_LOAD_OPS_RISK_DATA"`) are **never** keys in this index. Only column/variable names appear as keys. This means that even when `resolve_variable_nodes` is called with `target_variable = "FN_LOAD_OPS_RISK_DATA"` (Branch A in the retrieval path), it finds no match in the column index and returns `[]`.

---

## Live Data Collection Results

*Collected 2026-04-22 via Python Redis client (same connection as backend).*

### All graph:* keys (total: 180)

**OFSERM function graphs (20 keys):** ABL_DEF_PENSION_FUND_ASSET_NET_DTL, BASEL_III_*, CAP_CONSL_*, DERIVATIVES_*, ENTITY_*, EXCHANGE_RATE_DATA_POPULATION, FSI_PARTY_*, JURISDICTION_*, PARTY_*, PROD_TYPE_*, RUN_*, T2T_FSI_CAP_*, THIRD_PARTY_*

**OFSMDM function graphs (38 keys):** FN_LOAD_OPS_RISK_DATA, FN_UPDATE_RATING_CODE, POPULATE_GL_FROMGLBAL, POPULATE_PP_FROMGL, POPULATE_PP_FROMGL_AMC, POPULATE_STDACC_FROMGL, TLX_*, UPDATE_RUN_PARAMETERS, TEST_INACTIVE_FN, TEST_MANIFEST_FN, TEST_SIMPLE, plus duplicates with spaces (see §Notable below)

**Structural keys:**
- `graph:aliases:OFSERM`, `graph:aliases:OFSMDM`
- `graph:full:OFSERM`, `graph:full:OFSMDM`
- `graph:index:OFSERM`, `graph:index:OFSMDM`
- `graph:meta:OFSERM:*` (20 keys), `graph:meta:OFSMDM:*` (38 keys)
- `graph:source:*` keys not scanned (exist per store_raw_source)

### Type of graph:OFSMDM:FN_LOAD_OPS_RISK_DATA
`string` (MessagePack binary). Confirms correct storage format. Key exists and is readable via `get_function_graph` (pre-check succeeds; deserialization works).

### Column Index: graph:index:OFSMDM

**Actual content:**
```python
{'OFSMDM': ['TEST_SIMPLE:TEST_SIMPLE_N1']}
```

**Expected content:** `{COLUMN_NAME_UPPER: [node_id, ...], ...}` — hundreds of entries across all 59 nodes.

**Root cause:** The column index is overwritten by each `load_all_functions` call (one per module). TEST_MODULE was loaded last and had only one function (TEST_SIMPLE), whose column index had just one entry with schema name as key. This single-entry dict replaced the full OFSDMINFO_ABL_DATA_PREPARATION column index.

### Full Graph: graph:full:OFSMDM

**Actual:** 1 node, 0 edges (from TEST_MODULE/TEST_SIMPLE only).  
**Expected:** 59 nodes, 18 edges (all OFSDMINFO_ABL_DATA_PREPARATION functions merged).

Same overwrite problem as column index — last module loaded wins.

### Notable: Duplicate OFSMDM Keys With Spaces

Several OFSMDM keys have function names with spaces instead of underscores:
```
graph:OFSMDM:BASEL III CAPITAL CONSOLIDATION APPROACH TYPE RECLASSIFICATION FOR AN ENTITY
graph:OFSMDM:CAP CONSL EFFECTIVE SHAREHOLDING PERCENT FOR AN ENTITY
graph:OFSMDM:JURISDICTION CODE ASSIGNMENT
graph:OFSMDM:RUN PRODUCT CODE ASSIGNMENT
graph:OFSMDM:THIRD PARTY MINORITY HOLDING INDICATOR ASSIGNMENT UNDER CAPITAL  CONSOLIDATION
```

These appear alongside underscore versions of the same functions. Likely stored when ABL_CAR_CSTM_V4 module was first parsed (before function name normalization was applied). Neither the pre-check nor the graph pipeline would ever find these keys (both use underscore forms).

### Differences: What Is Stored vs. What resolve_variable_nodes Expects

| What code expects | What is stored |
|---|---|
| `graph:index:OFSMDM` → `{COLUMN_NAME: [node_ids]}` | `{OFSMDM: ['TEST_SIMPLE:TEST_SIMPLE_N1']}` — wrong structure |
| `graph:full:OFSMDM` → merged graph of all 59 nodes | 1 node, 0 edges — only TEST_MODULE data |
| `graph:OFSMDM:FN_LOAD_OPS_RISK_DATA` → function graph | Present and correct |
| Query-time key: `graph:OFSMDM:<bare function name>` | Key exists. But pipeline sends enriched string → misses |
