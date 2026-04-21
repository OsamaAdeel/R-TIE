# W34 Diagnostic — Stage Timing Results

Branch: `diagnostic/w34-latency-timing`
Data collected: 2026-04-21
Backend: `python run.py` (local), Oracle + Redis + Postgres all healthy.
Instrumentation: `[STAGE_TIMING]` lines in `logs/app.log`, extracted per correlation_id.

Client-side TTFT (time-to-first `event: token` line) and total response time were measured
concurrently with the stage timers. All runs were sequential with 2-second gaps.

---

## Raw Timing Tables

### Q1 — FUNCTION_LOGIC ("How does FN_LOAD_OPS_RISK_DATA work?")
Query type: FUNCTION_LOGIC → fallback to raw semantic (graph returned no nodes for this function).

**Run 1** | correlation_id: `2f504f61-bd38-4d6e-bffd-e9a180411e34`

| Stage                            | elapsed_ms  | % of total |
|----------------------------------|-------------|-----------|
| orchestrator_classify            | 6,245       | 9.7%      |
| └─ llm_api_classify (inner)      | 6,207       | 9.6%      |
| function_precheck                | 1.7         | 0.0%      |
| embedding_create                 | 2,643       | 4.1%      |
| vector_search                    | 48.0        | 0.1%      |
| metadata_fetch_multi             | 8.7         | 0.0%      |
| graph_resolve_nodes              | 1.3         | 0.0%      |
| hierarchy_header                 | 1.2         | 0.0%      |
| llm_stream_semantic_fallback     | 55,527      | 86.1%     |
| grounding_evaluate               | 0.4         | 0.0%      |
| done_emit                        | 0.2         | 0.0%      |
| **TOTAL (stage_timer)**          | **64,506**  | —         |
| *Client measured total*          | *66,573*    | —         |
| *Client TTFT*                    | *11,074*    | —         |

*TTFT note*: first `event: token` was the hierarchy_header line (Redis, ~9s into request).
LLM first chunk arrived at ~30,541ms (21.5s after entering stream_semantic).

**Run 2** | correlation_id: `5a6a66b1-a226-4986-84c5-9fb81179856e`

| Stage                            | elapsed_ms  | % of total |
|----------------------------------|-------------|-----------|
| orchestrator_classify            | 6,019       | 8.9%      |
| └─ llm_api_classify (inner)      | 5,990       | 8.8%      |
| function_precheck                | 1.6         | 0.0%      |
| embedding_create                 | 2,326       | 3.4%      |
| vector_search                    | 46.2        | 0.1%      |
| metadata_fetch_multi             | 9.9         | 0.0%      |
| graph_resolve_nodes              | 1.3         | 0.0%      |
| hierarchy_header                 | 1.5         | 0.0%      |
| llm_stream_semantic_fallback     | 59,389      | 87.5%     |
| grounding_evaluate               | 0.5         | 0.0%      |
| done_emit                        | 0.3         | 0.0%      |
| **TOTAL (stage_timer)**          | **67,827**  | —         |
| *Client measured total*          | *69,853*    | —         |
| *Client TTFT*                    | *10,494*    | —         |

---

### Q2 — DATA_QUERY SUM ("What is the total N_EOP_BAL for V_LV_CODE='ABL' on 2025-12-31?")
Query type: DATA_QUERY → AGGREGATE.

**Run 1** | correlation_id: `b6ce2c9a-e40e-4ff1-8791-97afab7e105a`

| Stage                            | elapsed_ms  | % of total |
|----------------------------------|-------------|-----------|
| orchestrator_classify            | 8,698       | 46.2%     |
| └─ llm_api_classify (inner)      | 8,671       | 46.0%     |
| data_query_answer (total)        | 10,127      | 53.8%     |
| └─ data_query_schema_catalog_build | 63.3      | 0.3%      |
| └─ llm_api_sql_generate          | 10,055      | 53.4%     |
| └─ oracle_query_execute          | 4.3         | 0.0%      |
| └─ suspicious_result_check       | 0.0         | 0.0%      |
| data_query_token_stream          | 9.4         | 0.0%      |
| done_emit                        | 0.1         | 0.0%      |
| **TOTAL (stage_timer)**          | **18,836**  | —         |
| *Client measured total*          | *20,853*    | —         |
| *Client TTFT*                    | *20,843*    | —         |

