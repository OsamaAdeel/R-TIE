# W88b — Classifier dependency for date-less named-computation queries (backlog)

**Status:** Logged, not started
**Parent:** W88 (named regulatory computation pre-router, FIXED 2026-05-18)
**Discovered during:** W88 verbatim canary run (2026-05-18) — see [scratch/w88_canary_artifacts/](../scratch/w88_canary_artifacts/) for first-run failures C04/C06/C08/C09.

---

## The seam

W88's pre-router is wired inside `data_query.answer_stream` between [_resolve_target_schema](../src/agents/data_query.py) and [_build_schema_catalog](../src/agents/data_query.py). It fires only on queries the orchestrator has already classified as `DATA_QUERY`. That gate is correct in principle — other query types have their own anchor paths (W76 for `FUNCTION_LOGIC`, BI routing for `VARIABLE_TRACE`) — but it ties W88's coverage to whatever the classifier decides.

The diagnostic ([docs/w88_diagnostic.md](w88_diagnostic.md) Section 3) tested 15 named-computation queries and observed 15/15 classified as `DATA_QUERY`. Empirically that held for queries phrased with explicit dates ("What is the CET1 ratio **on 2025-12-31**?") — but **not** for date-less phrasings.

## The empirical failure

During the W88 canary run, four queries phrased without dates failed routing through W88 entirely:

| Canary | Query (date-less)                                  | Routed by classifier to | Resulting behavior                          |
|--------|----------------------------------------------------|--------------------------|---------------------------------------------|
| C04    | "What is the Capital Adequacy Ratio?"              | (entity-seeking type)    | W87 fires → `type='unrecognized_term'`     |
| C06    | "What is the total Market Risk RWA?"               | (entity-seeking type)    | `type='clarification'`                      |
| C08    | "What is the Liquidity Coverage Ratio?"            | (entity-seeking type)    | W87 fires → `type='unrecognized_term'`     |
| C09    | "What is the Net Stable Funding Ratio?"            | (entity-seeking type)    | W87 fires → `type='unrecognized_term'`     |

All four passed when re-asked with "on 2025-12-31" appended. The classifier reads the date as a strong DATA_QUERY signal; without it, the named-computation phrasing alone reads to the classifier as a concept lookup.

**This is not a W87 bug.** W87 is doing its job: it sees an entity-seeking type with no resolvable function / column / BI literal and surfaces an honest "I don't know that term." The bug is upstream — the classifier shouldn't have routed `"What is the CET1 ratio?"` to an entity-seeking type in the first place.

## What production users will hit

Stakeholders ask date-less questions casually ("what's the CET1 ratio?") more often than they remember to scope by date. Without W88b they'll bounce off W87's unrecognized-term clarification rather than getting the canonical OFSERM answer. The user-facing experience for ~40-50% of named-computation queries (rough estimate from the canary sample) regresses to "RTIE doesn't know what that is" when the answer is sitting in the W88 registry one classifier-decision away.

## Two fix shapes

### Shape A — Move W88 detection upstream of the classifier

Detect named-computation phrasings in `orchestrator.classify_query` or in `main.py` before classification, force-route those queries to DATA_QUERY, and let the data_query.py wiring handle the rest. The W88 registry would be queried twice (once for early-route detection, once for the canonical-SQL emit), but the registry lookup is microseconds.

- **Supports it:** Closes the seam directly — W88's coverage no longer depends on the classifier's decisions. The detection is a pure regex pass over `raw_query`; no LLM cost added.
- **Argues against:** Crosses the orchestrator/data_query module boundary. Two call sites for the same registry. Adds a "force-classify" path that bypasses the existing classifier's contract.

### Shape B — Add classifier hint for named computations

Extend the classifier prompt (or its post-processing) to recognize the named-computation phrasings explicitly and bias toward DATA_QUERY. Simpler: when the classifier's intent / search_terms include a registered W88 name, override `query_type` to `DATA_QUERY` post-classification.

- **Supports it:** Minimal blast radius — one classifier-shaping helper. Keeps W88 wiring exactly as it is in [data_query.py](../src/agents/data_query.py). Mirrors how W76 (named-function anchor) interacts with the classifier.
- **Argues against:** Coupling — the classifier becomes aware of W88's registry. Adding a new named computation now requires touching two places (the registry + this hint).

**Tentative pick:** Shape A. The duplicate registry lookup is trivial; the classifier-coupling in B is the deeper smell. Shape B is the right call only if the classifier is the natural place to declare "named regulatory computations are always DATA_QUERY semantically" — but that's already true by definition of the W88 registry.

## Scope

1. Add an early-route hook in `main.py` (before `classify_query` runs, or immediately after it) that calls `detect_named_computation(raw_query, query_type=None)` — same detector function, but bypass its DATA_QUERY gate when invoked from this caller.
2. When the early detector matches, force `state["query_type"] = "DATA_QUERY"` so the downstream data_query path is taken.
3. Verify W87's gate still passes through correctly — DATA_QUERY isn't in W87's set, so this should be a no-op for W87.
4. Re-run the 4 date-less canaries (C04/C06/C08/C09 with their original dateless phrasings) and confirm they route through W88 anchor / decline correctly.
5. Update the unit-test surface — `detect_named_computation` currently gates on `query_type == "DATA_QUERY"`. Either add a `gate=False` argument for the early-route caller, or relax the gate when called from the early-route path.

## Non-goals

- **Does not change W87's behaviour.** W87's gate set remains `{FUNCTION_LOGIC, COLUMN_LOGIC, VARIABLE_TRACE}` — DATA_QUERY routing is what changes, not W87's decline logic.
- **Does not change W88's anchor SQL or decline framing.** Both stay exactly as shipped in v1.
- **Does not change the classifier prompt.** Shape A doesn't touch the classifier; only the routing decision downstream of it.

## Pre-condition

W88 must remain in place — both the registry and the data_query wiring. W88b is additive: when the classifier picks a non-DATA_QUERY type for a known named computation, the early-route hook overrides; otherwise nothing changes.

## Why a separate ticket

W88's scope was the structural pre-router: registry + anchor SQL + decline. The classifier-dependency was discovered only during the canary run, after W88's data_query wiring was already committed. Reworking the orchestrator-side routing inside W88 would have widened the diff into a different module boundary and pushed the merge past the verbatim review. W88b is the natural follow-up.

## Not blocking, but priority is medium-high

W88 v1 ships with a documented workaround (include a date in the query); W88b makes that workaround unnecessary. Users will discover the seam by themselves within the first few stakeholder demos.
