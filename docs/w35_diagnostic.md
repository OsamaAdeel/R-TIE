# W35 Phase 0 — Multi-Schema Diagnostic

**Branch:** `diagnostic/w35-architecture`
**Captured:** 2026-04-27
**Scope:** Read-only inventory. No source files, tests, configuration, or Redis state were modified.

This document is a snapshot of the codebase + Redis at the time of capture. It is the input to `w35_architecture.md`. Findings are recorded with file:line citations, exact key names, and exact byte counts where applicable so that downstream phases (1–8) have a trustworthy baseline.

> **Environment at time of capture:**
> - Redis Stack container `rtie-redis` healthy, port 6379
> - Postgres container `rtie-postgres` healthy
> - Backend not necessarily live; Redis state was populated by a prior backend startup (most-recent OFSERM `parsed_at` = 2026-04-24T09:35Z; most-recent `graph:full:OFSERM.built_at` = 2026-04-27T07:12Z)
> - Working tree on `main` had one uncommitted modification to `db/modules/ABL_CAR_CSTM_V4/functions/CS_Deferred_Tax_Asset_Net_of_DTL_Calculation.sql` (+177/-23) and untracked `tmp_w42_canary*` scratch files. The branch was created from current main; those changes follow into it but are not staged. They were not used to inform the diagnostic — Redis was inspected as-is.

---

## Section 1 — Hardcoded OFSMDM References

The literal `"OFSMDM"` (case-insensitive) appears in 16 distinct call sites across `src/` and 1 in `config/`. Every site is recorded below. Tests are listed as a single bucket (51 occurrences across 11 test files) since they will be revisited as a class during Phase 1 testing rather than refactored individually.

