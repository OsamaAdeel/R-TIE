# W43 Diagnostic: Findings Report

**Branch:** diagnostic/w43-graph-fallback  
**Date:** 2026-04-22  
**Investigator:** Diagnostic instrumentation + static code analysis  

---

## Section 1 — Summary (Plain English)

The bug is **systematic across all FUNCTION_LOGIC / COLUMN_LOGIC queries**, not specific to `FN_LOAD_OPS_RISK_DATA`.

The Phase 1 graph pipeline (`main.py:759-806`) resolves the identifier to look up in Redis by reading `state["object_name"]`. That field is set by the orchestrator (`orchestrator.py:384`) to the **enriched semantic search query** — a concatenation of the raw user query, the LLM's intent summary, and the extracted search terms. This is the correct value for the semantic vector search (used at `main.py:734`), but it is a multi-word sentence, not a bare function name.

When the graph pipeline calls `resolve_function_nodes(function_name=<enriched_string>, ...)`, the Redis key it constructs (`graph:OFSMDM:How does FN_LOAD_OPS_RISK_DATA work? ...`) does not exist. `get_function_graph` returns `None`. `resolve_function_nodes` returns `[]`. Zero nodes → fallback.

The alternative branch (when the LLM happens to populate `target_variable`) routes to `resolve_variable_nodes`, which looks up the function name in the **column index** — a structure that maps column names to node IDs, never function names. This also returns 0 nodes.

Neither branch can succeed for a FUNCTION_LOGIC query with the current code. The graph pipeline for this query type has never worked.

**Matching hypothesis:** H5 (never worked) is confirmed as the primary characterization. H6 (query-name extraction broken) identifies the specific mechanism. H7 (entry-point confusion: two code paths) is a secondary contributing factor.

---

## Section 2 — Per-Query Findings Table

*Live benchmark run: 2026-04-22, after W43_DIAG instrumentation deployed.*

| Query | Corr. ID (short) | Graph hit? | Nodes | Fallback? | Branch | Badge | Total time |
|-------|------|-----------|-------|-----------|--------|-------|------------|
| How does FN_LOAD_OPS_RISK_DATA work? | 53a47915 | No | 0 | Yes | function, enriched-string miss | VERIFIED | 139.2s |
| How does POPULATE_PP_FROMGL work? | 5d2ec6de | No | 0 | Yes | function, enriched-string miss | VERIFIED | 110.8s |
| How does TLX_OPS_ADJ_MISDATE work? | 9c7a2c17 | No | 0 | Yes | variable, fn-name not in col-index | VERIFIED | 131.0s |
| How does ABL_Def_Pension_Fund_Asset_Net_DTL work? | 93c3f7df | No | 0 | Yes | function, enriched-string miss | VERIFIED | 82.8s |
| How does FN_DEFINITELY_NOT_A_REAL_FUNCTION work? | 831a0d99 | N/A | N/A | N/A | pre-check short-circuit | DECLINED | 7.6s |
| How is N_ANNUAL_GROSS_INCOME calculated? | 1288122e | No | 0 | Yes† | variable, col-index has 1 entry | VERIFIED | 24.1s |

†Q6 routes to `_variable_tracer.stream_chain` (not raw-source fallback) because `query_type=VARIABLE_TRACE`, so its 24s time is faster than the raw-source path despite also returning 0 graph nodes.

**Key W43_DIAG log evidence for Q1 (representative):**
```
stage=graph_pipeline_entry  query_type='COLUMN_LOGIC'  target_variable=None
  object_name_len=204  g_query_type='function'
  g_search_term='How does FN_LOAD_OPS_RISK_DATA work? Explain how the PL/SQL function ...'
  
stage=resolve_function_nodes_entry
  function_name='How does FN_LOAD_OPS_RISK_DATA work? Explain...'
  redis_key_attempted='graph:OFSMDM:How does FN_LOAD_OPS_RISK_DATA work? Explain...'
  function_name_is_multiword=True

stage=resolve_function_nodes_result  cache_hit=false  node_count=0

stage=fallback_selected  reason=no_nodes_returned
```

