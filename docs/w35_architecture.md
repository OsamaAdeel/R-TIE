# W35 Phase 0 — Multi-Schema Architecture Proposal

**Branch:** `diagnostic/w35-architecture`
**Companion to:** [w35_diagnostic.md](w35_diagnostic.md)
**Captured:** 2026-04-27

This document proposes the architecture for Phases 1–8 of the production-ready OFSERM coverage work. Every decision is grounded in a finding from `w35_diagnostic.md`.

> **Caveat about the production plan input.** The Phase-0 prompt asked me to read a production plan at `/mnt/user-data/outputs/RTIE_OFSERM_Production_Plan.md` (or the user's local copy) and confirm/revise its 8 architectural decisions. **I could not locate that file** — it is not at the cited path, not under the project tree, not under `.claude/`, `docs/`, or `scratch/`. I have therefore proceeded with the concrete A–G design proposals (which the prompt also requires and which contain the substance), and recorded the missing-input as Open Question Q1. If the user provides the plan, this document will need a Section 0 ("8 decisions: confirm/revise") added before Phase 1 starts.

---

## A. SchemaAwareKeyspace helper API

**Why we need it.** Today, every Phase-1 caller will write `f"graph:{schema}:{fn}"` style strings. `src/parsing/store.py` already centralises this for the loader's keys, but the request-path layer (`main.py`, `metadata_interpreter.py`, `logic_explainer.py`, the vector store) constructs its own keys ad-hoc. Each ad-hoc construction is a place a future engineer can forget to substitute the schema. A small helper module makes "forget the schema" a type error.

**Proposed module:** `src/tools/keyspace.py` — a 50-line module with no business logic, only key construction and an explicit schema parameter on every function. (Reusing the existing `src/parsing/store.py:_key()` pattern, but extending it to cover the request-path key families that today live outside `parsing/`.)

```python
# src/tools/keyspace.py
from typing import Final

# Existing loader-owned key families (mirror of src/parsing/store.py:REDIS_KEYS)
def graph_function(schema: str, fn: str) -> str:           # "graph:OFSERM:CS_..."
def graph_meta(schema: str, fn: str) -> str:               # "graph:meta:OFSERM:CS_..."
def graph_source(schema: str, fn: str) -> str:             # "graph:source:OFSERM:CS_..."
def graph_full(schema: str) -> str:                        # "graph:full:OFSERM"
def graph_index(schema: str) -> str:                       # "graph:index:OFSERM"
def graph_aliases(schema: str) -> str:                     # "graph:aliases:OFSERM"

# Request-path key families (currently constructed via CacheClient.set_json)
def logic_cache(schema: str, fn: str) -> str:              # "rtie:logic:OFSERM:CS_..."
def schema_snapshot(schema: str) -> str:                   # "rtie:schema:snapshot:OFSERM"

# Vector store
def vector_doc(schema: str, fn: str) -> str:               # "rtie:vec:OFSERM:CS_..." (see decision E for rename rationale)

# Module hierarchy (schema-agnostic)
def hierarchy(batch: str) -> str:                          # "hierarchy:ABL_CAR_CSTM_V4"
HIERARCHY_BATCHES: Final[str] = "hierarchy:batches"
```

**Schema is always explicit.** The helper takes `schema: str` — never `Optional[str]`, never with a default. The "what schema is this?" decision is forced upstream into the orchestrator/loader, where it can be made deliberately. There is no "fall back to OFSMDM" inside the helper.

**Schema validation.** Each function asserts `schema in get_known_schemas(redis_client)` (cheap Redis SMEMBERS — see decision B) on first call per request, cached on the request context. A bad schema raises `UnknownSchemaError` instead of silently writing to a Redis key that no reader will ever look at.

**Migration path.** Existing call sites that already use `src/parsing/store.py:_key()` continue to work — the helper is additive. Phase 1's surgery is to convert the **8 HARDCODED_DEFAULT call sites** (Section 1 of the diagnostic) to use this helper, with `schema` passed as an argument from the call stack rather than defaulted.

**What we are NOT building.** A general-purpose Redis ORM. The helper has no read/write methods — only key construction. Reads/writes still go through `redis_client.get/set` or the existing `src/parsing/store.py` helpers.

---

## B. Schema discovery mechanism

**Why we need it.** The current code knows about two schemas because they're hardcoded in `RECOGNIZED_SCHEMAS = {"OFSMDM", "OFSERM"}` ([manifest.py:36](src/parsing/manifest.py#L36)) and `_PRECHECK_SCHEMAS = ("OFSMDM", "OFSERM")` ([orchestrator.py:54](src/agents/orchestrator.py#L54)). Adding a third schema (e.g. `OFSCAP`) today requires editing both places. We want the schema set to be **derivable from the loaded modules** so adding a schema is a config/manifest change, not a code change.

**Authoritative source: the manifest.** Every module ships `manifest.yaml` with a top-level `schema:` field. The set of schemas is the union of those values. The diagnostic confirmed two manifests: `OFSDMINFO_ABL_DATA_PREPARATION` declares `schema: OFSMDM`; `ABL_CAR_CSTM_V4` declares `schema: OFSERM`. This is the canonical input.

**Cache, don't recompute.** At loader-completion, write the discovered set to Redis:

```
SADD known_schemas OFSMDM OFSERM
```

(SET, not msgpack — it's tiny and queryable from any layer.)

**Read API:**
```python
# src/tools/schema_discovery.py
def get_known_schemas(redis_client) -> frozenset[str]: ...
def is_known_schema(redis_client, schema: str) -> bool: ...
```

**Why not derive at every call site?** Two reasons:
1. Discovery requires a Redis SCAN over `graph:*` or a manifest re-walk — both are O(modules), and we'd hit them on every request.
2. The set is updated only when the loader runs. Treating it as a persistent fact, not a runtime computation, matches its actual lifecycle.

**Bootstrapping:** if `known_schemas` is missing (clean Redis), `get_known_schemas` falls back to `RECOGNIZED_SCHEMAS` from `src/parsing/manifest.py`. That keeps the constant useful as a static-analysis hint and a test fixture even after Phase 1; it stops being load-bearing for behavior.

**Replaces:** the hardcoded `_PRECHECK_SCHEMAS` tuple in `orchestrator.py:54`. The orchestrator's pre-check loop iterates `get_known_schemas(redis_client)` instead.

---

## C. Redis key naming conventions

The diagnostic confirmed the existing key layout already includes `<schema>` as a path segment for every loader-owned family. Phase 1 should not break compatibility. The conventions below are the existing patterns, made explicit:

| Family | Pattern | Example | Owner |
|--------|---------|---------|-------|
| Per-function graph | `graph:<schema>:<FN_UPPER>` | `graph:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION` | loader |
| Per-function meta | `graph:meta:<schema>:<FN_UPPER>` | `graph:meta:OFSERM:...` | loader |
| Per-function raw source | `graph:source:<schema>:<FN_UPPER>` | `graph:source:OFSERM:...` | loader |
| Aggregated cross-function graph | `graph:full:<schema>` | `graph:full:OFSERM` | loader |
| Column → function index | `graph:index:<schema>` | `graph:index:OFSERM` | loader |
| Alias map | `graph:aliases:<schema>` | `graph:aliases:OFSERM` | loader |
| Source cache (request path) | `rtie:logic:<schema>:<FN_UPPER>` | `rtie:logic:OFSERM:...` | metadata_interpreter |
| Schema-type snapshot | `rtie:schema:snapshot:<schema>` | `rtie:schema:snapshot:OFSERM` | cache_manager |
| Module hierarchy | `hierarchy:<batch_name>` | `hierarchy:ABL_CAR_CSTM_V4` | loader |
| Batch enumeration set | `hierarchy:batches` | (set members) | loader |
| Discovered schemas set | `known_schemas` | (set members) | **NEW** — loader |
| Vector embedding doc | `rtie:vec:<schema>:<FN_UPPER>` | `rtie:vec:OFSERM:CS_...` | **CHANGED** — see decision D |
| Business-identifier index (literal → fn) | `graph:literal:<schema>:<IDENTIFIER_UPPER>` | `graph:literal:OFSERM:CAP943` | **NEW** — see decision E |

**Rules (apply uniformly):**
1. Function names are **uppercased** at the key boundary. The on-disk file `CS_Deferred_Tax_Asset_Net_of_DTL_Calculation.sql` produces key `CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION`. Every reader must `.upper()` the search term — the convention is established in `function_exists_in_graph()` ([orchestrator.py:468](src/agents/orchestrator.py#L468)).
2. Schema names are **uppercased** to match Oracle ALL_OBJECTS conventions and the loader's `_extract_schema_from_source` ([loader.py:83](src/parsing/loader.py#L83)).
3. **Don't introduce new key families without adding a constructor to `src/tools/keyspace.py`** (decision A). Keyspace drift is what got us here.

---

## D. Vector store schema metadata field

**Why we need it.** The diagnostic confirmed `idx:rtie_vectors` has no schema field — only `module` (TAG). `module` happens to map 1-to-1 to schema today (`OFSDMINFO_ABL_DATA_PREPARATION` ↔ OFSMDM), but it's a coincidence. Phase 1 needs `schema` as a first-class filter so a query can be scoped to OFSERM (`@schema:{OFSERM}`) without enumerating module aliases.

**Proposed field addition (`src/tools/vector_store.py:87-107`):**
```python
schema = (
    TextField("function_name"),
    TagField("schema"),       # NEW
    TagField("module"),
    TextField("description"),
    ...
)
```
And a corresponding upsert key in the `mapping` dict ([vector_store.py:155-167](src/tools/vector_store.py#L155-L167)):
```python
b"schema": schema_str.encode(),  # NEW; passed in from indexer
```

**Filter syntax.** RediSearch TAG fields are queryable as `@schema:{OFSERM}` and combinable: `@schema:{OFSERM} =>[KNN ...]`. The existing `module_filter` parameter on `search()` becomes a more general `filters: dict[str, str]` taking `{"schema": "OFSERM"}` and/or `{"module": "ABL_CAR_CSTM_V4"}`.

**Doc-key change.** Today: `rtie:vec:<module>:<fn>`. Proposed: `rtie:vec:<schema>:<fn>`. **This is a key rename** — the index must be dropped and recreated to take the new prefix and the new field. Fits the wipe-and-rebuild migration (decision F).

**Upsert plumbing.** `IndexerAgent` already knows the schema — it walks files under `db/modules/<MODULE>/functions/` and the manifest declares `schema:`. Plumbing it through `_scan_module_functions` → `upsert_function` is a 3-call-site change.

**Re-indexing scope.** Phase 1 must re-index the OFSMDM module (12 functions) under the new doc-key shape AND newly index the OFSERM module (372 functions) — incremental indexing keys off `source_hash`, but the doc key itself is changing, so all 12 OFSMDM docs must be deleted before re-indexing. The `auto_index_modules` config grows from `[OFSDMINFO_ABL_DATA_PREPARATION]` to include `ABL_CAR_CSTM_V4`.

---

## E. Business identifier pattern config

**Why we need it.** The user's claim that `CAP943 = MAX(CAP309) - MAX(CAP863)` is a *grounding* example: when the user asks "where does CAP943 come from?", we want to find `CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION` directly via the literal `CAP943`, not via fuzzy semantic search over function descriptions. The diagnostic confirmed the source body for that function, retrieved from `graph:source:OFSERM:CS_...`, contains all three CAP literals as bare strings — they are extractable.

**Proposed: a literal → function index, populated at parse time.**

```
Key: graph:literal:<schema>:<IDENTIFIER>
Value: msgpack list of function names
Example:
  graph:literal:OFSERM:CAP943
    -> ["CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION", ...]
  graph:literal:OFSERM:CAP309
    -> ["CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION", ...]
  graph:literal:OFSERM:CAP863
    -> ["CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION", ...]
```

**Pattern config.** The patterns to look for in source bodies live in `config/settings.yaml` so additions are config-only:

```yaml
business_identifiers:
  patterns:
    # Default
    - name: capital_account_head
      regex: '\bCAP\d+\b'
      description: "OFSAA capital adequacy account head IDs"
    # Suggested additions to be confirmed during Phase 1:
    - name: gl_code
      regex: '\bGL\d{4,}\b'
    - name: lob_code
      regex: '\bLV[A-Z]{2,4}\b'
```

**Why config, not constants.** OFSAA deployments at different banks introduce site-specific identifier conventions. We want adding a new convention to be a config change with a Redis-rebuild, not a code change with a deploy.

**Where the index is built.** In `src/parsing/builder.py` (or a new sibling `src/parsing/literal_indexer.py`). The literal extractor runs over the raw source lines (`graph:source:<schema>:<fn>` payload) for each function and writes one entry per (literal, function) pair. This is loader-time work; the builder already has a per-function loop available.

**Where it's read.** In the orchestrator's pre-check, parallel to `function_exists_in_graph`. If the user query contains a token matching any business-identifier regex, look up `graph:literal:<schema>:<token>` for each known schema. If a hit, the candidate functions go straight into the LLM context with no semantic search needed.

**Why not just re-use `graph:index:<schema>` (the column index)?** That index is built from `nodes[].column_maps` and breaks when `nodes` is empty (diagnostic Section 2.5 issue #1 — most OFSERM nodes are mis-classified as `commented_out_nodes`). The literal index runs over raw source and is robust to that bug. The two indexes overlap in coverage but answer different questions: column index = "what function writes this column?", literal index = "what function mentions this token?".

---

## F. Migration approach

**Recommendation: wipe Redis and rebuild on the next backend startup.** Concrete steps:

1. Operator runs `docker compose down redis && docker volume rm <redis_volume>` (or `redis-cli FLUSHDB` in dev).
2. On startup, `src/parsing/loader.py` re-discovers modules → re-parses every .sql → writes the full keyspace under the new conventions (decisions A–E).
3. `src/agents/indexer.py` re-builds the vector store under the new doc-key shape with the new schema field (decision D).
4. `src/agents/cache_manager.refresh_schema_snapshot()` is called once per discovered schema, populating `rtie:schema:snapshot:<schema>` (decision B + diagnostic Section 5).

**Why wipe is safe.** Redis is purely a cache in this system. The authoritative inputs are:
- `db/modules/<module>/functions/*.sql` — versioned in git
- `config/settings.yaml` and `db/modules/<module>/manifest.yaml` — versioned in git
- Oracle `ALL_SOURCE` / `ALL_TAB_COLUMNS` — recoverable from the database

There is no Redis-only data. The `rtie:logic:<schema>:<fn>` cache is rebuilt on first read; the vector embeddings are regenerated by the indexer (paid cost: ~$0.50–1.00 of OpenAI embedding API calls for 12+372 functions × ~1.5K-token descriptions, plus per-function description-generation LLM calls).

**Why not in-place migration?** Three forcing constraints:
1. The vector store doc-key prefix changes (`rtie:vec:<module>:` → `rtie:vec:<schema>:`). RediSearch indexes do not support changing the prefix. Drop + recreate is required.
2. The vector store gains a new TAG field (`schema`). RediSearch does not support adding fields to an existing index.
3. The diagnostic flagged data-hygiene issues (Section 2.5 #2, #3 — duplicate keys, cross-schema duplicates) that wipe-and-rebuild incidentally resolves.

**Persistent state that is NOT wiped:**
- `db/modules/` source files
- Configuration (`config/`, manifest YAMLs)
- Oracle DB
- Postgres (out-of-scope — currently used for chat history; not affected by Phase 1)

**Rollback strategy.** Keep `diagnostic/w35-architecture` and the per-phase branches reviewable before merge. If Phase N introduces a regression:
1. Revert the offending PR.
2. `FLUSHDB`.
3. Restart backend on the prior branch — the loader rebuilds Redis from on-disk inputs.

The wipe is reversible in the sense that any state the loader can produce, the loader can produce again.

---

## G. Open questions

### Q1 — Production plan input is missing (BLOCKING for "8 decisions" reconciliation)

- **Question:** the prompt referenced `RTIE_OFSERM_Production_Plan.md` containing 8 architectural decisions to confirm/revise. The file is not on disk.
- **What we know:** the prompt's A–G concrete proposals (covered above) and the inferable decisions one-by-one from the diagnostic.
- **What we don't know:** the exact text of those 8 decisions, what the plan's reasoning is, and whether any of A–G above conflicts with a decision the plan proposes.
- **Investigation:** ask the user to point at the plan, or to confirm A–G as the working architecture if the plan is no longer authoritative.

### Q2 — OFSERM nodes-vs-commented_out_nodes parser bug (BLOCKING for Phase 1 trace coverage)

- **Question:** why does the parser/builder classify the OFSERM CS_DEFERRED_TAX MERGE (and most other OFSERM merges, by size distribution) as `commented_out_nodes`?
- **What we know:** the source body in Redis is intact and contains the live MERGE. The builder writes `committed_after = false` and routes the node to `commented_out_nodes`. OFSMDM functions classify the same kind of MERGE correctly.
- **What we don't know:** the exact branch of `src/parsing/builder.py` (or `src/parsing/parser.py`) that decides `committed_after`. Likely culprits are OFSAA's `IF P_V_RUN_EXECUTION_ID...` wrapper or `BEGIN ... END` framing differences between the two source corpora.
- **Investigation:** Phase 1 must trace `committed_after` logic and run it against `CS_Deferred_Tax_Asset_Net_of_DTL_Calculation.sql` with a debugger; the fix is likely a small parser tweak. **Until this is fixed, no amount of multi-schema plumbing will produce traceable OFSERM edges** — the index will be empty and W47 will reappear after the rebuild.

### Q3 — Cross-schema unqualified table reads (DESIGN, Phase 1)

- **Question:** when an OFSERM function reads `STG_GL_DATA` unqualified, do edges in `graph:full:OFSERM` annotate that table with `schema=OFSMDM`, leave it unqualified, or do both?
- **What we know:** 59 OFSERM functions reference `STG_*` tables unqualified; only 1 OFSMDM function references OFSERM qualified.
- **What we don't know:** whether the user wants traces to surface "this OFSERM function reads from OFSMDM staging" as an explicit cross-schema hop, or treat the read as opaque.
- **Investigation:** small UX decision; recommend Option A from diagnostic Section 6.4 (annotate `STG_*` as OFSMDM by convention) since it gives the user better signal at almost no cost.

### Q4 — Is `rtie:logic:<schema>:<fn>` worth keeping? (CLEANUP, Phase 3)

- **Question:** should `MetadataInterpreter.fetch_logic` short-circuit on `graph:source:<schema>:<fn>` (loader-owned), or keep the parallel `rtie:logic:<schema>:<fn>` cache?
- **What we know:** Two caches for the same data, written by independent code paths, with no cross-invalidation. `rtie:logic:` was useful pre-loader (it could fall back to Oracle `ALL_SOURCE` for objects the loader hadn't parsed), but the loader's coverage is now broad enough that this is rare.
- **What we don't know:** whether any code path actually relies on the Oracle-fallback branch. A grep + a week of metrics could answer it.
- **Investigation:** Phase 3 — measure before deleting.

### Q5 — Function-name space variants (DATA, low priority)

- **Question:** should `BASEL III...` (with spaces) and `BASEL_III...` (with underscores) be treated as the same function, deduplicated, or one of the two dropped?
- **What we know:** both keys exist; both currently point at content. Likely the loader is reading from two sources (file basename → underscored; manifest task name → spaced) and not normalizing.
- **What we don't know:** which is the canonical surface (probably file-basename → underscored) and whether anything queries by spaced form.
- **Investigation:** Phase 1 hygiene — normalize at the loader (force `re.sub(r'\s+', '_', name).upper()`), wipe duplicates on next load.

---

## Summary of changes proposed across Phases 1–8

| Phase | Touches | Estimated surgery |
|-------|---------|-------------------|
| 1 — Schema parametricity | `main.py`, `metadata_interpreter.py`, `logic_explainer.py`, `pipeline/logic_graph.py`, `orchestrator.py` (LLM prompt), new `src/tools/keyspace.py`, new `src/tools/schema_discovery.py` | ~8 hardcoded-default sites become parametric. Net +2 small modules. |
| 2 — Schema-aware Oracle queries | `main.py:406` startup loop, `cache_manager.refresh_schema_snapshot` (no signature change, just iterate schemas) | ~5 lines |
| 3 — Source retrieval rationalization | `metadata_interpreter.fetch_logic` — decide Q4 outcome | depends on Q4 |
| 4 — Vector store schema field | `tools/vector_store.py` (index def + upsert + search filters), `agents/indexer.py` (pass schema), `config/settings.yaml` (`auto_index_modules`) | ~30 lines + RediSearch index drop/recreate |
| 5 — Business identifier index | `parsing/builder.py` (or new `parsing/literal_indexer.py`), `config/settings.yaml`, orchestrator pre-check | new module + ~20 lines wiring |
| 6 — Parser bug for OFSERM commented_out_nodes | `parsing/builder.py` (or `parser.py`) — Q2 | unknown until Q2 traced |
| 7 — Data hygiene (Q5) | `parsing/loader.py` (function-name normalization) | ~5 lines |
| 8 — Validation, integration tests, end-to-end CS_Deferred_Tax query | `tests/integration/`, new fixtures | net new test coverage; no production changes |

The total source diff is small. The discipline is in the order: Q2 (parser fix) gates everything else, because without it OFSERM has no edges to trace and Phase 1's plumbing wires up an empty graph.
