# W35 Phase 2 — Per-Schema Origins Catalog and Catalog Coverage

**Branch:** `refactor/w35-phase2-per-schema-indexes`
**Closes:** structural completion of W47-adjacent work; foundation for Phase 3
**Companion to:** [w35_diagnostic.md](w35_diagnostic.md), [w35_architecture.md](w35_architecture.md)

Phase 1 made `schema` a first-class parameter throughout the request and
loader paths via `SchemaAwareKeyspace` and `schema_discovery`. The Redis
key construction is schema-aware everywhere; the data behind those keys
was still OFSMDM-only.

Phase 2 fills in the data behind the schema-aware keys.

## What changed

### Origins catalog (Step 1)

**Before.** A single module-level `_catalog: OriginsCatalog | None` was
populated by `build_catalog(redis, schema=oracle_cfg["schema"])` at
startup. Reader functions (`classify_origin`, `is_gl_blocked`,
`get_eop_override`) consulted that single instance. OFSERM had no
catalog presence.

**After.** A per-schema registry `_catalogs: dict[str, OriginsCatalog]`
replaces the singleton. `build_catalog(redis)` (no schema arg) iterates
`schema_discovery.discovered_schemas()` and builds one catalog per
discovered schema. Per-schema build failures are logged and isolated —
an OFSERM outage does not blow away a working OFSMDM catalog.

Reader functions now accept an optional `schema` parameter. With it,
they scope to that schema. Without it, they iterate every built catalog
in insertion order — preserves the historical single-schema behaviour
for callers that don't yet thread a schema through (`value_tracer`,
`origin_classifier`).

**Redis snapshot.** Each successfully built catalog is also persisted
under `graph:origins:<schema>:<facet>` keys — `plsql`, `etl`,
`gl_blocked`, `eop_overrides`, `meta` — via `OriginsCatalog.to_redis`.
The in-memory registry is the source of truth; the keys are an
observability snapshot. Persistence failure is logged but does not
invalidate the in-memory catalog.

`SchemaAwareKeyspace.origins_key` (introduced in Phase 1) is the only
constructor for these keys. The frozen Phase-1 layout
(`graph:origins:<schema>[:<part>...]`) is the canonical pattern; an
operator running `redis-cli --scan --pattern "graph:origins:OFSMDM:*"`
or `--pattern "graph:origins:OFSERM:*"` will see five keys per schema
after a fresh load.

> **Note on the Phase-2 GATE pattern.** The Phase-2 prompt phrased the
> verification as `--scan --pattern "origins:OFSMDM:*"`. That glob does
> not match `graph:origins:OFSMDM:plsql` (no leading `graph:`). Use
> `--scan --pattern "graph:origins:OFSMDM:*"` (or `*origins:*`) to honour
> the frozen Phase-1 namespace.

### Oracle queries (Step 2)

The Oracle SQL templates already carry every schema-scoped predicate
through a `:schema` bind variable (Phase 0 Section 5 confirmed no
template hardcodes OFSMDM); the work was at the call sites.

- **`src/main.py:406`** — startup `refresh_schema_snapshot` was called
  once for `oracle_cfg["schema"]`. It now iterates
  `discovered_schemas(_graph_redis)`, priming
  `rtie:schema:snapshot:<schema>` per schema. Per-schema failures are
  logged but never abort the loop.
- **`src/main.py:347`** — `build_catalog` no longer takes
  `oracle_cfg["schema"]`; the multi-schema form (`build_catalog(redis)`)
  iterates every discovered schema.
- **`src/main.py:1691`** — the `/refresh-schema` admin command accepts
  an optional schema argument. Without one it refreshes every
  discovered schema; with one it preserves the historical single-schema
  response shape so existing tooling that pipes its output keeps
  working.

The bind-list IN-clause approach mentioned in the Phase-2 prompt is not
needed once `refresh_schema_snapshot` is called per schema — each
invocation already binds `:schema` and writes to a per-schema Redis
key. This keeps each Oracle query small (cheaper to retry on a
transient outage of one schema) and matches the existing per-schema
snapshot-key layout.

### Schema catalog generation/consumption (Step 3)

`load_column_types(redis, schema=None)` and `build_tables_to_columns(redis,
schema=None)` now default to multi-schema aggregation when called without
a schema argument. The single-schema callers (`DataQueryAgent`,
`ValueTracerAgent`) are unaffected — they pass an explicit schema.

The new `schema=None` mode lets a caller (today: tests; tomorrow:
Phase 4 routing) ask "what tables does RTIE know about, across every
schema?" without a per-schema iteration in client code. The aggregation
unions column sets when the same table name appears in two schemas — a
defensive measure; OFSAA's canonical naming makes collisions a
non-issue in practice.

## DATA_QUERY routing — unchanged

DATA_QUERY routing remains OFSMDM-default. A query like
`What is the total N_EOP_BAL for V_LV_CODE='ABL' on 2025-12-31?` still
routes to `STG_PRODUCT_PROCESSOR` in OFSMDM. What changes is that the
underlying catalog DATA STRUCTURE has cross-schema visibility — a
caller can now look up an OFSERM column by name. Whether the agent
USES that visibility is a Phase 4 question.

## Redis state after a fresh load

Phase 1 baseline (must be unchanged):

| Key | Bytes |
|-----|-------|
| `graph:index:OFSERM` | ~284,629 |
| `graph:index:OFSMDM` | ~40,249 |

Phase 2 new state:

| Pattern | Type | Count per schema |
|---------|------|------------------|
| `graph:origins:OFSMDM:plsql` | string (msgpack) | 1 |
| `graph:origins:OFSMDM:etl` | string (msgpack) | 1 |
| `graph:origins:OFSMDM:gl_blocked` | string (msgpack) | 1 |
| `graph:origins:OFSMDM:eop_overrides` | string (msgpack) | 1 |
| `graph:origins:OFSMDM:meta` | string (msgpack) | 1 |
| `graph:origins:OFSERM:*` | (same family, OFSERM-scoped) | 5 |
| `rtie:schema:snapshot:OFSERM` | string (json) | 1 (NEW) |

## Out of scope (deferred)

Per the Phase-2 prompt, the following are deliberately left for later
phases:

- Vector store / semantic search extension to OFSERM (Phase 3)
- Source retrieval rationalization (`rtie:logic` vs `graph:source`)
  (Phase 3)
- Orchestrator routing changes (Phase 4)
- Business-identifier literal index (Phase 5)
- Parser MERGE-classification fix for OFSERM `commented_out_nodes`
  (Phase 6 — Q2 in the architecture doc)

## Testing

- 10 tests in `tests/unit/phase2/test_origins_catalog.py` — covers
  atomic-swap invariants (preserved), per-schema build, Redis
  persistence round-trip, isolated per-schema failure, and the
  multi-schema reader contracts.
- 7 tests in `tests/unit/agents/test_phase2_multi_schema_catalog.py`
  — covers `build_tables_to_columns` and `load_column_types` in both
  single-schema (Phase 1 contract) and multi-schema (Phase 2 contract)
  modes, plus a defensive check that the SQL templates remain free of
  hardcoded schema literals.
- One pre-existing failure in `tests/unit/parsing/test_query_engine.py`
  (`test_assemble_llm_payload_structure`) reproduces on main without
  any Phase 2 changes; left untouched.
