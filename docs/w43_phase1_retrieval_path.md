# W43 Diagnostic: Phase 1 Retrieval Path Map

**Branch:** diagnostic/w43-graph-fallback  
**Date:** 2026-04-22  
**Purpose:** Document the exact code path from `/v1/stream` to the graph retriever for FUNCTION_LOGIC queries.

---

## 1. Entry Point

**File:** `src/main.py:582`  
**Route:** `POST /v1/stream`  
**Function:** `async def stream_endpoint(request: QueryRequest, req: Request)`

All queries arrive here. A FUNCTION_LOGIC query ("How does FN_LOAD_OPS_RISK_DATA work?") proceeds through the following stages.

---

## 2. Query Classification — How Identifiers Are Extracted

**File:** `src/agents/orchestrator.py:322`  
**Method:** `Orchestrator.classify_query(query, state, provider, model)`

The LLM is invoked with `CLASSIFICATION_SYSTEM_PROMPT` to produce a JSON object:

```
{
  "query_type": "COLUMN_LOGIC",
  "intent": "...",
  "search_terms": [...],
  "target_variable": null,   ← or the function name, depending on LLM
  "schema_name": "OFSMDM",
  ...
}
```

**CRITICAL — line 381-384:**
```python
enriched_query = f"{query} {result.intent} {' '.join(result.search_terms)}"
state["query_type"]      = result.query_type           # "COLUMN_LOGIC"
state["object_name"]     = enriched_query              # ← FULL ENRICHED STRING
state["target_variable"] = result.target_variable or ""  # may be "" or function name
state["schema"]          = result.schema_name
```

`state["object_name"]` is set to the **enriched semantic search query** — a long string like:
`"How does FN_LOAD_OPS_RISK_DATA work? Explain the PL/SQL function FN_LOAD_OPS_RISK_DATA logic FN_LOAD_OPS_RISK_DATA operational risk data loading"`

This value is intended for the semantic vector search embedding (used at `main.py:734`), **not** as a bare function name.

`ClassificationResult` has no dedicated field for a function object name. The only named-identifier field is `target_variable` (Optional[str]).

---

## 3. Graph Retrieval Function Called

**File:** `src/main.py:759-806`  
**Called:** `resolve_query_to_nodes()` imported from `src/parsing/query_engine.py`

### How the search term is selected (main.py:761-773):

```python
target_var = state.get("target_variable", "").strip()   # "" or function name
obj_name   = state.get("object_name", "").strip()       # enriched query string

if target_var:
    g_query_type = "variable"
    g_search_term = target_var          # branch A
elif obj_name:
    g_query_type = "function"
    g_search_term = obj_name            # branch B — uses enriched string!
else:
    g_query_type = "variable"
    g_search_term = state["raw_query"]  # branch C
```

For "How does FN_LOAD_OPS_RISK_DATA work?":
- **If LLM sets `target_variable = None`:** Branch B fires. `g_search_term` = full enriched query string (e.g. 80–200 chars). `g_query_type = "function"`.
- **If LLM sets `target_variable = "FN_LOAD_OPS_RISK_DATA"`:** Branch A fires. `g_search_term = "FN_LOAD_OPS_RISK_DATA"`. `g_query_type = "variable"`.

Both branches fail (see §5).

---

## 4. Input Parameters to resolve_query_to_nodes

**File:** `src/parsing/query_engine.py:25`

```python
node_ids = resolve_query_to_nodes(
    query_type     = g_query_type,      # "function" or "variable"
    target_variable= g_search_term if g_query_type == "variable" else "",
    function_name  = g_search_term if g_query_type == "function" else "",
    table_name     = "",
    schema         = g_schema,          # "OFSMDM"
    redis_client   = _graph_redis,
)
```

`resolve_query_to_nodes` routes to either:
- `resolve_function_nodes(function_name, schema, redis_client)` when `qt == "function"`
- `resolve_variable_nodes(target_variable, schema, redis_client)` when `qt == "variable"`

