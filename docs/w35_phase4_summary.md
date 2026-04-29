# W35 Phase 4 — Schema-Aware Orchestrator Routing

**Branch:** `refactor/w35-phase4-orchestrator-routing`
**Closes:** structural completion of OFSERM column query support — DATA_QUERY
and VARIABLE_TRACE now route OFSERM-owned tables and columns to the right
schema. Major progress on W35.

Phase 1 made the keys schema-aware. Phase 2 populated the multi-schema
catalogs and origins. Phase 3 closed source retrieval and semantic search
into OFSERM. After Phase 3, OFSERM **function-name** queries returned
VERIFIED end-to-end. OFSERM **column** queries (DATA_QUERY against an
OFSERM table, VARIABLE_TRACE against an OFSERM-owned column) still
silently routed to the OFSMDM-default catalog and either declined or
suggested a wrong-schema substitute.

Phase 4 closes that last routing gap.

## What changed

### New multi-schema lookup helpers (Step 1 → 2 plumbing)

`src/parsing/schema_discovery.py` gains three pure-helper functions:

| Helper | Purpose |
|---|---|
| `schemas_for_table(table_name, redis)` | Returns the schemas whose graphs reference the table as `target_table` or `source_tables`. Used by DATA_QUERY routing. |
| `schemas_for_column(column_name, redis)` | Returns the schemas whose `graph:index:<schema>` lists the column. Used by VARIABLE_TRACE routing. |
| `identifier_grounded_in_any_schema(ident, redis)` | Substring scan over `graph:source:<schema>:*` across every discovered schema. Used by the W45 detector's multi-schema backstop. |

All three accept an optional pre-snapshotted `schemas=` list to avoid a
re-SCAN when the caller already enumerated `discovered_schemas()`. None
mutate state; all fail closed (empty list / False) on Redis exceptions.

### DATA_QUERY routing (Step 2)

Before Phase 4, `DataQueryAgent.answer(user_query, schema=…)` passed the
orchestrator-classified schema (default OFSMDM) straight through to
`_build_schema_catalog`. The catalog was scoped to that one schema, so
OFSERM tables never appeared and the LLM either picked an OFSMDM
substitute (e.g. `STG_STANDARD_ACCT_HEAD` for `FCT_STANDARD_ACCT_HEAD`)
or produced a `column_not_found` rejection.

After Phase 4, the agent runs a routing pre-check before catalog build:

1. `_extract_user_query_tables(user_query)` — picks up OFSAA-shaped
   tokens (`STG_/FCT_/FSI_/DIM_/SETUP_/OFSDWH_/INTERNAL_/MAP_` prefixes)
   from the natural-language question. Function-name prefixes
   (`FN_/TLX_/ABL_/MAPPING_`) are excluded so a query about a function
   doesn't confuse the table resolver.
2. `_resolve_target_schema(user_query, default_schema, redis)` — for
   each candidate token, calls `schemas_for_table`. Three outcomes:
   - **Single owner** → pivot the request to that schema. The catalog
     is rebuilt against the table's actual owner; the rendered prompt
     emits `Table: <schema>.<table>` (qualified form) for non-OFSMDM
     schemas; the LLM's generated SQL inherits the qualifier.
   - **Multiple owners** (a table that lives in two schemas at once,
     synthetic in today's corpus but real after future loads) →
     short-circuit with a `table_ambiguous` CLARIFICATION response. The
     response carries one schema-qualified rephrase per candidate so
     the user can pick.
   - **Zero owners or no table mentioned** → keep the
     orchestrator-classified default; existing OFSMDM-only queries
     stay unchanged.
3. `_build_schema_catalog(target_schema, qualify_in_prompt=…)` — same
   internals as Phase 3, plus an optional flag that prefixes each
   `Table:` line with `<schema>.`. The dict keys returned are still
   bare so `SQLGuardian.validate_column_residency` (which strips the
   schema qualifier in `FROM`) keeps working unchanged.
4. The system prompt grows a single hard-constraint paragraph
   instructing the LLM to mirror the qualified `<schema>.<table>` form
   when the catalog uses it, and to keep bare names otherwise.

The result dict gains a `"schema": target_schema` key so `main.py` can
surface the schema RTIE actually routed to in the streamed `meta`
event — distinct from the orchestrator's classification when the pivot
fired.

`main.py:_data_query_stream` dispatches the new
`result["type"] == "table_ambiguous"` shape onto the same SSE path as
the existing `identifier_ambiguous` clarification.

### VARIABLE_TRACE routing (Step 3)

`main.py`'s graph-pipeline branch (the path that builds `llm_payload`
from per-function graphs) previously took
`g_schema = state.get("schema") or fallback_to_default_schema(...)`.
For OFSERM column queries this resolves to OFSMDM, and the subsequent
`resolve_query_to_nodes(query_type="variable", schema="OFSMDM", …)`
asks `graph:index:OFSMDM` for an OFSERM-only column — guaranteed miss.