**Key W43_DIAG log evidence for Q3 (Branch A — variable path):**
```
stage=graph_pipeline_entry  query_type='COLUMN_LOGIC'
  target_variable='TLX_OPS_ADJ_MISDATE'  g_query_type='variable'
  g_search_term='TLX_OPS_ADJ_MISDATE'

stage=resolve_variable_nodes_index_lookup
  col_index_size=1  aliases=['TLX_OPS_ADJ_MISDATE']
  alias_hits={'TLX_OPS_ADJ_MISDATE': []}      ← col-index has 1 key, function name absent

stage=resolve_variable_nodes_result  direct_nodes=0  after_edge_walk=0
```

**Q6 reveals second structural problem:** `col_index_size=1` for `N_ANNUAL_GROSS_INCOME` (a genuine column). The Redis column index `graph:index:OFSMDM` contains only `{'OFSMDM': ['TEST_SIMPLE:TEST_SIMPLE_N1']}` — it has been overwritten by the last module loaded (TEST_MODULE). See Section 7.2.

---

## Section 3 — Hypothesis Evaluation

### H1: Function name case mismatch

**Evidence for:** None. The key `graph:OFSMDM:FN_LOAD_OPS_RISK_DATA` is confirmed to exist. The function name in the query is already uppercase.

**Evidence against:** `function_exists_in_graph` (pre-check) uses `.upper()` and finds the key successfully. The graph pipeline passes the same name (when it happens to have it), so case is not the issue. The pre-check passes, which means the stored key case matches `FN_LOAD_OPS_RISK_DATA` exactly.

**Verdict:** REJECTED. Case is not the problem.

---

### H2: Graph key format mismatch (W38 schema prefix)

**Evidence for:** W38 introduced schema-prefixed keys (`graph:OFSMDM:*`). The `store.py` key template is `graph:{schema}:{function_name}` — three segments. If any lookup path still uses an old two-segment format (e.g., `graph:FN_LOAD_OPS_RISK_DATA`), it would miss.

**Evidence against:** All lookups in `query_engine.py` and `store.py` use the three-segment format consistently. `find_similar_function_names` correctly filters for three-segment keys (`len(parts) == 3`). No two-segment lookup path was found in the code.

**Verdict:** REJECTED (pending live Redis inspection confirming no two-segment keys). The key format is consistent throughout the code.

---

### H3: resolve_variable_nodes doesn't match W39 hierarchy-annotated nodes

**Evidence for:** W39 added `hierarchy` metadata to nodes. `resolve_variable_nodes` has an `_is_inactive` filter that reads `hierarchy.get("active")`. If `active` is missing or mis-set, active nodes could be filtered out.

**Evidence against:** The `_is_inactive_node` function (`query_engine.py:111`) returns `False` (treats as active) when the `hierarchy` block is absent — so W39 nodes without a manifest would not be filtered. Nodes with `active=True` (or missing) pass through. The bug occurs before any filtering — 0 nodes are returned from the index lookup, not filtered from a larger set.

**Verdict:** REJECTED as primary cause. Could be a secondary issue for specific manifested-inactive functions, but is not the cause of the 0-node result for `FN_LOAD_OPS_RISK_DATA`.

---

### H4: Node-level cache invalidation incomplete (W30)

**Evidence for:** If `store_function_graph` stores MessagePack but an older entry was stored as JSON, `from_msgpack` would raise an exception caught by `get_function_graph`, which returns `None`. `resolve_function_nodes` would then return `[]`.

**Evidence against:** The Redis key was confirmed to exist (diagnostic context). The pre-check (`function_exists_in_graph`) calls the same `get_function_graph` and returns `True` — meaning the data deserializes successfully. If deserialization were broken, the pre-check would also fail and the function would be DECLINED, not answered via fallback.

**Verdict:** REJECTED. The pre-check's success proves the deserialization works for `graph:OFSMDM:FN_LOAD_OPS_RISK_DATA`. The issue is that the graph pipeline uses a different (wrong) key.

---

### H5: Never worked — graph pipeline always fell through for FUNCTION_LOGIC

**Evidence for:**
1. `state["object_name"]` has always been the enriched search string (see `orchestrator.py:384`). There is no git history of it ever being the bare function name.
2. The column index has never contained function names as keys — only column names.
3. Both resolution paths (function branch via enriched string, variable branch via function-name-in-column-index) have always returned 0 nodes for FUNCTION_LOGIC queries.
4. W33/W37/W39/W41 validations all used queries that were answered by the fallback; none verified the graph pipeline contributed.