**Run 2** | correlation_id: `703efab1-eff9-4c53-8d35-07be7916c235`

| Stage                            | elapsed_ms  | % of total |
|----------------------------------|-------------|-----------|
| orchestrator_classify            | 7,885       | 47.0%     |
| └─ llm_api_classify (inner)      | 7,851       | 46.8%     |
| data_query_answer (total)        | 8,866       | 52.9%     |
| └─ data_query_schema_catalog_build | 65.5      | 0.4%      |
| └─ llm_api_sql_generate          | 8,794       | 52.5%     |
| └─ oracle_query_execute          | 3.6         | 0.0%      |
| └─ suspicious_result_check       | 0.0         | 0.0%      |
| data_query_token_stream          | 9.7         | 0.0%      |
| done_emit                        | 0.1         | 0.0%      |
| **TOTAL (stage_timer)**          | **16,763**  | —         |
| *Client measured total*          | *18,782*    | —         |
| *Client TTFT*                    | *18,772*    | —         |

---

### Q3 — DATA_QUERY CHAR filter ("How many accounts have F_EXPOSURE_ENABLED_IND='N' on 2025-12-31?")
Query type: DATA_QUERY → AGGREGATE with CHAR column filter.

**Run 1** | correlation_id: `e117c778-c5c2-4558-b8b5-582d77eeff8b`

| Stage                            | elapsed_ms  | % of total |
|----------------------------------|-------------|-----------|
| orchestrator_classify            | 6,190       | 33.8%     |
| └─ llm_api_classify (inner)      | 6,160       | 33.6%     |
| data_query_answer (total)        | 12,115      | 66.1%     |
| └─ data_query_schema_catalog_build | 60.9      | 0.3%      |
| └─ llm_api_sql_generate          | 12,010      | 65.5%     |
| └─ oracle_query_execute          | 35.5        | 0.2%      |
| └─ suspicious_result_check       | 0.0         | 0.0%      |
| data_query_token_stream          | 17.2        | 0.1%      |
| done_emit                        | 0.4         | 0.0%      |
| **TOTAL (stage_timer)**          | **18,325**  | —         |
| *Client measured total*          | *20,331*    | —         |
| *Client TTFT*                    | *20,313*    | —         |

**Run 2** | correlation_id: `0672602f-6d2c-464f-9791-0fa976ffa770`

| Stage                            | elapsed_ms  | % of total |
|----------------------------------|-------------|-----------|
| orchestrator_classify            | 7,445       | 32.6%     |
| └─ llm_api_classify (inner)      | 7,404       | 32.5%     |
| data_query_answer (total)        | 15,329      | 67.3%     |
| └─ data_query_schema_catalog_build | 104.8     | 0.5%      |
| └─ llm_api_sql_generate          | 15,201      | 66.7%     |
| └─ oracle_query_execute          | 19.4        | 0.1%      |
| └─ suspicious_result_check       | 0.0         | 0.0%      |
| data_query_token_stream          | 13.2        | 0.1%      |
| done_emit                        | 0.3         | 0.0%      |
| **TOTAL (stage_timer)**          | **22,793**  | —         |
| *Client measured total*          | *24,804*    | —         |
| *Client TTFT*                    | *24,789*    | —         |

---

### Q4 — VALUE_TRACE ("Why is N_EOP_BAL negative for account PK00108091TR00PKRGBP-T24-LIVEPOSG on 2025-12-31?")
Query type: VALUE_TRACE (Phase 2 ETL origin).

**Run 1** | correlation_id: `5d0abfe7-5f7a-4b66-82b3-f76cbbf0e561`

| Stage                            | elapsed_ms  | % of total |
|----------------------------------|-------------|-----------|
| orchestrator_classify            | 8,904       | 33.0%     |
| └─ llm_api_classify (inner)      | 8,869       | 32.8%     |
| phase2_trace_value               | 18,103      | 67.0%     |
| phase2_token_stream              | 19.6        | 0.1%      |
| done_emit                        | 0.1         | 0.0%      |
| **TOTAL (stage_timer)**          | **27,029**  | —         |
| *Client measured total*          | *29,037*    | —         |
| *Client TTFT*                    | *29,017*    | —         |

**Run 2** | correlation_id: `cc3a7a61-a2e8-4b65-a28f-f3cea0a09122`

