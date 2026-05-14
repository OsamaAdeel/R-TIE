# Stakeholder test 2 — 2026-05-14 — Chain ordering calibration

Second piece of gold-standard calibration evidence after the 2026-05-12
stakeholder test. Drives W89 (chain ordering — fixed in this PR), the
W80 scope expansion, and the new W90 / W91 / W92 tickets.

## Query

> Trace `N_SIGNIFICANT_INVST_AMT` from classification through deduction.

## Reference response (Cowork — independent)

Walked 5 functions in execution order, matching the manifest pipeline:
classification → aggregation → threshold → deduction routing.

**Paste Cowork's full markdown response here:**

```markdown
<TOHEED TO PASTE: full Cowork response, including the 5 functions
in execution order with their narrative steps.>
```

## Actual RTIE response (2026-05-14)

Returned 10 functions; none matched Cowork's correct 5-function list.
Narrative walked them in a non-execution order:

  Step 1: deduction (significant)        ← end of pipeline
  Step 2: phase-in
  Step 3: deduction (INsignificant)       ← topic flips
  Step 4: deduction (significant)         ← repeats Step 1
  ...

**Paste RTIE's full markdown response here:**

```markdown
<TOHEED TO PASTE: full RTIE response, including functions_analyzed
list and the step-by-step narrative.>
```

## Failure attribution

Two failures compound on this query:

1. **Retrieval incorrectness — W80 territory.** Most retrieved
   functions are name-similar but pipeline-wrong. Pure name-similarity
   matching missed upstream functions operating on different table
   names. The trace returned 10 functions, 0 matching Cowork's correct
   5-function pipeline. Scope: closer to 100% retrieval miss on this
   cross-table multi-stage VARIABLE_TRACE (the original Run 8 estimate
   of "~25% retrieval miss" understates the failure mode).

2. **Ordering incorrectness — W89 territory.** Even if retrieval
   returned the right 5 functions, today's assembly path doesn't sort
   them by execution order. Manifest's `task_order` (per W39) carries
   the order signal, but the VARIABLE_TRACE assembly path doesn't
   consult it.

W89 (this PR) closes the ordering gap by sorting the chain by
`(batch, process, sub_process_path, task_order)` before the narrative
LLM is invoked. W80 (separate work, scope expanded based on this
evidence) is the retrieval fix.

W89 cannot fully fix the test_2 case alone — both detectors must
land. But after W89, every VARIABLE_TRACE response carries a clean
ordering-by-construction property, and future stakeholder evaluations
can cleanly attribute failures to retrieval vs ordering instead of
seeing a scrambled response and not being able to tell.

## Other detector signals captured from this response

- **W57 GROUNDING-LOW fired:** "Line 24 cited 4 times" — the W57
  padding detector working as designed at LOW tier. Actual padding
  pattern was 27 distinct empty-text citations at the same line
  across multiple SQL blocks (distributed padding at scale). Today
  LOW tier, advisory only, badge stays VERIFIED. **W90 (new)** tracks
  the upgrade to HIGH-tier with badge flip when over a threshold.

- **`(SCHEMA)` placeholder leak in markdown heading:** Response
  heading shows literal `(SCHEMA)` — a template placeholder that
  wasn't substituted. Also surfaced in Q9 of 2026-05-12. **W91
  (new)** is the small fix to substitute the actual schema name.

- **Schema-label mismatch:** `data.schema: "OFSMDM"` in payload, but
  every table cited (FSI_NON_REG_CONSL_ENTITY_INVST, etc.) is
  OFSERM. `schema_searched` correctly lists both schemas — only the
  single-schema label is wrong. **W92 (new)** is the response-builder
  cleanup.

## Cross-reference

- Weakness log entries: W89 (fixed this PR), W90 / W91 / W92 (new),
  W80 (scope expanded). See `docs/RTIE_Weakness_Log.md`.
- W89 implementation: `src/agents/chain_ordering.py` +
  `src/agents/variable_tracer.py::build_transformation_chain` +
  `src/main.py::event_stream` (VARIABLE_TRACE branch).
- W89 tests: `tests/unit/agents/test_w89_chain_ordering.py` (20
  tests) + `tests/integration/test_live_stream.py` (3 tests).