**Evidence against:** If there were a period when `target_variable` was reliably set to the function name AND the graph pipeline somehow worked, some developers might have noticed faster response times on FUNCTION_LOGIC queries. No such observation is recorded.

**Verdict:** CONFIRMED. The graph pipeline for FUNCTION_LOGIC (COLUMN_LOGIC) queries has never functioned correctly.

---

### H6: Query-name extraction is broken

**Evidence for:**
- `orchestrator.py:384`: `state["object_name"] = enriched_query` (full sentence, not function name)
- `main.py:768-770`: Graph pipeline uses `obj_name = state["object_name"]` as `function_name`
- The Redis key attempted is `graph:OFSMDM:<full enriched query>` — this never exists
- The pre-check correctly extracts the bare function name via regex (`extract_function_candidates`) but this extraction result is not reused by the graph pipeline

**Evidence against:** The code design intent appears to be: `object_name` for semantic search, `target_variable` for graph lookup (variable queries). FUNCTION_LOGIC queries were never given a dedicated graph-lookup identifier.

**Verdict:** CONFIRMED as the primary mechanism. The enriched query string was never intended to be a Redis key; it's inadvertently being used as one.

---

### H7: Entry-point confusion — two code paths, wrong one used

**Evidence for:**
- There are two places that extract function names from a query: `extract_function_candidates()` (pre-check, correct) and `state["object_name"]` (graph pipeline, incorrect)
- The pre-check uses regex extraction on the raw query text and finds `FN_LOAD_OPS_RISK_DATA` correctly
- The graph pipeline uses a state field that is populated for a completely different purpose (semantic search enrichment)
- These two paths are inconsistent: one works, the other doesn't

**Evidence against:** The "two paths" are not really competing paths for the same function — they serve different purposes (W37 decline guard vs. graph retrieval). The confusion is a design gap, not a refactoring regression.

**Verdict:** CONFIRMED as a contributing factor. The fix will need to thread the correctly-extracted function name through to the graph pipeline, using the same identifier that the pre-check already has.

---

## Section 4 — Root Cause

**Primary root cause:**

`state["object_name"]` is set to the enriched semantic search query (a multi-word sentence) at `orchestrator.py:384`. The graph pipeline at `main.py:762` reads this same field expecting it to contain a bare function name for Redis lookup. Since the enriched string is never a valid Redis key, every FUNCTION_LOGIC query returns 0 nodes and falls through to raw-source fallback.

**File and line:**  `src/main.py:762` (reads `state["object_name"]` as function name)  
**Origin:**  `src/agents/orchestrator.py:384` (writes enriched string to `object_name`)

**Secondary contributing factor:**

Even when Branch A fires (LLM populates `target_variable` with the function name), the code routes to `resolve_variable_nodes`, which searches the column index. Function names are not column index keys. This path also returns 0 nodes.

**How it came to be:** This appears to be an original design gap. The `object_name` field was introduced as an enriched semantic-search term (for embedding). The graph pipeline was built later and reused the same field for a different purpose without a dedicated identifier for function-mode lookups. No PR introduced this as a regression — it was never wired correctly.

---

## Section 5 — Fix Candidates

### Candidate A: Extract function name in the graph pipeline using existing regex

At `main.py:758-773`, before the current identifier selection, call `extract_function_candidates(state["raw_query"])` (already imported from `orchestrator`). If it returns a candidate, use that as the function name for graph lookup.

- **Effort:** Low (2-4 lines)
- **Risk:** Low. The regex is already used and trusted by the pre-check. No new logic introduced.
- **What might break:** Edge case: if the user query names two functions, `extract_function_candidates` returns both. The current code only processes one. Would need to decide which candidate to try first (first match is usually correct).
- **Architectural fit:** Good. Reuses the existing, tested identifier extraction rather than adding another extraction path.

### Candidate B: Add a dedicated `function_name` field to ClassificationResult and state

Add `function_name: Optional[str]` to `ClassificationResult`. Have the LLM populate it for COLUMN_LOGIC queries. Persist it to state. Use `state.get("function_name")` in the graph pipeline instead of `state["object_name"]`.