| Stage                            | elapsed_ms  | % of total |
|----------------------------------|-------------|-----------|
| orchestrator_classify            | 10,328      | 32.3%     |
| └─ llm_api_classify (inner)      | 10,292      | 32.2%     |
| phase2_trace_value               | 21,569      | 67.5%     |
| phase2_token_stream              | 19.6        | 0.1%      |
| done_emit                        | 0.2         | 0.0%      |
| **TOTAL (stage_timer)**          | **31,919**  | —         |
| *Client measured total*          | *33,946*    | —         |
| *Client TTFT*                    | *33,925*    | —         |

*Gap note*: `phase2_trace_value` is a single 18-22s black box. Sub-stages within
`ValueTracerAgent.trace_value()` (row_inspector, origin_classifier, Phase2Explainer LLM call)
were not yet instrumented — see Incidental Observations.

---

## Analysis

### 1. What stage consistently dominates?

**LLM API calls dominate on every query type. No exceptions.**

| Query type        | Dominant stage(s)                                   | Combined LLM ms (avg) | % of avg total |
|-------------------|-----------------------------------------------------|-----------------------|----------------|
| FUNCTION_LOGIC    | `llm_stream_semantic_fallback`                      | 57,458                | 87%            |
| DATA_QUERY        | `orchestrator_classify` + `llm_api_sql_generate`    | 16,599                | 96%            |
| VALUE_TRACE       | `orchestrator_classify` + `phase2_trace_value`*     | 29,452                | 98%            |

*`phase2_trace_value` is a black box at this instrumentation level but is almost certainly
LLM-dominated (see §6 and Incidental Observations).

Non-LLM contributions are trivially small:
- `embedding_create`: ~2.5s on FUNCTION_LOGIC only (not run on DATA_QUERY/VALUE_TRACE paths)
- `oracle_query_execute`: 4-36ms — effectively free
- `data_query_schema_catalog_build` (Redis): 63-105ms — negligible
- All other stages (precheck, vector_search, metadata_fetch, graph_resolve, hierarchy_header): < 50ms combined

### 2. Is the dominant stage the same across query types?

The *type* of bottleneck is the same (LLM API wait), but the *specific call* differs:

- **FUNCTION_LOGIC**: bottleneck is the final streaming explanation LLM call (55-60s). The
  classify (6s) and embedding (2.3s) are secondary. This query type is not TTFT-broken — tokens
  stream progressively — but the total time is excessive because the fallback path passes full
  raw PL/SQL source to the LLM (large context → slow + expensive).

- **DATA_QUERY**: bottleneck is `orchestrator_classify` + `llm_api_sql_generate` in roughly
  equal proportion. Together they consume 96%+ of the request time. Oracle execution (4-36ms)
  and Redis catalog (65ms) are negligible.

- **VALUE_TRACE**: bottleneck is `orchestrator_classify` (9-10s) + the Phase 2 pipeline (18-22s).
  The classify call is proportionally larger here because the user query contains a complex
  account identifier + date filter, requiring more LLM reasoning.

### 3. Run-to-run variance

LLM calls are highly variable. Oracle and Redis are stable.

| Stage                        | Run-pair     | Min (ms) | Max (ms) | Spread |
|------------------------------|--------------|----------|----------|--------|
| `orchestrator_classify`      | Q2, Q3, Q4   | 6,019    | 10,328   | 72%    |
| `llm_api_sql_generate`       | Q2, Q3       | 8,794    | 15,201   | 73%    |
| `phase2_trace_value`         | Q4           | 18,103   | 21,569   | 19%    |
| `embedding_create`           | Q1           | 2,326    | 2,643    | 14%    |
| `oracle_query_execute`       | Q2, Q3       | 3.6      | 35.5     | < 35ms |
| `data_query_schema_catalog_build` | Q2-Q3  | 60.9     | 104.8    | stable  |

The LLM variance (up to 73%) is consistent with OpenAI API behaviour under variable load.
Oracle and Redis are essentially constant (sub-50ms, sub-110ms).

### 4. Does TTFT equal total response time?

**For DATA_QUERY and VALUE_TRACE: yes, TTFT ≈ TOTAL (delta < 20ms).**

