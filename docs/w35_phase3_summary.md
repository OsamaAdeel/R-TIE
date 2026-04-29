# W35 Phase 3 — Schema-Aware Source Retrieval and Vector Store Extension

**Branch:** `refactor/w35-phase3-source-retrieval-vector`
**Closes:** structural completion of OFSERM function-name retrievability;
partial close of W49 (will fire less for OFSERM); first user-visible
OFSERM coverage.

Phase 1 made schema first-class. Phase 2 populated per-schema origins
and Oracle catalog awareness. Phase 3 closes the gap users feel:
source retrieval and semantic search now reach into OFSERM keyspace.

This is the **first user-visible OFSERM improvement** in W35. Phases
0/0.5/1/2 were foundation. After Phase 3, asking "how does
`CS_Deferred_Tax_Asset_Net_of_DTL_Calculation` work?" produces a
VERIFIED step-by-step explanation rather than a W49 "source not
indexed" structured response.

## What changed

### Source retrieval (Step 1)

**Before.** `MetadataInterpreter.fetch_logic` consulted three caches:
`rtie:logic:<schema>:<fn>` → Oracle `ALL_SOURCE` → disk scan. The
loader-managed `graph:source:<schema>:<fn>` cache (where every loaded
function's body lives) was never read. `fetch_multi_logic` used a
single `state["schema"]` for every result in the batch, even when the
result name was an OFSERM function while the request was routed to
OFSMDM.

**After.** `MetadataInterpreter` now accepts a graph Redis client via
`set_graph_redis_client` (mirrors the existing
`LogicExplainer.set_redis_client` pattern — the constructor can't take
it because `_graph_redis` is created later in the lifespan). The
fetch chain becomes:

1. `graph:source:<schema>:<fn>` — loader-owned, populated for every
   loaded function (NEW)
2. `rtie:logic:<schema>:<fn>` — async cache, populated by Oracle/disk
   fallbacks (preserved for `version_hash` semantics)
3. Oracle `ALL_SOURCE`
4. `db/modules/` disk scan

`fetch_multi_logic` resolves the actual owning schema per result via
`schema_for_function(name, redis)` before running the chain. The
discovered-schema list is snapshotted once per call to avoid
re-scanning `graph:*` per result. Each `multi_source[fn_name]` entry
now carries a `schema` field recording which schema served the body
(useful for downstream hierarchy headers and Phase 4 routing logic).

**Cache rationalization decision.** Kept both caches.
`graph:source:` is now the read-through primary; `rtie:logic:` is the
write-target for Oracle/disk fallbacks (the existing
`/refresh-cache` and `/cache-status` admin paths still use it for
`version_hash` and `cached_at`). A full unification (e.g. delete
`rtie:logic:`, point `fetch_logic` at `graph:source:` for the cache
path too) is deferred to Phase 8 so Phase 3 can keep the diff small.

### Vector store schema TAG (Steps 2–4)

**Before.** Index `idx:rtie_vectors` had no schema field. Doc keys
were `rtie:vec:<module>:<fn>` (module is the loader's batch name —
happens to map 1-to-1 to schema today, but coincidentally). Only
`OFSDMINFO_ABL_DATA_PREPARATION` (the OFSMDM module) was in
`auto_index_modules`, so OFSERM functions had no embeddings and
semantic search never surfaced them.

**After.**

- **Doc-key prefix:** `rtie:vec:<schema>:<fn>` (e.g.
  `rtie:vec:OFSERM:CS_DEFERRED_TAX_ASSET_NET_OF_DTL_CALCULATION`).
  The prefix is the only RediSearch-tracked thing that pre-Phase-3
  produced doc-key collisions across schemas.
- **Schema TAG field:** `schema` is now a first-class TAG, queryable
  as `@schema:{OFSERM}` and combinable with the existing `module`
  TAG via space-separated AND. `VectorStore.search` accepts an
  optional `schema_filter` kwarg; default `None` searches every
  schema (preserves the pre-Phase-3 multi-schema-blind behaviour).
- **Index drop+recreate:** `ensure_index` introspects the existing
  index via `FT.INFO`. If the index is missing the `schema` attribute
  (i.e. pre-Phase-3 shape), it calls `dropindex(delete_documents=True)`
  to clear stale docs and recreates with the new schema. This is
  automatic at startup — no manual CLI call needed.
- **Indexer rewiring:** A new `IndexerAgent.index_all_loaded` method
  iterates `discovered_schemas() → graph:<schema>:<fn>` keys and
  pulls source from `graph:source:<schema>:<fn>`. This replaces the
  `auto_index_modules` disk-walk loop at startup and naturally
  honours the manifest's active/inactive filter. The legacy
  `index_module(module_name)` admin path stays for `/index-module`
  but now derives schema per-function from the source body's
  `CREATE OR REPLACE FUNCTION schema.name` prefix.

`config/settings.yaml` adds `ABL_CAR_CSTM_V4` to `auto_index_modules`.
With Phase 3's startup wiring iterating Redis directly, the list is
informational at startup — entries are still authoritative for the
legacy `/index-module` admin path.

### Hierarchy header — latent bug surfaced by Phase 3

`LogicExplainer.hierarchy_header` ranked ``multi_source`` entries
with ``reverse=True`` over the COSINE distance score, which picks the
**worst** match rather than the best. Pre-Phase-3 the index only held
12 OFSMDM functions, so the worst match was still an OFSMDM function
and the per-schema graph lookup happened to succeed. Phase 3 added
141 OFSERM functions, so the worst match is now reliably an OFSERM
function whose hierarchy is looked up under OFSMDM (the fallback
schema) and missed — silently producing an empty header.

The fix is two lines: rank ASC (lowest score = closest match) and
prefer the per-entry ``schema`` field that Phase 3's
`MetadataInterpreter.fetch_multi_logic` now stamps onto each
``multi_source`` value. Falls through to ``state["schema"]`` when the
entry doesn't carry one (older paths). Without this, Phase 3's
canary (d) GATE — "Hierarchy header: `ABL_CAR_CSTM_V4 → ...`" —
would not be satisfied.

## Expected post-restart Redis state

Phase 1 baseline (regression):

| Key | Bytes |
|-----|-------|
| `graph:index:OFSERM` | ~284,629 |
| `graph:index:OFSMDM` | ~40,249 |

Phase 2 baseline (regression):

| Pattern | Count |
|---------|-------|
| `graph:origins:OFSMDM:*` | 5 |
| `graph:origins:OFSERM:*` | 5 |

Phase 3 new state:

| Probe | Expected |
|-------|----------|
| `FT.INFO idx:rtie_vectors` | TAG attribute named `schema` present |
| `FT.SEARCH idx:rtie_vectors "*" LIMIT 0 0` | ~153 docs (12 OFSMDM + 141 OFSERM) |
| `FT.SEARCH idx:rtie_vectors "@schema:{OFSERM}" LIMIT 0 1` | ≥ 1 result |
| `FT.SEARCH idx:rtie_vectors "@schema:{OFSMDM}" LIMIT 0 1` | ≥ 1 result |

## Out of scope (deferred)

Per the Phase 3 prompt, the following are explicitly Phase 4+ work:

- Orchestrator routing changes (Phase 4)
- Business identifier indexing — `graph:literal:<schema>:<id>` (Phase 5)
- Expression extraction for derivations (Phase 6)
- W49 / W45 detector logic (untouched — only their inputs change)
- Description-generation prompt or model
- `rtie:logic:` vs `graph:source:` full unification (Phase 8 cleanup)

## Testing

- 5 tests in `tests/unit/agents/test_phase3_source_retrieval.py` —
  loader-cache short-circuit, fall-through to rtie:logic chain, no
  graph_redis client preserves Phase 1 chain, per-function schema
  resolution in `fetch_multi_logic`, fall-back to request schema
  when resolution fails.
- 6 tests in `tests/unit/tools/test_phase3_vector_store.py` —
  doc-key prefix, filter clause builder for None/schema-only/
  module-only/combined cases, schema-field constant.
- The existing Phase 1+2 suites (origins, multi-schema catalog,
  CHAR padding, etc.) continue to pass.
- One pre-existing failure remains
  (`tests/unit/parsing/test_query_engine.py::test_assemble_llm_payload_structure`) —
  reproduces on `main` without any Phase 3 changes; left untouched.

## User-visible canary

The Phase 3 new canary is:

> "How does CS_Deferred_Tax_Asset_Net_of_DTL_Calculation work?"

Expected Phase 3 response:

- VERIFIED badge
- Hierarchy header citing `ABL_CAR_CSTM_V4` → `ABL_CAPITAL_STRUCTURE_DATA_POPULATION` → …
- MERGE statement cited with line numbers
- The `CAP943 = CAP309 - CAP863` derivation visible in the explanation
- No `PARTIAL_SOURCE_INDEXED` warning
- No `UNGROUNDED_IDENTIFIERS` warning

Pre-Phase-3 the same query produced the W49 "Source Not Currently
Indexed" structured response.