| File | Line | Classification | Context |
|------|------|----------------|---------|
| [config/settings.yaml](config/settings.yaml#L11) | 11 | CONFIG | `oracle.schema: OFSMDM` — single startup-time schema for snapshot priming + default state seeding |
| [src/parsing/manifest.py](src/parsing/manifest.py#L36) | 36 | HARDCODED_DEFAULT | `RECOGNIZED_SCHEMAS: frozenset[str] = frozenset({"OFSMDM", "OFSERM"})` — gatekeeper for manifest validation; OFSERM already on the list |
| [src/agents/orchestrator.py](src/agents/orchestrator.py#L54) | 54 | HARDCODED_DEFAULT | `_PRECHECK_SCHEMAS = ("OFSMDM", "OFSERM")` — schemas iterated by `function_exists_in_graph()` |
| [src/agents/orchestrator.py](src/agents/orchestrator.py#L65) | 65 | COMMENT | docstring example `(e.g. OFSMDM)` |
| [src/agents/orchestrator.py](src/agents/orchestrator.py#L119) | 119 | COMMENT | LLM classification prompt: `"schema_name": "<Oracle schema name, default OFSMDM>"` |
| [src/agents/orchestrator.py](src/agents/orchestrator.py#L194) | 194 | COMMENT | LLM prompt rule: `schema_name defaults to "OFSMDM" unless another schema is specified.` |
| [src/agents/metadata_interpreter.py](src/agents/metadata_interpreter.py#L124) | 124 | HARDCODED_DEFAULT | `default_schema: str = "OFSMDM"` constructor default; used by `resolve_object()` line 153 and `fetch_multi_logic()` line 346 when `state["schema"]` is empty |
| [src/agents/metadata_interpreter.py](src/agents/metadata_interpreter.py#L131) | 131 | COMMENT | docstring: `Default Oracle schema. Defaults to 'OFSMDM'.` |
| [src/agents/logic_explainer.py](src/agents/logic_explainer.py#L530) | 530 | HARDCODED_DEFAULT | `schema = (state.get("schema") or "").strip() or "OFSMDM"` in `_render_hierarchy_header()` |
| [src/agents/variable_tracer.py](src/agents/variable_tracer.py#L156) | 156 | LOG_MESSAGE | Negative-example template inside an LLM prompt: `## {IDENTIFIER} in TLX_PROV_AMT_FOR_CAP013 (OFSMDM)` (illustrative, not behavior) |
| [src/agents/data_query.py](src/agents/data_query.py#L971) | 971 | LOG_MESSAGE | `_SKIP_TABLE_TOKENS` set used by table-name extraction heuristic to ignore the literal token `OFSMDM` if it slips through tokenization |
| [src/pipeline/state.py](src/pipeline/state.py#L22) | 22 | COMMENT | docstring: `schema: Oracle schema containing the object (e.g. OFSMDM).` |
| [src/pipeline/logic_graph.py](src/pipeline/logic_graph.py#L139) | 139 | HARDCODED_DEFAULT | `state["schema"] = state.get("schema") or "OFSMDM"` at end of `semantic_search` node |
| [src/main.py](src/main.py#L762) | 762 | HARDCODED_DEFAULT | `state["schema"] = state.get("schema") or "OFSMDM"` after vector search in SSE COLUMN_LOGIC stream |
| [src/main.py](src/main.py#L785) | 785 | HARDCODED_DEFAULT | `g_schema = state.get("schema", "OFSMDM")` for graph pipeline node resolution |
| [src/main.py](src/main.py#L1174) | 1174 | HARDCODED_DEFAULT | `schema = state.get("schema") or "OFSMDM"` in `_phase2_stream` (VALUE_TRACE / DIFFERENCE_EXPLANATION) |
| [src/main.py](src/main.py#L1315) | 1315 | HARDCODED_DEFAULT | `schema = state.get("schema") or "OFSMDM"` in `_data_query_stream` (DATA_QUERY) |
| [src/tools/sql_guardian.py](src/tools/sql_guardian.py#L331) | 331 | COMMENT | comment in unqualified-table-name normalizer: `Drop schema prefix if present: OFSMDM.STG_GL_DATA -> STG_GL_DATA` |
| [src/tools/cache_tools.py](src/tools/cache_tools.py#L155) | 155 | COMMENT | docstring example: `pattern: Redis SCAN pattern (e.g. 'logic:OFSMDM:*').` |
| `tests/**/*.py` (11 files) | (51 occurrences) | TEST | All test fixtures and assertions still assume `OFSMDM` as the schema under test. To be revisited as a group during Phase 1; one test (`tests/unit/parsing/test_loader_discovery.py:192`) already asserts an OFSERM-detection branch, which is encouraging |

**Headline counts in `src/`:** 17 OFSMDM references in 9 files, of which **8 are HARDCODED_DEFAULT** and the rest are docstrings/comments/log strings. Five of the eight live in `src/main.py` and `src/agents/*` — the request-path layer — which is where the bulk of Phase 1 surgery will land.

**Already schema-parametric (no Phase 1 work needed):**
- `src/parsing/store.py` — every `_key()` template takes `schema=` as a parameter ([store.py:16-25](src/parsing/store.py#L16-L25)). The Redis key family is already multi-schema by design.
- `src/templates/sql_templates.yaml` — every Oracle template binds `:schema` ([sql_templates.yaml](src/templates/sql_templates.yaml)). Templates do not hardcode an owner.
- `src/parsing/loader.py:_extract_schema_from_source()` ([loader.py:73-84](src/parsing/loader.py#L73-L84)) — derives schema from the `CREATE OR REPLACE FUNCTION schema.name` prefix in each .sql file. Multi-schema discovery is already built into the loader.

---

## Section 2 — Redis State Inventory

Redis was inspected via `docker exec rtie-redis redis-cli`.

### 2.1 Total keyspace
- `DBSIZE` = **1300**

### 2.2 Key family breakdown

| Pattern | Type | Count | Notes |
|--------|------|-------|-------|
| `graph:OFSMDM:<fn>` | string (msgpack) | 38 | per-function parsed graph |
| `graph:OFSERM:<fn>` | string (msgpack) | 380 | per-function parsed graph |
| `graph:meta:OFSMDM:<fn>` | string (msgpack) | 38 | parse metadata (parsed_at, node_count, edge_count) |
| `graph:meta:OFSERM:<fn>` | string (msgpack) | 380 | parse metadata |
| `graph:source:OFSMDM:<fn>` | string (msgpack list[str]) | 38 | raw SQL lines |
| `graph:source:OFSERM:<fn>` | string (msgpack list[str]) | 380 | raw SQL lines |
| `graph:full:OFSMDM` | string | 1 (127,968 B) | aggregated cross-function graph |
| `graph:full:OFSERM` | string | 1 (7,587 B) | aggregated cross-function graph |
| `graph:index:OFSMDM` | string | 1 (40,249 B) | column → function index |
| `graph:index:OFSERM` | string | 1 (**1 B**) | **EMPTY — confirms W47** |
| `graph:aliases:OFSMDM` | string | 1 (620 B) | alias map |
| `graph:aliases:OFSERM` | string | 1 (620 B) | alias map |
| `hierarchy:<batch>` | string (json) | 4 | batches: `OFSDMINFO_ABL_DATA_PREPARATION`, `ABL_CAR_CSTM_V4`, `TEST_BATCH_WITH_MANIFEST`, plus orphan `TEST_BATCH...` |
| `hierarchy:batches` | set | 1 | enumeration of known batch names (set type, not string) |
| `rtie:vec:<module>:<fn>` | hash | 24 | **vector embeddings — 12 unique funcs × 2 module aliases (OFSMDM only)** |
| `rtie:logic:OFSMDM:<fn>` | string | 11 | source-cache from `MetadataInterpreter.fetch_logic` (separate from `graph:source:`) |
| `rtie:schema:snapshot:OFSMDM` | string | 1 | `ALL_TAB_COLUMNS` snapshot for column-type catalog (OFSMDM only) |

Schemas observed in graph keys: **OFSMDM, OFSERM**. (Both are already in `RECOGNIZED_SCHEMAS` in `src/parsing/manifest.py`.)

### 2.3 Verdict on `CS_Deferred_Tax_Asset_Net_of_DTL_Calculation`

**It loads.** The pattern `graph:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION` exists.

| Key | Size | Notes |
|-----|------|-------|
| `graph:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION` | 2,076 B | parsed graph dict |
| `graph:meta:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION` | 143 B | `node_count: 0`, `edge_count: 0`, `parsed_at: 2026-04-24T09:35:09.562587+00:00` |
| `graph:source:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION` | 6,072 B | full source body retrievable; CAP943 / CAP309 / CAP863 literals all present; the MERGE statement is intact |

**Function-name casing.** The on-disk file is `CS_Deferred_Tax_Asset_Net_of_DTL_Calculation.sql` (mixed case). The Redis key is **uppercased** to `CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION` — see [src/parsing/loader.py:70](src/parsing/loader.py#L70) (`_function_name_from_file` calls `.upper()`). Searches with `*CS_Deferred_Tax*` (mixed case) find nothing; searches with `*CS_DEFERRED_TAX*` find the keys. **Phase 1 callers must upper-case the search term before hitting Redis.** This is already the convention in `function_exists_in_graph()` ([orchestrator.py:468](src/agents/orchestrator.py#L468)).

**Why nodes is empty (and CAP943 isn't traceable).** Inspecting the `graph:OFSERM:CS_DEFERRED_TAX...` payload: `nodes: []`, `edges: []`, but `commented_out_nodes` contains exactly one entry — a MERGE node into `FCT_STANDARD_ACCT_HEAD` whose `assignments.N_STD_ACCT_HEAD_AMT` calculation references both `CAP309` and `CAP863` literals (matching the user's claim). The parser successfully extracted the MERGE; the builder classified it as `commented_out_nodes` with `committed_after = false` rather than as a live node. This is an **OFSERM-wide pattern, not a one-off**:

- 380 OFSERM graph dicts; informal sampling shows the bulk fall in the 1.8–4.6 KB range — too small to contain populated `nodes` / `edges` arrays. The five largest OFSERM graphs are 28–42 KB, which is the size range where node arrays become populated; OFSMDM's largest (`POPULATE_PP_FROMGL`) is 71 KB.
- The OFSMDM equivalent `graph:OFSMDM:FN_LOAD_OPS_RISK_DATA` is 13.5 KB and contains `FN_LOAD_OPS_RISK_DATA_N1`, `_N2`, … with populated `target_table`, `source_tables`, `column_maps`. So the parser/builder works for OFSMDM but is silently moving most of OFSERM's MERGE statements into `commented_out_nodes`.
- Because `graph:index:OFSERM` is built from per-function `nodes` / `edges` and most OFSERM functions have `nodes: []`, the index ends up at 1 byte (an empty msgpack dict). This is the structural cause of W47.

**This is a parser/builder bug, not just a missing schema-routing.** Phase 1 must address it (or Phase 1 will produce no traceable edges for OFSERM). Logged below; deferred to Phase 1+ per scope discipline.

### 2.4 Vector store coverage

`FT.INFO idx:rtie_vectors` reports `num_docs: 24`. The unique function set is 12; each is duplicated under two `module` tag values: `OFSDMINFO_ABL_DATA_PREPARATION` (the canonical module) and `DATA_PREPARATION` (an alias). **No OFSERM functions are present in the vector store.** Confirms W35.

`CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION` has no vector embedding. Any natural-language query for "deferred tax asset" will not match it via semantic search; it will only match via exact-name lookup against `graph:OFSERM:*` (which is what the W37 pre-check does).

### 2.5 Issues found during inventory (logged, NOT fixed in this PR)

1. **OFSERM graph dicts predominantly classify MERGE nodes as `commented_out_nodes`.** Root cause likely in `src/parsing/builder.py` / `src/parsing/parser.py`'s `committed_after` heuristic — possibly mis-detecting OFSAA's wrapper macros (`P_V_TASK_ID`, `IF P_V_RUN_EXECUTION_ID...`) as inactive guards. Concrete example: `graph:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION`. **Phase 1 candidate.**
2. **Function-name collisions across the OFSMDM keyspace.** `graph:OFSMDM:*` contains both `BASEL_III_CAPITAL_CONSOLIDATION_APPROACH_TYPE_RECLASSIFICATION_FOR_AN_ENTITY` (underscored) and `BASEL III CAPITAL CONSOLIDATION APPROACH TYPE RECLASSIFICATION FOR AN ENTITY` (space-separated) — same logical function, two keys. Same for `JURISDICTION_CODE_ASSIGNMENT`, `CAP_CONSL_EFFECTIVE_SHAREHOLDING_PERCENT_FOR_AN_ENTITY`, `RUN_PRODUCT_CODE_ASSIGNMENT`, `THIRD_PARTY_MINORITY_HOLDING...`. The space-separated names appear in `graph:full:OFSMDM`'s aggregated node list, suggesting the loader is reading from two sources (file basename vs. node-internal `task_name`) and writing both. **Data-hygiene candidate.**
3. **Cross-schema duplicates.** `JURISDICTION_CODE_ASSIGNMENT` exists in BOTH `graph:OFSMDM:` and `graph:OFSERM:`. The OFSMDM copy's source body does not begin with `CREATE OR REPLACE FUNCTION` — looks like residue from an earlier load when the manifest pointed differently, or the loader stored a stripped/wrapped variant. Whichever it is, **Phase 1's "wipe Redis on migration" recommendation will clear this incidentally**; flagging in case Phase 1 chooses a non-wipe path.
4. **Hierarchy `TEST_BATCH_WITH_MANIFEST` lingering in Redis.** Visible in `hierarchy:*`. Test residue from `tests/unit/parsing/test_loader_manifest.py`. Will clear on next Redis wipe.
5. **372 OFSERM .sql files on disk vs 380 OFSERM graph keys in Redis.** Eight extra graph keys probably correspond to the duplication noted in (2) carried over to OFSERM as well. Worth a hash-vs-keys reconciliation in Phase 1.
6. **`rtie:logic:OFSMDM:<fn>` cache is independent of `graph:source:OFSMDM:<fn>`.** The loader writes raw source under `graph:source:`; `MetadataInterpreter.fetch_logic` ([metadata_interpreter.py:206](src/agents/metadata_interpreter.py#L206)) writes a different copy under `rtie:logic:` after Oracle/disk fallback. Two caches for the same data, populated by different code paths, with no cross-invalidation. **Phase 3 (source retrieval) should consider unifying these — either by having `fetch_logic` short-circuit on `graph:source:` cache hits, or by removing one of the two caches.**

---

## Section 3 — Source Retrieval Code Paths

The flow that fetches function source when answering a query, end-to-end:

### 3.1 Per-file map

#### [src/agents/orchestrator.py](src/agents/orchestrator.py)
- **Schema assumption:** multi-schema. `_PRECHECK_SCHEMAS = ("OFSMDM", "OFSERM")` is iterated by `function_exists_in_graph()` ([orchestrator.py:454-475](src/agents/orchestrator.py#L454-L475)) and `find_similar_function_names()` ([orchestrator.py:478-516](src/agents/orchestrator.py#L478-L516)).
- **Schema flow:** the LLM classifier returns `schema_name` ([orchestrator.py:75](src/agents/orchestrator.py#L75)) defaulting to `"OFSMDM"` per the system prompt. This is the *only* place schema is decided based on the query content.
- **Redis-key construction:** delegates to `get_function_graph()` from `src.parsing.store`. Iterates schemas and accepts the first hit.
- **Where schema would flow if parametric:** already there — this is the cleanest layer. The remaining cleanup is the LLM prompt's "default OFSMDM" hint (line 119, 194) which biases classification; should be replaced with a discovered-schema list.

#### [src/agents/metadata_interpreter.py](src/agents/metadata_interpreter.py)
- **Schema assumption:** single schema. Constructor takes `default_schema: str = "OFSMDM"` ([line 124](src/agents/metadata_interpreter.py#L124)).
- **Function under inspection:** `fetch_logic(state)` ([line 206-294](src/agents/metadata_interpreter.py#L206)). Signature accepts the entire `LogicState`; uses `state["schema"]` directly with no default. `fetch_multi_logic(state)` ([line 331-395](src/agents/metadata_interpreter.py#L331)) falls back to `self._default_schema` when state's schema is empty.
- **Schema flow:** state-driven. If the orchestrator stamps `state["schema"]` correctly, this layer is fine. The constructor default is the safety net that picks OFSMDM if nothing upstream set it.
- **Redis-key construction:** `await self._cache.get_json("logic", schema, object_name)` → resolved by `CacheClient` to `rtie:logic:<schema>:<object_name>`. **Schema is already a key segment.**
- **Note:** this path does NOT consult `graph:source:<schema>:<fn>` written by the loader. Two source caches coexist — see Section 2.5 issue #6.

#### [src/agents/logic_explainer.py](src/agents/logic_explainer.py)
- **Schema assumption:** state-driven with OFSMDM fallback. `_render_hierarchy_header()` ([line 530](src/agents/logic_explainer.py#L530)) reads `state["schema"]` else `"OFSMDM"`.
- **Schema flow:** consumes already-fetched source from `state["multi_source"]`. Calls `get_function_graph(redis, schema, fn_upper)` for hierarchy lookup.
- **Where schema would flow if parametric:** already parametric.

#### [src/agents/variable_tracer.py](src/agents/variable_tracer.py)
- **Schema assumption:** schema appears only in a negative-example LLM template at [line 156](src/agents/variable_tracer.py#L156). Behaviorally schema-agnostic; takes inputs from already-resolved graph nodes upstream.

#### [src/main.py](src/main.py)
- **Schema assumption:** single schema with OFSMDM fallback at four locations ([line 762, 785, 1174, 1315](src/main.py#L762)).
- **Schema flow:** SSE handler stamps `state["schema"] = state.get("schema") or "OFSMDM"` *after* vector search (line 762). This is a safety net AND a silent default — when the LLM omits `schema_name`, OFSMDM wins.
- **Where schema would flow if parametric:** would consume `state["schema"]` set by the classifier, falling through to a discovered-schema-set sentinel if none was inferable, rather than baking in OFSMDM.

#### [src/parsing/store.py](src/parsing/store.py)
- **Schema assumption:** **fully parametric.** Every reader/writer takes `schema: str` as the first argument after `redis_client`. Templates in `REDIS_KEYS` ([line 16-25](src/parsing/store.py#L16-L25)) all interpolate `{schema}`. **Phase 1 should not touch this file.**

### 3.2 The fetch path, step by step

For a query that resolves to a function name `FN`:
1. `OrchestratorAgent.classify()` sets `state["schema"]` from the LLM's `schema_name` (or, on failure, defaults via the rest of the pipeline).
2. SSE pipeline ([main.py:744-768](src/main.py#L744-L768)) calls vector search via `_vector_store.search()` (no schema filter today — see Section 4).
3. SSE stamps `state["schema"] = state.get("schema") or "OFSMDM"` ([main.py:762](src/main.py#L762)). **This is the critical hardcoded fallback.**
4. `MetadataInterpreter.fetch_multi_logic(state)` ([metadata_interpreter.py:331](src/agents/metadata_interpreter.py#L331)) iterates the search results. For each `fn_name`, builds a mini-state and calls `fetch_logic`.
5. `fetch_logic` ([metadata_interpreter.py:206](src/agents/metadata_interpreter.py#L206)) tries:
   1. Redis cache `rtie:logic:<schema>:<fn>` — populated only by prior fetches via this path
   2. Oracle `ALL_SOURCE` via `TMPL_FETCH_SOURCE` (parametric on `:schema`)
   3. Disk scan via `_scan_modules_for_file()` ([line 55-75](src/agents/metadata_interpreter.py#L55-L75)) — note: case-insensitive basename match, but **does not stamp the resulting schema back into state**. If the file lives in the OFSERM module directory but `state["schema"]` is OFSMDM, the result is cached under the wrong schema key.

### 3.3 Phase 1 implications

The hardcoded-fallback wedge that needs to move first is **`main.py:762`**. Once that line stops defaulting to OFSMDM and instead either:
- propagates whatever schema the classifier (or the vector-search hit) decided, or
- preserves `state["schema"]` as `None` and lets `fetch_logic` iterate `_PRECHECK_SCHEMAS`,

the rest of the request path becomes schema-agnostic with minimal further work, because `parsing/store.py` is already parametric.

---

## Section 4 — Vector Store and Semantic Search Inventory

### 4.1 Index definition

`FT.INFO idx:rtie_vectors`:
- **Index name:** `idx:rtie_vectors`
- **Key prefix:** `rtie:vec:` (HASH type)
- **Doc-key format:** `rtie:vec:<module>:<function_name>` — see [src/tools/vector_store.py:318-328](src/tools/vector_store.py#L318-L328)
- **Schema in document:** **NOT INDEXED.** The fields are `function_name` (TEXT), `module` (TAG), `description` (TEXT), `tables_read` (TEXT), `tables_written` (TEXT), `key_columns` (TEXT), `status` (TAG), `generated_at` (TEXT), `description_hash` (TEXT), `source_hash` (TEXT), `embedding` (VECTOR FLAT FLOAT32 1536-dim COSINE).
- **Documents:** 24 (= 12 unique funcs × 2 module aliases — `OFSDMINFO_ABL_DATA_PREPARATION` and `DATA_PREPARATION`)
- **Records:** 3,071 (term postings)
- **Memory:** 6.0 MB (vector index) + 0.16 MB (text/tag indexes)

### 4.2 Indexing scope

Driven by `embedding.auto_index_modules` in `config/settings.yaml` ([line 39-40](config/settings.yaml#L39-L40)):
```yaml
auto_index_modules:
  - "OFSDMINFO_ABL_DATA_PREPARATION"
```
Only one module is auto-indexed at startup. The OFSERM module `ABL_CAR_CSTM_V4` is NOT in this list, which is the proximate cause of "vector store is OFSMDM-only."

### 4.3 Search scope

`VectorStore.search()` ([vector_store.py:175-228](src/tools/vector_store.py#L175-L228)) supports a `module_filter` parameter. The SSE handler calls it without a filter ([main.py:760](src/main.py#L760)) — every query searches the entire (OFSMDM-only) corpus. There is no `schema` filter capability today; adding one requires a new TAG field in the index.

### 4.4 Description generation

`IndexerAgent._generate_description()` ([src/agents/indexer.py:171-175](src/agents/indexer.py#L171-L175)): per-function, the source body is sent to GPT-4o (see `DESCRIPTION_SYSTEM_PROMPT` at [indexer.py:34-59](src/agents/indexer.py#L34-L59)) which returns a structured `description / tables_read / tables_written / key_columns` JSON. The description, plus the embedded vector, is stored in the hash. Skipped if the source hash hasn't changed (incremental).

### 4.5 CS_Deferred_Tax embedding presence

Confirmed absent: `KEYS rtie:vec:*CS_DEFERRED_TAX*` returns empty. The 24 indexed docs are all OFSMDM functions. `idx:rtie_vectors` cannot match the user's question about `CS_Deferred_Tax_Asset_Net_of_DTL_Calculation` via semantic search; the only positive match is via exact-name lookup against `graph:OFSERM:*` (which the W37 pre-check does perform).

### 4.6 Phase 1 implications

Three discrete changes turn the vector store schema-aware:
1. Add `schema` TAG field to the `idx:rtie_vectors` definition (small change in [vector_store.py:87-107](src/tools/vector_store.py#L87-L107)).
2. Stamp `schema` on each upsert ([vector_store.py:155-167](src/tools/vector_store.py#L155-L167)) — the indexer knows the schema from the `CREATE OR REPLACE FUNCTION schema.name` prefix in source.
3. Re-run indexing for OFSERM modules (extend `auto_index_modules` to include `ABL_CAR_CSTM_V4`).

The index must be dropped and recreated to add a TAG field; an incremental schema mutation is not supported by RediSearch. Phase 1's "wipe + rebuild" approach handles this for free.

---

## Section 5 — Oracle Schema-Aware Queries

All Oracle SQL is centralized in [src/templates/sql_templates.yaml](src/templates/sql_templates.yaml). Every template that touches schema-scoped views uses a `:schema` bind parameter. **No template hardcodes OFSMDM.**

| Template | View(s) | Purpose | Current scope | Phase 2 need |
|----------|---------|---------|---------------|--------------|
| `TMPL_FETCH_SOURCE` | `all_source` | Fetch PL/SQL body for a single object | Caller-driven schema | Already parametric. Caller must pass OFSERM when needed. |
| `TMPL_OBJECT_EXISTS` | `all_objects` | Existence check + last_ddl_time | Caller-driven schema | Already parametric. |
| `TMPL_SCHEMA_SNAPSHOT` | `all_tab_columns` | Bulk column-type snapshot for SQLGuardian | Caller-driven schema | Already parametric, **but called only for `oracle_cfg["schema"]`** (single schema) at startup — see below. |
| `TMPL_BATCH_RUN_ID_LOOKUP` | `fsi_message_log` | Locate batch_run_id for a date | Schema-agnostic (single global table) | No change. |

**Call sites that pass a single hardcoded schema argument:**

| File | Line | Call | Phase 2 need |
|------|------|------|--------------|
| [src/main.py:406](src/main.py#L406) | startup | `_cache_manager.refresh_schema_snapshot(oracle_cfg["schema"])` | Must call once per discovered schema. Snapshot key family is already parametric (`rtie:schema:snapshot:<schema>` — see [data_query.py:837](src/agents/data_query.py#L837)). |
| [src/main.py:1644](src/main.py#L1644) | `/refresh-schema` admin endpoint | `_cache_manager.refresh_schema_snapshot(schema)` | Already accepts `schema` as a parameter. No change needed beyond exposing OFSERM as a valid argument. |
| [src/agents/data_query.py:624](src/agents/data_query.py#L624) | per-query | `load_column_types(self._redis, schema)` | Reads the snapshot for the query's schema. Already parametric — depends only on (1) being populated for OFSERM. |

**Bottom line for Phase 2:** the SQL layer is already parametric. The work is **wiring**: at startup, iterate the discovered schema set and call `refresh_schema_snapshot` for each, instead of just `oracle_cfg["schema"]`. Roughly a 5-line change in `main.py`.

---

## Section 6 — Cross-Schema Function References

Inputs:
- `db/modules/OFSDMINFO_ABL_DATA_PREPARATION/functions/*.sql` — 12 OFSMDM functions
- `db/modules/ABL_CAR_CSTM_V4/functions/*.sql` — 372 OFSERM functions

### 6.1 OFSMDM → OFSERM (qualified)

Pattern: `OFSERM\.` (case-insensitive) inside an `OFSMDM.*` function body.

| Source file (OFSMDM) | Target | Lines |
|----------------------|--------|-------|
| [db/modules/OFSDMINFO_ABL_DATA_PREPARATION/functions/FN_LOAD_OPS_RISK_DATA.sql](db/modules/OFSDMINFO_ABL_DATA_PREPARATION/functions/FN_LOAD_OPS_RISK_DATA.sql) | `OFSERM.VW_JURISDICTION_BR_MAP` | 154 (commented), 184 (commented), 258 (live), 293 (live) |

Only one OFSMDM function references OFSERM, and it's a single view (`VW_JURISDICTION_BR_MAP`). Two of the four hits are commented out, leaving **two live qualified cross-schema reads**.

### 6.2 OFSERM → OFSMDM (qualified)

`OFSMDM\.` inside an `OFSERM.*` function: **0 hits.** OFSERM never qualifies OFSMDM tables explicitly.

### 6.3 OFSERM → OFSMDM (implicit, unqualified)

OFSERM functions DO read OFSMDM staging/operational tables — they just don't qualify them. Quick scan:

- `STG_*` tables (the canonical OFSMDM staging prefix): referenced in **59 OFSERM function files** out of 372.
- `STG_GL_DATA` specifically: 2 OFSERM files.

These reads are unqualified — Oracle resolves them via grants or SQL*Plus session context. From a graph-edge perspective they appear as "reads from `STG_*`" without schema annotation. Phase 1's edge-tracking design must decide how to attribute these:
- Option A: every `STG_*` reference is implicitly OFSMDM (matches OFSAA convention).
- Option B: store unqualified table refs as `(schema=null, table=STG_…)` and let consumers join by table-name catalog.

### 6.4 Phase 1 implications

Cross-schema references are **NOT rare**: there is one pervasive pattern (OFSERM functions reading OFSMDM staging tables, unqualified, ~16% of OFSERM functions touch `STG_*`). Edge-tracking must cope with unqualified references. Explicit qualified cross-schema references are essentially a one-off (1 OFSMDM file → 1 OFSERM view). The simpler edge model — annotate tables with a schema only when the source qualifies it, otherwise leave it unqualified — fits the data.

---

## Index of citations

- Source: `src/parsing/store.py`, `src/parsing/manifest.py`, `src/parsing/loader.py`, `src/parsing/parser.py`, `src/parsing/builder.py`, `src/parsing/indexer.py`, `src/parsing/query_engine.py`
- Agents: `src/agents/orchestrator.py`, `src/agents/metadata_interpreter.py`, `src/agents/logic_explainer.py`, `src/agents/variable_tracer.py`, `src/agents/data_query.py`, `src/agents/cache_manager.py`, `src/agents/indexer.py`, `src/agents/validator.py`
- Tools: `src/tools/vector_store.py`, `src/tools/sql_guardian.py`, `src/tools/cache_tools.py`, `src/tools/schema_tools.py`
- Pipeline: `src/main.py`, `src/pipeline/state.py`, `src/pipeline/logic_graph.py`
- Templates: `src/templates/sql_templates.yaml`
- Config: `config/settings.yaml`
- Data: `db/modules/OFSDMINFO_ABL_DATA_PREPARATION/`, `db/modules/ABL_CAR_CSTM_V4/`
- Redis state captured 2026-04-27 from container `rtie-redis`