These routes emit no streaming tokens during pipeline execution. All work is done — SQL
generated, Oracle queried, LLM explanation collected — and only then does a `_chunk_text` loop
re-emit the result as a burst of 4-char SSE events. From the client's perspective the stream
is silent for the entire request duration, then delivers all tokens in under 20ms.

**For FUNCTION_LOGIC: TTFT ≠ total.** The pipeline overhead (classify + embedding + search +
metadata + graph + hierarchy_header) takes ~9s before the first SSE token (the hierarchy
header line). LLM streaming then begins at ~30s and continues until ~64s. This is genuine
progressive streaming — tokens arrive one at a time from the LLM — but there is still a
~9s silent window before any text appears.

### 5. Does the SSE layer emit events incrementally, or all at the end?

**Depends on query type:**

- **FUNCTION_LOGIC**: genuinely incremental. The `llm_stream_semantic_fallback` stage runs
  `async for chunk in llm.astream(...)` and yields each token as it arrives. Client receives
  tokens continuously from ~11s onwards. The 9s pre-stream silent window comes from upstream
  pipeline stages, not the SSE layer.

- **DATA_QUERY and VALUE_TRACE**: batch-at-end. `first_sse_token_emit` (measured via
  `mark_event`) fires immediately before `_chunk_text(explanation)` starts. The rechunk loop
  itself is 9-20ms for 320-670 chars. All token events arrive in that 9-20ms window — the
  frontend sees a burst, not a stream.

  The SSE transport is not the problem here. `_chunk_text` and `StreamingResponse` are
  working as designed. The issue is that the explanation string doesn't exist until all LLM
  and Oracle work completes, so there is nothing to stream earlier.

### 6. 80/20 fix candidates

Listed by expected latency reduction vs implementation risk.

**Candidate A — Stream the DATA_QUERY explanation before SQL runs (structural)**
- What: for DATA_QUERY, start emitting a "running your query..." status token before the LLM SQL
  generation begins, and begin streaming the final explanation as soon as Oracle returns results.
  The explanation builder (`_build_explanation`) is deterministic once rows are available. It
  can be called during the token-stream phase instead of before.
- Impact on TTFT: full 18-25s improvement (TTFT becomes "time after Oracle returns" ≈ 50ms,
  vs current "after LLM SQL gen + Oracle" ≈ 18-25s). Zero improvement to total latency.
- Risk: low — structural refactor of `_data_query_stream` in main.py; no agent logic changes.

**Candidate B — Stream the VALUE_TRACE explanation (structural)**
- What: `Phase2Explainer` currently uses `llm.ainvoke()` (blocking, collects full response).
  Switching to `llm.astream()` and yielding tokens through `_phase2_stream` as they arrive
  would make VALUE_TRACE genuinely progressive.
- Impact on TTFT: reduces TTFT from ~29-34s to ~9-11s (classify + row_inspector + origin, i.e.
  time before LLM explanation starts). Total latency unchanged.
- Risk: medium — requires modifying `Phase2Explainer.explain()` signature to async generator +
  threading through `_phase2_stream`. Worth it.
- Note: `phase2_trace_value` (18-22s) still needs deeper instrumentation to confirm that LLM is
  its primary cost vs Oracle row fetch. See Incidental Observations.

**Candidate C — Use a faster model for `orchestrator_classify`**
- What: classify prompt is a structured JSON extraction task (not open-ended generation). A
  smaller, faster model (e.g. gpt-4o-mini instead of gpt-4o) may produce equivalent results
  for classification while cutting 6-10s to 1-2s.
- Impact: ~6-9s savings per request across ALL query types — this is the only LLM call shared
  by every path.
- Risk: medium — classification accuracy must be validated on representative query sets before
  deploying. Mis-classification (e.g. VALUE_TRACE classified as DATA_QUERY) produces wrong
  responses, not just slow ones.
- Upper bound: if this saves 8s, DATA_QUERY goes from 18-25s to 10-17s. Not a full fix alone.

**Candidate D — Use a faster model for `llm_api_sql_generate`**
- What: SQL generation from a structured schema catalog is a constrained, well-defined task.
  gpt-4o-mini benchmarks well on SQL generation benchmarks.
- Impact: ~8-13s savings on DATA_QUERY. Combined with Candidate C, DATA_QUERY could drop from
  18-25s to ~3-5s total.
- Risk: medium — SQL correctness must be validated. Guardian safeguards catch DML/DDL but not
  semantic errors.