- **Effort:** Medium (modify prompt, model, state, orchestrator, graph pipeline)
- **Risk:** Medium. Requires LLM to reliably extract function names. Adds a new state field that touches several layers.
- **What might break:** LLM might hallucinate function names or fail to populate for ambiguous queries. Prompt changes need testing. Adds LLM-extracted data on the critical path.
- **Architectural fit:** Cleanest long-term design — clear separation of concerns. But introduces LLM dependency for what is currently a regex operation.

### Candidate C: Prefer `target_variable` when it matches a known graph function name

In the graph pipeline, after extracting `target_var`, check whether `get_function_graph(redis_client, schema, target_var.upper())` returns non-None. If yes, switch to `g_query_type = "function"` with the uppercased name.

- **Effort:** Low (add one Redis check before the routing decision)
- **Risk:** Low-medium. Adds an extra Redis round-trip per query. The check is the same as `function_exists_in_graph`, which is already done in the pre-check.
- **What might break:** If a column name happens to match a function name (unlikely in OFSAA convention), the routing could be ambiguous.
- **Architectural fit:** Moderate. It's a workaround rather than a fix. Candidate A is cleaner.

**Recommended candidate for the fix PR:** Candidate A — lowest effort, reuses proven logic, no new LLM dependency, minimal blast radius.

---

## Section 6 — Scope of Impact

### Does this affect just FUNCTION_LOGIC, or more?

- **COLUMN_LOGIC queries:** Affected by primary bug (enriched-string key miss). 0 nodes → fallback. Confirmed on Q1, Q2, Q4.
- **VARIABLE_TRACE queries:** Affected by **both** bugs. Even when the LLM correctly sets `target_variable` to a column name, `resolve_variable_nodes` looks up the column in the column index — which is a single-entry dict `{'OFSMDM': [...]}` due to Bug 7.2 (index overwrite). No real column name is ever found. 0 nodes → fallback. Confirmed on Q6 (`N_ANNUAL_GROSS_INCOME`, col_index_size=1, alias_hits empty).
- **COLUMN_LOGIC where LLM puts function in `target_variable`:** Affected by both — Branch A fires, function name passed to `resolve_variable_nodes`, never in column index. Confirmed on Q3 (`TLX_OPS_ADJ_MISDATE` in target_variable, col_index_size=1).
- **VALUE_TRACE / DIFFERENCE_EXPLANATION:** Routed to `_phase2_stream` before reaching the graph pipeline block. Not affected.
- **DATA_QUERY:** Routed to `_data_query_stream` before reaching the graph pipeline block. Not affected.

**Revised conclusion:** The graph-payload path (`state["llm_payload"]`) has never produced a response for any query type. Bug 7.1 (enriched string) breaks function queries. Bug 7.2 (index overwrite) breaks variable queries. Together they ensure the graph pipeline never contributes to Phase 1 output.

### Were W33/W37/W39/W41 validations testing what we thought?

- **W33 (baseline):** Regression check used FUNCTION_LOGIC queries. The graph pipeline has never been used for these. Validation confirmed reasonable answers (from fallback) but not graph-pipeline usage.
- **W37 (function pre-check):** Validated that DECLINED responses trigger correctly for unknown functions. The pre-check is correct and independent of the graph pipeline. W37 is fine.
- **W39 (hierarchy annotation):** Validated that the hierarchy header appears. The hierarchy header (`logic_explainer.hierarchy_header()`) is loaded independently from Redis per the function name, bypassing the broken graph pipeline. W39 validated the header, not graph payload delivery.
- **W41 (grounding):** Validated grounding evaluation against LLM output. Grounding works on the raw-source fallback content. The grounding badge is correct but reflects source-based, not graph-based, analysis.

**Implication:** All four validations were testing the fallback path, not the graph pipeline. The "graph-as-source-of-truth" principle has not been exercised for FUNCTION_LOGIC queries in any released version.

The DATA_QUERY and VALUE_TRACE paths are not affected (they use different routing). Their graph lookups — if any — would need separate verification.

---

## Section 7 — Incidental Findings

### 7.1 `state["object_name"]` dual purpose