---

## 5. What Each Branch Returns and Why It Returns 0

### Branch A: query_type="function", function_name=\<enriched string\>

**File:** `src/parsing/query_engine.py:280` (`resolve_function_nodes`)

```python
graph = get_function_graph(redis_client, schema, function_name)
```

**File:** `src/parsing/store.py:61`

```python
key = f"graph:{schema}:{function_name}"
# Actual key tried: "graph:OFSMDM:How does FN_LOAD_OPS_RISK_DATA work? ..."
data = redis_client.get(key)
# data = None — this key does not exist in Redis
return None
```

`resolve_function_nodes` sees `graph is None`, logs a warning, returns `[]`.

**Result: 0 nodes.**

### Branch B: query_type="variable", target_variable="FN_LOAD_OPS_RISK_DATA"

**File:** `src/parsing/query_engine.py:136` (`resolve_variable_nodes`)

1. `resolve_aliases("FN_LOAD_OPS_RISK_DATA", "OFSMDM", redis_client)` → `["FN_LOAD_OPS_RISK_DATA"]` (no alias entry for a function name)
2. `get_column_index(redis_client, "OFSMDM")` → the column→node_id map (keys are COLUMN names like `N_EOP_BAL`, `N_ANNUAL_GROSS_INCOME`, etc.)
3. `col_index.get("FN_LOAD_OPS_RISK_DATA", [])` → `[]` (function names are never column index keys)
4. `direct_nodes = []` → no cross-function traversal happens

**Result: 0 nodes.**

---

## 6. The "0 Nodes → Fallback" Decision

**File:** `src/main.py:785-804`

```python
if node_ids:        # [] is falsy
    # ... graph path (never reached for this query)
else:
    logger.info("Graph returned no nodes, falling back to raw source for query: %s", ...)
    # state["llm_payload"] remains ""
```

Later at `main.py:823`:
```python
if state.get("llm_payload"):   # "" is falsy → skips graph path
    ...
elif state.get("query_type") == "VARIABLE_TRACE":
    ...
else:
    # RAW-SOURCE FALLBACK — stream_semantic with full PL/SQL source
    async for token in _logic_explainer.stream_semantic(state, ...):
        yield token
```

The function `stream_semantic` uses `state["multi_source"]` which contains the raw PL/SQL retrieved by semantic search. This path sends the full PL/SQL file to the LLM, producing the observed 55-60s response times.

---

## 7. The Fallback Path

**File:** `src/agents/logic_explainer.py:658` (`stream_semantic`)

- Takes `state["multi_source"]` — a dict of `{function_name: source_code_string}` populated by `_metadata_interpreter.fetch_multi_logic(state)` at `main.py:746`
- Constructs a prompt with the raw PL/SQL and the user query
- Streams LLM tokens directly
- No graph-structured context, no node types, no execution order

The response content is qualitatively reasonable (LLM reads real source code) but is slower and unstructured compared to the graph-payload path.

---

## Summary Table

| Step | File | Line | What Happens |
|------|------|------|-------------|
| Entry | `main.py` | 582 | POST /v1/stream received |
| Classify | `orchestrator.py` | 322 | LLM classifies → COLUMN_LOGIC; sets `object_name` = enriched string |
| Precheck | `main.py` | 1206 | `function_exists_in_graph` uses `.upper()` correctly → PASSES |
| Graph entry | `main.py` | 759 | `target_var` or `obj_name` selected as `g_search_term` |
| Identifier mismatch | `main.py` | 768 | `g_search_term` = enriched string (not bare function name) |
| Graph lookup | `query_engine.py` | 280 | `get_function_graph` tries key with enriched string → None |
| 0 nodes | `query_engine.py` | 293 | Returns `[]` |
| Fallback decision | `main.py` | 803 | `node_ids` is `[]` → fallback |
| Raw-source stream | `logic_explainer.py` | 658 | Full PL/SQL sent to LLM → 55-60s |