**Candidate E — Prompt caching on classification system prompt**
- What: the `CLASSIFICATION_SYSTEM_PROMPT` (lines 107-248 in orchestrator.py) is a large,
  static prompt sent with every request. OpenAI supports automatic prompt caching for prompts
  > 1024 tokens. If the prefix matches a cached prompt, the first N tokens of the API call are
  served from cache at reduced latency.
- Impact: latency reduction on classify cache hits: likely 30-60% reduction (anecdotally 2-4s
  saved on a 6-10s call). No code change needed for OpenAI (automatic); explicit cache_control
  blocks needed for Anthropic.
- Risk: very low — transparent, server-side, no behavior change.

**Upper bound analysis:**

For DATA_QUERY (total ≈ 18-25s):
- LLM calls (classify + SQL gen) = ~17s avg = 96% of total.
- Non-LLM work = ~1s.
- Eliminating ALL LLM calls would bring total to ~1s. That is the ceiling.
- Realistic target (Candidates C+D): classify to 2s + SQL gen to 3s = ~7s total.
- Adding streaming (Candidate A): TTFT drops to ~50ms regardless.

For VALUE_TRACE (total ≈ 27-34s):
- phase2_trace_value is a black box (18-22s). Even if classify (9s) drops to 2s, total becomes
  ~20-24s. The sub-stages of phase2 need instrumentation (see Incidental Observations).

For FUNCTION_LOGIC (total ≈ 64-70s, TTFT already streaming):
- 87% is `llm_stream_semantic_fallback`. This path was triggered because graph returned no
  nodes for FN_LOAD_OPS_RISK_DATA, forcing the fallback which sends large raw source code.
  If the graph were populated for this function, it would use the structured payload path
  (smaller context → faster LLM call).
- Two independent fixes: (1) populate the graph for this function, (2) investigate why
  graph_resolve_nodes returned 0 nodes when FN_LOAD_OPS_RISK_DATA exists in db/modules/.

---

## Incidental Observations

**1. `phase2_trace_value` is uninstrumented internally.**
The 18-22s black box (Q4) contains: `RowInspector.fetch_target_row` (Oracle SELECT),
`OriginClassifier.classify` (Redis), `EvidenceBuilder.build_for_etl_origin` (sync),
`Phase2Explainer.explain` (LLM `ainvoke`). The LLM call is almost certainly dominant
(similar to classify at ~8-10s), but we cannot confirm without adding `stage_timer` inside
`src/agents/value_tracer.py` and `src/phase2/explainer.py`. Recommend as first instrumentation
addition in the fix PR.

**2. `FN_LOAD_OPS_RISK_DATA` is not resolving in the graph.**
`graph_resolve_nodes` ran and returned 0 nodes for Q1. This forces the raw-source fallback
(~57s stream) instead of the structured graph payload path (expected to be faster due to
smaller, structured context). This may mean the function is not indexed, uses a different
schema, or the function name in the query doesn't match the stored key. Recommend checking
`get_function_graph(redis, schema, "FN_LOAD_OPS_RISK_DATA")` and the graph loader logs.

**3. Client vs backend total time diverges by ~2s.**
Backend `total_request` consistently reports 2-3s less than client-measured total. This gap
represents: event loop scheduling overhead, asyncio task handoff, HTTP transport to/from
localhost, and SSE line detection in the client. Negligible for analysis purposes, but confirms
the timer is measuring server-side accurately.

**4. `data_query_schema_catalog_build` spikes occasionally (63ms → 105ms).**
Redis catalog reads are generally 63-65ms but spiked to 105ms on Q3 run 2. Not material, but
worth monitoring if catalog grows. It builds a full per-table column listing including type
metadata — may warrant a short TTL in-memory cache.

**5. `suspicious_result_check` returned 0ms for all AGGREGATE queries.**
This means `_check_suspicious_result` is short-circuiting without an Oracle baseline query.
Looking at the code: the function checks `if query_kind != "AGGREGATE" or not rows` — since
the aggregate returned rows (non-empty result), `not rows` is False, but the aggregate may have
returned a non-zero value, causing early exit at `if first_value not in (0, 0.0, None, "0")`.
This is correct behaviour; just noting it confirms the suspicious-result check path is not
adding Oracle latency for these queries.
