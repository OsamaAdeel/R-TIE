# W80c — Hybrid Graph + Vector Retrieval (Stage 1 Diagnostic)

**Status:** Diagnostic only — no code changes.
**Date:** 2026-05-18
**Branch:** none (read-only investigation)
**Author trail:** W80 v1 (top_k floor) → W80a (description quality) → W80b (per-query-type top_k) → **W80c (this).**

---

## TL;DR

The premise of W80c is confirmed. All three currently-missing canary
functions are **one cross-function edge away** from a function that
vector search already surfaces, and the edges to traverse already exist
in Redis at [`graph:full:OFSERM`](../src/parsing/store.py) (2,249
`CROSS_FUNCTION_TABLE_FLOW` edges, 1.1 MB).

W80c does NOT need a new edge-indexer. It needs:
1. an in-memory reader for the existing cross-function edges,
2. a small traversal that expands the top-K vector hits by 1 hop,
3. a reranker that fuses cosine score with edge-derived signals.

The recommended slot is between the `_run_scoped_vector_search` call
and the `ensure_anchor_in_search_results` call in [main.py:1145-1173](../src/main.py#L1145-L1173).

---

## Section 1 — Edge inventory

### 1.A Per-function graph (`graph:<schema>:<fn>`)

166 OFSERM functions + 12 OFSMDM functions persisted. Each is a
MessagePack blob containing `nodes`, `edges`, `column_index`,
`hierarchy`. Built by [builder.py:56](../src/parsing/builder.py#L56)
(`build_function_graph`). Each node carries:

- `target_table` — single uppercased table the node writes
- `source_tables` — list of uppercased tables the node reads
- `column_maps`, `calculation`, `conditions` — used by `_extract_written_columns` / `_extract_read_columns` in [indexer.py:227-301](../src/parsing/indexer.py#L227)

This is the atomic substrate. Reading 166 of these per request is too
much; we want a pre-built aggregate.

### 1.B Cross-function graph (`graph:full:<schema>`) — **the W80c gold**

Built by [`build_cross_function_graph`](../src/parsing/indexer.py#L14)
at loader time and persisted via
[`store_full_graph`](../src/parsing/store.py#L79). Live sizes:

| Key                  | Bytes      | Edges    | Edge type |
|----------------------|------------|----------|-----------|
| `graph:full:OFSERM`  | 1,100,641  | 2,249    | 100% `CROSS_FUNCTION_TABLE_FLOW` |
| `graph:full:OFSMDM`  | 127,550    | (~unmeasured, small) | same |

Each edge record:

```python
{
  "id": "CROSS_E1",
  "type": "CROSS_FUNCTION_TABLE_FLOW",
  "from": "<from_node_id>",
  "to":   "<to_node_id>",
  "table": "FCT_ENTITY_INFO",
  "from_function": "CAP_CONSL_NON_REGULATORY_ENTITY_..._IDENTIFICATION",
  "to_function":   "ABL_SIGNIFCNT_INVSTMNT_IN_NON_REG_CONSL_ENTITY_DATA_POP",
  "matching_columns": ["F_SIGNIFICANT_INVESTMENT_IND"],  # may be []
}
```

The `matching_columns` field is critical: a 0-element overlap means the
writer's columns and the reader's columns share no name in common
(weak edge, often just join keys). A 5-element overlap is a strong
data-flow signal.

### 1.C Cross-schema edges — **gap**

The loader scopes `build_cross_function_graph` to the **primary
schema only** ([loader.py:476-485](../src/parsing/loader.py#L476)).
That means:

- OFSMDM-writes → OFSERM-reads edges are **not** in either
  `graph:full:*` rollup.
- For the significant-investment canary this is fine: **all 5 target
  functions live in OFSERM** (Section 2.A).
- For future cross-schema canaries (e.g. an OFSMDM module raw-load
  feeding an OFSERM enrichment), the W80c traversal would need
  per-schema rollups merged at query time, or the loader would need a
  new "global" rollup. Out of scope for the initial implementation.

### 1.D Global column index (`graph:index:<schema>`)

| Key                   | Bytes    | Entries |
|-----------------------|----------|---------|
| `graph:index:OFSERM`  | 348,955  | 949 columns/tables |
| `graph:index:OFSMDM`  | 40,249   | (~unmeasured) |

Built by [`build_global_column_index`](../src/parsing/indexer.py#L91).
Shape: `{COLUMN_OR_TABLE_NAME: ["FN_NAME:node_id", ...]}`. **Indexes
table names as well as column names** (lines 113-126), so it is
already usable as a "tables-touched-by → functions" lookup.

The README baseline says ~385 KB; the live value is 349 KB, within
the expected drift band.

### 1.E BI literal index (`graph:literal:<schema>:<id>`)

141 OFSERM keys, 0 OFSMDM keys (CAP-codes are OFSERM-only by design).
Each is a list of `{function, line, role}` records. Roles include
`filter`, `case_when_target`, etc. Already used by `apply_bi_routing`
for anchor stamping. **Not yet used as a retrieval co-occurrence
signal** — see Section 3 for proposed use.

### 1.F Manifest hierarchy

Stored at `hierarchy:<batch_name>` (JSON). Already drives W89 chain
ordering of `multi_source` post-retrieval. Each per-function graph
also carries an inline `hierarchy` block (`batch`, `process`,
`sub_process_path`, `task_order`). This is read by W89 via
[`get_function_graph`](../src/agents/chain_ordering.py#L64) at the
reorder step.

For W80c, co-membership in the same `process` or same
`sub_process_path` is a strong "these belong to the same logical
chain" signal — see Section 3.

---

## Section 2 — Reachability probe

### 2.A The 5 canary functions, all in OFSERM

| # | Function | Writes | Reads (selected) | task_order | sub_process_path |
|---|----------|--------|------------------|------------|------------------|
| 1 | `CAP_CONSL_NON_REGULATORY_ENTITY_SIGNIFICANT_INVESTMENT_IDENTIFICATION` | `FCT_ENTITY_INFO` | DIM_BASEL_CAP_CONSL_APPR, FCT_ENTITY_INFO, … | 2 | CONSOLIDATION_DATA_POPULATION |
| 2 | `ABL_SIGNIFCNT_INVSTMNT_IN_NON_REG_CONSL_ENTITY_DATA_POP` | `FSI_NON_REG_CONSL_ENTITY_INVST` | FCT_PARTY_SHR_HLD_PERCENT, FSI_CAP_INVESTMENT_EXPOSURES, FCT_ENTITY_INFO | 2 | ABL_SIGNIFICANT_INVESTMENT_IN_ENTITIES_OUTSIDE_REG_CONSOLIDATION_PROCESSING |
| 3 | `SIGNIFICANT_INVESTMENT_IN_PARTY_FOR_REPORTING_BANK_IDENTIFICATION` | `FCT_PARTY_SHR_HLD_PERCENT` | FCT_ENTITY_INFO, DIM_BASEL_CAP_CONSL_APPR | 6 | CONSOLIDATION_DATA_POPULATION |
| 4 | `SIGNIFICANT_INVST_THRESHOLD_TREATMENT_DATA_POP` | `FSI_THRESHOLD_TREATMENT` | `FSI_NON_REG_CONSL_ENTITY_INVST`, DIM_STANDARD_ACCT_HEAD | 1 | THRESHOLD_TREATMENT_CALCULATIONS |
| 5 | `SIGNFCNT_INVSTMNT_CAP_DEDUCTION_EXPOSURES` | `FSI_CAP_DEDUCTION_EXPOSURES` | `FSI_NON_REG_CONSL_ENTITY_INVST`, `FCT_PARTY_SHR_HLD_PERCENT`, `FCT_ENTITY_INFO`, FSI_CAP_INVESTMENT_EXPOSURES | 5 | ABL_CAPITAL_STRUCTURE_DEDUCTIONS_RWA_EXPOSURES |

All five share `batch=ABL_CAR_CSTM_V4`. No cross-schema boundary
to worry about for this canary.

### 2.B Adjacency diagram — surfaced {1,2} → missing {3,4,5}

```
                    (vector-surfaced post-W80b at ranks 1, 2)
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼ FCT_ENTITY_INFO     ▼ FCT_ENTITY_INFO     ▼ FCT_ENTITY_INFO
       (1) ─────────────────► (3) MISSING          (5) MISSING
       (1) ─────────────────► (2) ──────────────► (5) MISSING
                              │  FSI_NON_REG_
                              │  CONSL_ENTITY_
                              │  INVST  (3 cols)
                              ▼
                             (5) MISSING
                              │
                              │  FSI_NON_REG_
                              │  CONSL_ENTITY_
                              │  INVST  (5 cols)
                              ▼
                             (4) MISSING

       (3) MISSING ──────────► (2)              via FCT_PARTY_SHR_HLD_PERCENT
       (3) MISSING ──────────► (5) MISSING      via FCT_PARTY_SHR_HLD_PERCENT
```

**Every missing target {3, 4, 5} is reachable in exactly 1 hop from
at least one surfaced target {1, 2}.** Direct cross-function edges
counted: 7 (within the 5-set).

Concretely, the edges out of the surfaced set:

| From | Table | matching_columns | To |
|------|-------|------------------|----|
| 1 | FCT_ENTITY_INFO | `[F_SIGNIFICANT_INVESTMENT_IND]` (1) | **2** (already surfaced) |
| 1 | FCT_ENTITY_INFO | `[]` (0) | **3** (missing) |
| 1 | FCT_ENTITY_INFO | `[]` (0) | **5** (missing) |
| 2 | FSI_NON_REG_CONSL_ENTITY_INVST | `[F_SIGNIFICANT_INVESTMENT_IND, N_CET1_INVESTMENT_AMOUNT, N_GAAP_SKEY, N_MIS_DATE_SKEY, N_RUN_SKEY]` (5) | **4** (missing) |
| 2 | FSI_NON_REG_CONSL_ENTITY_INVST | `[N_GAAP_SKEY, N_MIS_DATE_SKEY, N_RUN_SKEY]` (3) | **5** (missing) |

The strongest signals are the {2}→{4} edge (5 matching columns) and
the {2}→{5} edge (3 matching columns, all `..._SKEY` join keys plus
the actual investment amount). The {1}→{3,5} edges have zero matching
columns — the table is touched in both, but the columns shared are
just dimension surrogates that pass through every function in the
chain.

### 2.C Out-of-corpus / schema-boundary check

Negative — all 5 in OFSERM. No traversal required across schema
boundaries for this canary. The traversal stays within
`graph:full:OFSERM`.

### 2.D Neighbor explosion warning

```
Function                                                         neighbors
─────────────────────────────────────────────────────────────────────────
CAP_CONSL_NON_REGULATORY_ENTITY_SIGNIFICANT_INVESTMENT_IDENTIFICATION   114
ABL_SIGNIFCNT_INVSTMNT_IN_NON_REG_CONSL_ENTITY_DATA_POP               103
SIGNIFICANT_INVESTMENT_IN_PARTY_FOR_REPORTING_BANK_IDENTIFICATION       6
SIGNIFICANT_INVST_THRESHOLD_TREATMENT_DATA_POP                         17
SIGNFCNT_INVSTMNT_CAP_DEDUCTION_EXPOSURES                              16
```

The first two seeds touch widely-used dimension tables (DIM_DATES,
DIM_GAAP, DIM_RUN, FCT_ENTITY_INFO) and therefore have 100+ 1-hop
neighbors. **An unscored "include all 1-hop neighbors" expansion
would dump 200+ candidates into the fetch stage.** The rerank
absolutely needs the `matching_columns` count as a weight, otherwise
the dimension-table edges drown out the load-bearing ones.

### 2.E BI literal coverage of the 5 targets

| Function | BI references found |
|----------|---------------------|
| 1 | (none) |
| 2 | CAP897 (filter) |
| 3 | (none) |
| 4 | CAP928 (filter) |
| 5 | CAP925, CAP926, CAP927 (all filter) |

Useful for CAP-code queries but the canary's query text
(*"how is significant investment in a non-regulated entity computed"*)
does not name a CAP code, so the BI index can't be the primary
expansion signal here. It is a useful secondary signal: candidates
that share at least one CAP-code with the seed deserve a boost.

---

## Section 3 — Rerank signal candidates

Cosine score (`score`, smaller-is-better — RediSearch returns the
distance, not similarity) is the starting point. For each candidate
function `c` and the seed set `S` (the top-K of the vector hit list,
typically just position 0 or 1 for VARIABLE_TRACE), the following
graph-derived features are available cheaply:

| Signal | What it measures | Redis ops/query | Likely lift on canary |
|--------|------------------|-----------------|-----------------------|
| **Edge presence** `1{∃ e: c↔s, s∈S}` | Is `c` a 1-hop neighbor of any seed? | 0 (in-memory) | High — gates expansion to {3,4,5} |
| **Matching-column count** `max_s |edge(c,s).matching_columns|` | Edge strength: 5 matching cols → strong data flow; 0 → dimension passthrough | 0 (in-memory) | High — discriminates load-bearing from background edges |
| **Multi-seed reachability** `\|{s∈S : ∃ e(c,s)}\|` | Reached from how many seeds | 0 | Medium — {5} is reached from {1, 2, 3} → strong; a generic dim-touching fn is reached from 30 seeds → suppress |
| **Same sub_process_path** `1{path(c) == path(s)}` | Manifest co-membership at the innermost grouping | 0 (per-fn graph already has `hierarchy`) | Medium — disambiguates the wide neighbor sets |
| **Same process** `1{process(c) == process(s)}` | Manifest co-membership at the outer grouping | 0 | Low/medium — too coarse alone (all 5 share batch) |
| **Shared BI literal** `\|BI(c) ∩ BI(s)\|` | Both functions reference the same CAP-code | O(literal lookup) ≈ 1 hash get | Low for non-BI queries, **high** for CAP-code queries |
| **Column co-write** `1{∃ col: c writes col ∧ s writes col}` | Both write to the same fact column — typically siblings of one variable | 0 (via column_index inversion) | Medium — captures the "all the functions that compute X" pattern |

### Recommended fusion shape

**Reciprocal Rank Fusion (RRF)** for the vector rank and the
graph-edge rank, with the graph rank computed as a weighted sum of
the high-signal features:

```
graph_score(c) =
    α · matching_column_sum(c, S)         # primary edge weight
  + β · seed_reach_count(c, S)            # multi-seed reachability
  + γ · same_sub_process_path(c, S)       # manifest co-membership
```

Then fuse:

```
final_rank(c) = 1 / (k + vector_rank(c)) + 1 / (k + graph_rank(c))
```

with `k = 60` (standard RRF constant from Cormack & Clarke 2009).
RRF is order-of-magnitude robust to score-scale differences between
the two retrievers, which is exactly what we need: cosine distance
and the weighted graph score live in completely different units.

A linear-combination alternative (`λ · cosine + (1-λ) · graph`) was
considered but requires score normalization that is fragile to corpus
drift. RRF needs neither normalization nor a per-corpus tuning of λ.

**Initial weights to start at** (subject to canary tuning in Stage 2):
α = 1.0 (matching_columns is already in the right ballpark, max 6),
β = 0.5, γ = 0.5. The fusion only ranks; the graph-score absolute
value is irrelevant.

### Initial expansion policy

To avoid the 114-neighbor explosion: expand only from the top **3**
vector hits (not all 20). That gives us the seed set `S` of strongly-
ranked candidates without letting a rank-20 outlier drag in 100
unrelated neighbors. The expansion bag is then capped at top-K + 10
post-rerank, so the fetch stage sees at most ~30 functions.

---

## Section 4 — Cost analysis verdict

### Measured baselines (from `logs/app.log`)

- `vector_search` at top_k=15, ALL-scope: **90-105 ms** per query.
- This is dominated by the per-schema RediSearch round-trip; KNN
  itself is sub-millisecond at 178 docs.
- Embedding API call (separate stage, ~200-500 ms) dominates total
  retrieval latency.

### Estimated W80c cost

Two design choices govern the cost:

1. **Loading the edge list once at process boot** (recommended):
   `graph:full:OFSERM` is 1.1 MB MessagePack → decoded dict of
   2,249 edges → ~5 MB in memory. Loading takes ~30-50 ms at boot.
   At request time the cost is **zero Redis ops**: a dict lookup
   in `{seed_fn: [(neighbor_fn, table, matching_cols)]}`.

2. **Loading on demand per query**: rejected. Each cold load is 30-50
   ms; warming the OS file cache helps but msgpack decode is the
   bottleneck.

Per-query cost estimate with the boot-load design:

```
~3 seeds × ~100 avg neighbors = ~300 edge lookups (in-memory dict)
+ rerank scoring: ~30 candidates × constant-time feature extraction
+ optional: 30 BI lookups against graph:literal:* (skip when query has no CAP code)
─────────────────────────────────────────────────────
expected stage_timer("graph_rerank") = 1-3 ms
```

**Verdict (one sentence):** W80c at expected depth/breadth (1-hop
expansion from top-3 seeds, RRF fusion against the top-20 vector
slate) adds **1-3 ms** to retrieval, which is **~2% of current
vector_search latency** and well under 1% of the embed+search+fetch
total. The boot-time cost is a one-time 30-50 ms graph load on top
of the existing loader.

### Mitigations if the cost ever bites

- Cap expansion to top-3 seeds (already in the plan).
- Skip the BI overlap feature when `state["bi_routing"]["function"]` is empty.
- Prune dimension-table edges (`matching_columns == [] and table starts with "DIM_"`) at boot-load time — these are pure join-key passthrough, almost never load-bearing for a data-flow trace.

---

## Section 5 — Implementation site recommendation

**Recommended site:** [main.py:1173](../src/main.py#L1173), immediately
**before** the existing `ensure_anchor_in_search_results(state)` call,
inside the `with stage_timer(...)` block.

Pseudocode:

```python
# Stage 2: Semantic search
results, schemas_searched = await _run_scoped_vector_search(...)
state["search_results"] = results
state["schemas_searched"] = schemas_searched

# W80c: graph-aware rerank (NEW)
if state.get("query_type") in {"VARIABLE_TRACE", "COLUMN_LOGIC"} and _graph_redis is not None:
    with stage_timer("graph_rerank", correlation_id, schema_scope=schema_scope):
        state["search_results"] = apply_graph_rerank(
            state["search_results"],
            redis_client=_graph_redis,
            seed_count=3,
            keep_top=resolve_top_k(state.get("query_type")) + 10,
        )
    results = state["search_results"]

# W95: anchor injection
ensure_anchor_in_search_results(state)
results = state["search_results"]
```

**Rationale:**

- **Consistency with W95.** That helper sits in the same gap (between
  vector search and source-fetch) and operates on the same data
  structure (`state["search_results"]` — list of dicts with
  `function_name`, `schema`, `score`). W80c is the same shape of
  intervention, just upstream of W95 so the anchor's injection at
  position 0 can't be displaced by the rerank.
- **Inside `_run_scoped_vector_search` is wrong** — that function is
  the vector-store adapter and shouldn't grow a graph dependency. The
  caller is the orchestrator.
- **Gating on query_type** (VARIABLE_TRACE, COLUMN_LOGIC) follows the
  pattern set by W80b's per-query-type top_k. FUNCTION_LOGIC stays at
  top_k=5 and is anchor-driven; rerank would add cost without
  surfacing new candidates, because the answer is one named function.
- **New module:** `src/agents/graph_rerank.py`. Pure helper, takes the
  vector hits and a Redis client, returns the reranked list. Testable
  in isolation against fixture graphs.

---

## Section 6 — Stage 2 scope split

Two PRs, each independently shippable with its own canary
measurement.

### PR 1 — `src/agents/graph_rerank.py` (helper module)

**Adds:**
- `EdgeIndex` dataclass: in-memory `{from_fn: [(to_fn, table, matching_cols)]}` built from `graph:full:<schema>`. Loaded lazily at first call and cached in a module-level dict keyed by schema.
- `expand_one_hop(seeds: list[str], schema: str) -> list[Candidate]`
- `score_candidate(candidate, seeds, edge_index, hierarchy_for_fn) -> float`
- `rerank_with_rrf(vector_hits: list[dict], seed_count=3, keep_top=30, schema=None) -> list[dict]`
- Unit tests in `tests/unit/agents/test_graph_rerank.py` against a hand-rolled fixture graph (8 functions, ~12 edges, known expected reranks).

**Out of scope for PR 1:**
- Wiring into `main.py` — module is unused at end of PR 1. CI passes; existing canaries unaffected.
- Cross-schema traversal — uses the existing per-schema `graph:full:<schema>` rollup only. Cross-schema is a future PR if/when a canary surfaces a cross-schema gap.

**Canary measurement at PR 1 close:** unit tests demonstrate
correctness on the fixture; no integration canary needed yet because
the wire-in hasn't happened.

### PR 2 — Wire-in at `main.py` + canary regression

**Adds:**
- Insert the `apply_graph_rerank` call at [main.py:1173](../src/main.py#L1173) gated on `query_type` and Redis availability.
- Wrap in `stage_timer("graph_rerank", ...)` so latency cost shows up in the standard timing log.
- Update [`tests/canary/canaries.yaml`](../tests/canary/canaries.yaml) to add or tighten the significant-investment canary: floor lifts from current "≥2 of 5" (W80 v1) toward "≥4 of 5" or "5 of 5", depending on what the wire-in actually achieves.
- Update `docs/w35_diagnostic.md` if any new hardcoded schema defaults are introduced (none expected).

**Canary measurement at PR 2 close:**
- Tier 1 canary set passes (no regression on FUNCTION_LOGIC, UNSUPPORTED, etc.).
- Tier 2 DATA_QUERY canaries unaffected (rerank doesn't run for DATA_QUERY).
- The new significant-investment floor is met. If the floor we hit
  is "≥4 of 5" rather than "5 of 5", the surviving miss is a Stage 3
  ticket, not a Stage 2 blocker.

### Why not three PRs

The "edge index builder at boot" sub-piece doesn't need to ship
separately because the edges already exist in `graph:full:<schema>`.
Pre-computing them at boot is just decoding the same msgpack blob
once; that fits inside PR 1's `EdgeIndex` class without earning its
own PR.

---

## Section 7 — Open questions for Toheed before Stage 2

1. **Cross-schema reachability.** Acceptable to defer? The
   significant-investment canary doesn't need it. The risk is a
   future canary that crosses OFSMDM → OFSERM (e.g. a raw-load
   feeding an enrichment) won't benefit from W80c until the loader
   builds a global rollup. **Recommendation:** defer — log it in
   `docs/w35_diagnostic.md` Section 1 as a known limit, address when
   a canary surfaces the gap.

2. **Should W80c also engage for `FUNCTION_LOGIC`?** Today
   FUNCTION_LOGIC retrieves top_k=5 anchored upstream by W76 / BI
   routing / W87. The W95 helper already handles "anchor missing
   from top-5". Layering W80c on FUNCTION_LOGIC would add cost for
   little expected lift on single-function queries. **Recommendation:**
   no — keep it gated to VARIABLE_TRACE + COLUMN_LOGIC.

3. **Dimension-table edge pruning at boot?** As Section 4 noted, edges
   where `matching_columns == []` and table name starts with `DIM_`
   are almost never load-bearing — they're surrogate-key passthrough.
   Should PR 1's `EdgeIndex` drop them at load time, or keep them and
   rely on the `matching_columns=0` weighting to suppress them?
   **Recommendation:** keep them, rely on the weighting. Less
   pre-processing logic, easier to audit when a rerank looks off.

4. **Cap on candidates added by graph expansion.** Section 3 proposed
   "top-K + 10". Is 10 the right number, or should it scale with
   top_k? VARIABLE_TRACE has top_k=20; adding 10 → 30 candidates,
   which is below the LLM's effective context window for a chain
   narrative. **Recommendation:** start at +10, tune in PR 2 based on
   canary observations.

5. **W80c interaction with W89 chain reorder.** W89 currently runs
   AFTER fetch (post-`fetch_multi_logic`) on `multi_source` keyed by
   function name. W80c runs BEFORE fetch on `search_results`. They
   operate on different lists at different stages so there's no
   direct collision. **Sanity check needed in PR 2:** confirm the
   reordered `multi_source` post-W89 still includes the W80c-added
   candidates (it should — `fetch_multi_logic` reads from
   `search_results`, then W89 reorders what fetch produced). No
   action expected.

6. **Telemetry shape.** What should `stage_timer("graph_rerank")`
   emit? Suggest: `elapsed_ms`, `seed_count`, `expanded_count`,
   `kept_count`, `rank_change_count` (how many positions actually
   moved). This lets us audit at the canary level whether the rerank
   is doing useful work or coasting on the vector ranks.

---

## Appendix — Probe artifacts

The Redis probe script used to produce this report lives at the repo
root as `tmp_w80c_probe.py` (throwaway, not committed). To rerun:

```powershell
python tmp_w80c_probe.py
```

Requirements: backend `.env.dev` Redis on `localhost:6379`, OFSERM
+ OFSMDM corpora already loaded (i.e., `python cli.py index --force`
has been run at least once).