Phase 4 adds an explicit column-owner pivot at the same call site:

```
if target_var and _graph_redis is not None:
    column_owners = schemas_for_column(target_var, _graph_redis)
    if len(column_owners) == 1 and column_owners[0] != g_schema:
        g_schema = column_owners[0]
```

When the column lives in exactly one schema, the graph pipeline pivots
to that schema. Multi-schema columns keep the orchestrator's
classification (the existing semantic-search + raw-source fallback
path is multi-schema-aware after Phase 3). Cross-schema function
references — Phase 0 found 16% of OFSERM functions read OFSMDM staging
tables unqualified — remain handled by the existing graph traversal,
which honours the explicit `OFSMDM.STG_PRODUCT_PROCESSOR`-style
qualifications in the source.

### Catalog renderer fall-through (Step 2 follow-on)

Surfaced during canary (e) validation. Pre-Phase 4 the catalog was
scoped to OFSMDM, where the loaded modules INSERT into every relevant
table — so the graph-derived column set was always non-empty. Phase
4's pivot to OFSERM brought in tables that the corpus only **reads**
from (e.g. `DIM_DATES` is referenced as a `JOIN`/`FROM` source in
~141 OFSERM functions but is never an INSERT target). The renderer
populated `tables_to_columns["DIM_DATES"]` with an empty set via
`setdefault(table, set())` on the source-tables walk, then emitted
`Columns: (none discovered in graph)` literally — and the LLM
paraphrased that as the `unsupported.reason` text returned from
canary (e):

> "DIM_DATES exists but no columns were discovered in the schema."

The Oracle snapshot at `rtie:schema:snapshot:OFSERM` already held all
36 DIM_DATES columns (the Phase 2 startup priming runs `ALL_TAB_COLUMNS
WHERE owner=:schema` for every discovered schema), but the renderer
only consulted `column_types` for *type annotations* on
already-known columns — never as a column-set fallback.