`state["object_name"]` serves two distinct roles: semantic search embedding input (intended) and graph lookup identifier (unintended). The field name implies a single object name, but its content is a multi-word enriched query. Renaming it or adding a parallel field would clarify intent.

### 7.2 CRITICAL: Full graph and column index are overwritten by each module load

**This is a separate, independently impactful bug from the W43 primary finding.**

`load_all_functions` (`src/parsing/loader.py:380-389`) calls `store_full_graph` and `store_column_index` at the end of each module's load. Both calls use `SET` which **overwrites** whatever was stored by previous module loads.

Startup module order and what `graph:full:OFSMDM` / `graph:index:OFSMDM` contain after each:
1. ABL_CAR_CSTM_V4 → 14 nodes from ABL functions
2. OFSDMINFO_ABL_DATA_PREPARATION → 59 nodes, 18 edges ← correct data loaded here
3. TEST_BATCH_WITH_MANIFEST → 2 nodes ← correct data destroyed
4. TEST_MODULE → 1 node (TEST_SIMPLE) ← final state, wrong data wins

**Live Redis state confirmed:**
- `graph:full:OFSMDM`: 1 node, 0 edges
- `graph:index:OFSMDM`: `{'OFSMDM': ['TEST_SIMPLE:TEST_SIMPLE_N1']}` — schema name as key, not column names

**Impact:** VARIABLE_TRACE queries ALSO always return 0 nodes from the graph pipeline, making the graph pipeline non-functional for ALL query types. The W39 variable tracer (which uses its own alias-map path, not `resolve_variable_nodes`) is unaffected — but the Phase 1 graph payload path is completely broken.

**File:line:** `src/parsing/loader.py:381` (`store_full_graph`), `src/parsing/loader.py:389` (`store_column_index`).

### 7.3 Pre-check extracts identifier correctly but result is not reused

`extract_function_candidates(query)` is called at `main.py:1218` (pre-check path). The correctly-extracted function name is used only to verify existence (DECLINED vs. not), then discarded. The graph pipeline re-derives the identifier from `state["object_name"]` using a different (broken) approach. Wiring the extracted candidate through would fix the primary bug.

### 7.4 resolve_function_nodes does not uppercase function_name

`resolve_function_nodes` (`query_engine.py:297`) passes `function_name` as-is to `get_function_graph`. If a caller passes a mixed-case name, it would miss the stored key. `function_exists_in_graph` explicitly uppercases. Latent — currently masked by the larger bug but would surface once the primary fix is applied if the extracted name is not uppercased before use.

### 7.5 Duplicate OFSMDM keys with spaces

Redis contains both `graph:OFSMDM:JURISDICTION CODE ASSIGNMENT` (spaces) and `graph:OFSMDM:JURISDICTION_CODE_ASSIGNMENT` (underscores) for the same logical function. The space-variant keys were stored when ABL_CAR_CSTM_V4 was first parsed. Neither the pre-check nor the graph pipeline can retrieve them (both use underscore forms). These are orphaned keys consuming Redis memory.

### 7.6 Fallback produces VERIFIED badge

The grounding evaluator marks responses VERIFIED when identifiers from the answer appear in the source. Since the fallback uses the real PL/SQL source, this check passes. There is no badge or metadata signal distinguishing graph-path from fallback-path responses. All 4 FUNCTION_LOGIC queries returned VERIFIED while using the raw-source fallback — indistinguishable from a graph-path VERIFIED response in the current UX.

### 7.7 W43_DIAG confirmed hypothesis on first query

The enriched-string diagnosis was confirmed immediately from Q1 logs:
```
stage=resolve_function_nodes_entry
  function_name='How does FN_LOAD_OPS_RISK_DATA work? Explain how the PL/SQL function FN_LOAD_OPS...'
  redis_key_attempted='graph:OFSMDM:How does FN_LOAD_OPS_RISK_DATA work? Explain how the PL/SQL ...'
  function_name_is_multiword=True
  
stage=resolve_function_nodes_result  cache_hit=false  node_count=0  
  diagnosis='key_miss_or_deserialize_error'
```
No ambiguity. The W43_DIAG instrumentation should be kept as permanent infrastructure (similar to W34 timing lines).
