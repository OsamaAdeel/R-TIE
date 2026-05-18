# W88c — BIA SUM scope: single entity vs aggregate-across-entities (backlog)

**Status:** Logged, not started
**Parent:** W88 (named regulatory computation pre-router, FIXED 2026-05-18)
**Discovered during:** W88 verbatim canary run (2026-05-18) — C01 returned `20,398,245,100` vs the diagnostic's cowork reference `20,386,244,100`. 0.06% delta.

---

## The numbers

The W88 BIA anchor emits canonical SQL against `OFSERM.FCT_OPS_RISK_DATA` filtered to `N_BASEL_METHOD_SKEY=115 (ORBIA)`, scoped to the latest `(N_MIS_DATE_SKEY, N_RUN_SKEY)` via `DENSE_RANK`, then `SUM(N_CAPITAL_CHARGE)` across all rows in that run.

| Source                                | BIA capital charge       | Scope                                |
|---------------------------------------|--------------------------|--------------------------------------|
| Diagnostic Section 2 (cowork ref)     | 20,386,244,100           | Single (entity, run) row             |
| W88 v1 canary C01 (live)              | 20,398,245,100           | SUM across entities, latest run      |
| Delta                                 | **+12,001,000 (+0.06%)** | One or more small entity rows summed |

The diagnostic Section 7 anomaly #8 already flagged this: *"BIA totals differ across runs by entity-by-entity scope. Σ N_CAPITAL_CHARGE across all 57 BIA rows = 408.93 B; but the single (run=812, entity=ABL-ish) row = 20.39 B. ... Any handler that returns 'the BIA charge' without scoping run+entity will return a sum that's not the stakeholder-meaningful number. Default-scoping rules need to be explicit in the handler."*

W88 v1 implemented "scope to latest run+date" but did NOT scope to a single entity. The result is the **per-run BIA total** across all entities, which matches what a Basel analyst expects at the bank-consolidated level — but differs from the cowork reference which was a single sub-entity sample.

## What "right" looks like depends on the user

The question "what's the BIA?" has at least three valid scopes, each with a different number:

1. **Per-entity at latest run:** 20,386,244,100 (cowork's reference — a sample row).
2. **Sum across entities at latest run:** 20,398,245,100 (W88 v1 output).
3. **Sum across runs and entities:** 408,928,819,500 (diagnostic's 57-row total — the wrong answer; would mix multiple report periods).

Cowork picked (1). W88 v1 picks (2). Both are defensible depending on what the user means by "the BIA." For a single-entity bank, (1) and (2) collapse to the same number. For multi-entity, they diverge by the per-entity row count at the latest run.

## Scope: what W88c needs to decide

The W88c work is fundamentally a **default-scoping policy decision**, then a small SQL change to enforce it. Three sub-questions:

1. **Single-entity vs multi-entity default.** What does "the BIA" mean in the local OFSAA deployment? The diagnostic says the local Oracle's BIA fact rows are at `N_RUN_SKEY IN (812, 818, 840)` and `N_MIS_DATE_SKEY=20251231`, spread across multiple entities per run. The cowork reference picked one entity arbitrarily ("ABL-ish"); the bank-consolidated number is the sum across entities.
2. **Entity-naming surface.** If a user writes "BIA for ABL on 2025-12-31", should W88 filter to that entity? The current canonical SQL doesn't extract entity from the query — that's a follow-on.
3. **Surfacing the scope to the user.** Whatever W88c picks as default, the response should make the scope explicit ("BIA across all entities in run 840 = 20.40 B PKR") so the user can interpret 20.40 vs cowork's 20.39 without confusion.

## Two implementation shapes

### Shape A — Keep aggregate-across-entities; surface scope in the summary

Default stays at (2): SUM across entities at latest run. Augment the response summary to say "summed across N entities" explicitly. Cheapest change — one line in the summary string.

- **Supports it:** Matches the "bank-consolidated" view that's the Basel-meaningful number. Aligns with diagnostic Section 3 filter-defaulting rule "sum across all entities".
- **Argues against:** Diverges from cowork's reference. Stakeholders comparing W88 output against cowork will see the delta and ask why.

### Shape B — Default to single entity (the one matching local config); offer aggregate as a follow-up

Read the entity from `FSI_CAP_RUN_EXE_PARAMETERS` or default to the entity that has the most rows at the latest run. Return that entity's BIA. If the user explicitly asks for the consolidated number, surface (2).

- **Supports it:** Matches cowork's reference number. Removes the "why is your number different from cowork's?" friction.
- **Argues against:** Requires entity-resolution logic that W88 v1 explicitly defers. Local Oracle has multiple entities; picking one means encoding a "primary entity" heuristic.

### Shape C — Return both scopes; let the user pick

Emit two rows: per-entity-and-aggregate. The summary names both. User picks the one they want.

- **Supports it:** Honest — surfaces the ambiguity rather than papering over it.
- **Argues against:** Changes the response shape. Frontend rendering may need an update. Same `query_kind=AGGREGATE` but multiple meaningful values per cell.

**Tentative pick:** Shape A for the immediate fix (one-line summary change), with Shape B logged as W88c2 once entity-resolution lands as a first-class capability.

## Non-goals

- **Does not change the W88 registry shape.** The BIA `W88ComputationDefinition` stays as-is — the SQL template is what changes.
- **Does not affect the CAP-code anchors (CET1, TIER1, CAR, Credit RWA, Market RWA).** Those return single values from `FCT_STANDARD_ACCT_HEAD` which is already bank-consolidated at the CAP-code level — no entity scope ambiguity.
- **Does not block W88b.** W88b (classifier dependency) is independent and higher priority because it determines whether the BIA canary is reachable at all from date-less user phrasings.

## Pre-condition

W88 v1 must remain in place. W88c modifies the BIA anchor's SQL template only; other anchors and the decline arms are untouched.

## Why a separate ticket

W88 v1's purpose was structural: get the query to the right table, never fabricate. The SUM scope is a refinement — answer quality, not routing correctness. Conflating the two would have stretched W88 past the merge gate and pushed the canary review another round.

## Not blocking, but priority is medium

Users won't fail to get an answer (the W88 BIA canary returns a number); they'll just see a slightly different number from cowork's reference. Worth resolving before stakeholders notice and ask, but the W88 v1 result IS the bank-consolidated BIA total — defensible to leave it as the default if W88c lands on Shape A.