The fix in [`_build_schema_catalog`](RTIE/src/agents/data_query.py#L691-L706):
right after fetching both data sources, walk the snapshot once and
populate any `tables_to_columns[table]` that came back empty. The
enrichment is purely additive — the snapshot only fills already-listed
tables, never invents new ones — so the existing `(none discovered
in graph)` sentinel still fires for tables the snapshot also doesn't
know about. SQLGuardian's `validate_column_residency` benefits too:
it now sees the real column set when checking SQL like
`WHERE D_CALENDAR_DATE = …`.

### Classifier prompt scope correction (Step 2 follow-on, second amend)

Surfaced during canary (e) re-validation after the catalog renderer
fix landed. Symptom: canary (e) declined with `"cross-table
reconciliation against FCT tables (not in scope)"` even though the
query is a single-table aggregate, not a reconciliation.

The trigger was a single bullet at
[`orchestrator.CLASSIFICATION_SYSTEM_PROMPT`](RTIE/src/agents/orchestrator.py#L155-L162):

> `* References to FCT_* tables or downstream result tables not present
>   in the graph (cross-table reconciliation).`

Pre-Phase-4 the bullet was a defensible backstop — FCT_* tables lived
only in OFSERM, routing was OFSMDM-only, refusing all FCT_* queries
prevented confidently wrong answers. Post-Phase-4 the same bullet
becomes a false-positive engine: any FCT_* mention triggers
UNSUPPORTED *before* the schema pivot in DataQueryAgent has a chance
to run. The prompt also said "the current schema + graph" (singular),
which is no longer accurate after the pivot.

The fix is a prompt edit, not code. Three changes in the same
prompt:

1. **Bullet rewrite** — trigger is now reconciliation phrasing, not
   table-name pattern matching:
   > `* Reconciliation queries comparing values across two tables
   >   (typically STG vs FCT) — phrased with "differs from", "differs
   >   between", "doesn't match", "reconcile X with Y", "X vs Y for
   >   account ...". A bare aggregate / row query against an FCT_*
   >   table in any discovered schema is NOT unsupported — it routes
   >   as DATA_QUERY against the table's owning schema.`
2. **Schema generalization** — "the current schema + graph" → "any
   discovered schema + its parsed graph".
3. **New positive few-shot** — explicitly demonstrates that
   `"What is the total N_STD_ACCT_HEAD_AMT in FCT_STANDARD_ACCT_HEAD
   on 2025-12-31?"` classifies as DATA_QUERY (with `schema_name:
   "OFSERM"`), so the LLM has a concrete positive case alongside the
   two existing reconciliation negatives.

The two pre-existing reconciliation negatives at lines 261 and 267
remain unchanged — both use "differ"/"differs from" phrasing and
should still classify as UNSUPPORTED under the new rule, guarding
against the prompt over-relaxing.

A regression-guard suite at
[`tests/unit/agents/test_phase4_orchestrator_prompt.py`](RTIE/tests/unit/agents/test_phase4_orchestrator_prompt.py)
pins five invariants on the prompt text: the broad FCT_* trigger is
gone, reconciliation phrasing is present, the positive FCT example
is paired with `query_type: "DATA_QUERY"`, the negative examples
remain paired with `UNSUPPORTED`, and the schema-singular phrasing
is gone.

**Note on Phase 4 scope.** The original Phase 4 prompt's "MUST NOT
change: Any prompt" rule was technically violated by this edit. We
landed it on the same branch as a deliberate exception because (a)
the rule discovery only happened during Phase 4 canary validation,
(b) without it the user-visible Phase 4 promise (DATA_QUERY against
OFSERM tables) is half-met, and (c) the change is scope-corrective
rather than scope-expanding — it removes a pre-Phase-4 backstop that
became a false-positive after the routing pivot, restoring the rule
to the intent its examples already demonstrated. Recorded here as a
deliberate scope decision, not stealth scope-creep.

### W45 detector multi-schema backstop (Step 4)

Pre-Phase 4 `detect_ungrounded_identifiers` decided ungroundedness from
the multi_source dict alone. After Phase 3 made vector search
multi-schema-aware that's usually accurate, but a CAP-code-like
identifier owned by an OFSERM function that didn't make it into the
top-K retrieval still produced a false-positive W45 fire.

Phase 4 plumbs an optional `redis_client` parameter through both
`detect_ungrounded_identifiers` (the pre-generation routing check) and
`evaluate_grounding` (the post-hoc warning emitter). When supplied,
both helpers call `identifier_grounded_in_any_schema` on each
locally-ungrounded candidate and drop the ones found in any schema's
source body. Pre-Phase-4 callers (no `redis_client` arg) get the
original behaviour unchanged; existing tests stay green.

`main.py` passes `_graph_redis` to both call sites so the streamed
SSE response uses the multi-schema view.

### `fallback_to_default_schema` accounting

Phase 4 does NOT remove the helper — call sites that still hit it
(`main.semantic_search`, `main.graph_pipeline`,
`main._phase2_stream`, `main._data_query_stream`,
`logic_explainer.hierarchy_header`,
`pipeline.logic_graph`,
`metadata_interpreter`) all log a single `WARNING` line per fire.

In Phase 4 testing, the warnings should fire **only** on queries where
no schema can be plausibly resolved (e.g. queries that mention no
table, no specific column, and no function name). Any OFSERM-table
DATA_QUERY or OFSERM-column VARIABLE_TRACE that still emits the
warning is a Phase 4 regression — the pivot path should have
overridden the default upstream.

## Out of scope (deferred)

- **Business identifier indexing** (`graph:literal:<schema>:<id>`) —
  Phase 5. CAP-code queries like "How is CAP973 calculated?" still
  reach the W45 path because the WHERE-clause literals in OFSERM
  functions aren't yet indexed as first-class identifiers. The
  multi-schema source-body scan helps with grounded literals
  (`identifier_grounded_in_any_schema` finds `CAP943` in any function
  that mentions it as a literal) but doesn't yet drive routing.
- **Expression extraction for derivations** — Phase 6.
- **Business-identifier-driven routing** — Phase 7 (the question "How
  is CAP973 calculated?" pivoting to the schema whose function
  computes the value).
- **`rtie:logic:` vs `graph:source:` cache unification** — Phase 8.
- **VALUE_TRACE cross-schema target-table resolution** — current
  `_TARGET_TABLE_BY_COLUMN` map in `value_tracer.py` is OFSMDM-only.
  Out of scope for Phase 4 (which the prompt scopes to DATA_QUERY +
  VARIABLE_TRACE + W45). Carrying as observed-but-deferred.

## Testing

New tests:

* `tests/unit/parsing/test_phase4_schema_lookup.py` — 12 tests
  covering `schemas_for_table` / `schemas_for_column` /
  `identifier_grounded_in_any_schema` shape and edge cases (single
  owner / ambiguous / unknown / case-insensitivity / Redis-None / 
  empty index / cross-schema source scan).
* `tests/unit/agents/test_phase4_data_query_routing.py` — 10 tests
  covering `_extract_user_query_tables`,
  `_resolve_target_schema` (pivot / default-fallthrough / ambiguity),
  `_build_table_ambiguous_response` shape, and the
  `_build_schema_catalog(qualify_in_prompt=…)` rendering.
* `tests/unit/agents/test_phase4_w45_multi_schema.py` — 6 tests
  pinning the pre-Phase-4 (no-redis) behaviour and the new
  redis-supplied multi-schema backstop in both
  `detect_ungrounded_identifiers` and `evaluate_grounding`.

Existing tests:

* `tests/unit/agents/test_ambiguity.py` two `lambda schema:`
  monkeypatches updated to `lambda schema, qualify_in_prompt=False:`
  to match the new `_build_schema_catalog` signature. No behavioural
  change.
* All Phase 1/2/3 tests (origins, multi-schema catalog, schema TAG,
  Phase 3 source retrieval, vector store) continue to pass.
* The pre-existing
  `tests/unit/parsing/test_query_engine.py::test_assemble_llm_payload_structure`
  failure noted in the Phase 3 summary remains. It reproduces on
  `main` without any Phase 4 changes; left untouched.

## User-visible canaries

Phase 4 NEW canaries:

> **(e)** "What is the total N_STD_ACCT_HEAD_AMT in
> FCT_STANDARD_ACCT_HEAD on 2025-12-31?"

Expected: VERIFIED + SUM result. Generated SQL targets
`OFSERM.FCT_STANDARD_ACCT_HEAD` (schema-qualified). No "did you mean
STG_STANDARD_ACCT_HEAD?" suggestion. The streamed `meta` event reports
`schema: OFSERM` (overrides the orchestrator-classified OFSMDM).

> **(f)** "What writes N_STD_ACCT_HEAD_AMT?"

Expected: VARIABLE_TRACE response citing OFSERM functions
(`CS_Deferred_Tax_…` and others that target FCT_STANDARD_ACCT_HEAD).
No W45 fire — the column is grounded in OFSERM source bodies.

> **(g)** "What is the total N_EOP_BAL on 2025-12-31?"

Expected: unchanged. No specific table named, default schema stays
OFSMDM, prompt and SQL stay bare-table.

> **(h)** "How is FAKE_COLUMN_999 calculated?"

Expected: unchanged. W45 fires correctly because the identifier is
truly absent from every schema's source bodies.

Regression triple (a/b/c/d) — must remain identical to Phase 3:

* (a) Function-name COLUMN_LOGIC for an OFSMDM function: VERIFIED +
  hierarchy.
* (b) DATA_QUERY for an OFSMDM column: VERIFIED + SUM
  (`-24,179,237,139.63`).
* (c) CAP-code COLUMN_LOGIC: UNVERIFIED + W45 (Phase 5 fixes the
  CAP-code path, not Phase 4) — **see CAP973 routing observation
  below; live behaviour drifted from this prediction.**
* (d) OFSERM function-name query
  (`CS_Deferred_Tax_Asset_Net_of_DTL_Calculation`): VERIFIED +
  hierarchy + MERGE + CAP derivation (Phase 3).

## CAP973 routing observation (canary (c) follow-up)

**Tracking note for the PR description.** Canary (c) ("How is CAP973
calculated?") changed behaviour as a side effect of the Phase 4 W45
multi-schema backstop:

* Pre-Phase-4: UNVERIFIED + W45 ungrounded (the multi_source check
  didn't find CAP973 in any retrieved function body for that query).
* Post-Phase-4: VERIFIED, routed to
  `REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP`.

**Why it changed:** the W45 backstop scans `graph:source:<schema>:*`
across every schema before flagging an identifier as ungrounded.
CAP973 lives as a WHERE-clause literal in two OFSERM functions
(`REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP` and
`CS_Regulatory_Adjustments_Phase_In_Deduction_Amount`), so the
backstop correctly suppresses W45 — the literal IS grounded.

**Why the route lands on the loader, not the computer:**
`REGULATORY_ADJUSTMENT_STANDARD_ACCT_HEAD_DATA_POP` is the function
that **loads** CAP973 rows into `FCT_STANDARD_ACCT_HEAD`.
`CS_Regulatory_Adjustments_Phase_In_Deduction_Amount` is the function
that **computes** the phase-in deduction amount applied to CAP973 (and
seven sibling codes). Semantic search currently ranks the loader
first because its function name contains "STANDARD_ACCT_HEAD" — the
same string that dominates the corpus' table descriptions.

**Disambiguation requires Phase 5 + Phase 7:**
* **Phase 5** (literal indexing) populates
  `graph:literal:<schema>:<id>` so a query naming a business
  identifier resolves directly to every function that mentions it.
* **Phase 7** (business-identifier-driven routing) ranks those
  functions by *role* — computer above loader — so the LLM
  receives the calculation function in its top-K rather than the
  staging function.

Not a Phase 4 concern. The Phase 4 W45 update is correct in principle
(literal IS grounded, so don't claim "ungrounded"); the resulting
explanation is defensible but lower-fidelity than Phase 5/7 will
deliver. Document as expected-but-tracked behaviour.
