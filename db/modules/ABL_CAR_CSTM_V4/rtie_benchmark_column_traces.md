# RTIE benchmark — column traces & value lineages (OPS_RISK)

8 column-trace / value-lineage prompts to evaluate RTIE.

These differ from the process-level prompts in the previous file: each one
asks RTIE to trace a single column or value through transformations, joins,
and conditional logic. That's where lineage tools tend to fail in subtle
ways — over-summarising, dropping the conditional branch, getting the
direction wrong, missing the aggregation, etc.

Workflow:
1. Paste each *Prompt* into RTIE verbatim.
2. Save RTIE's reply.
3. Score with the rubric at the bottom.

---

## Prompt 1 (Easy — straight copy with a single dimension lookup)

> Trace `N_EXCHANGE_RATE` in `FCT_OPS_RISK_DATA` back to its source.
> Which task populates it, and which table does the value come from?

### Ground-truth answer

`N_EXCHANGE_RATE` in `FCT_OPS_RISK_DATA` is populated by the task
**`ABL Operational Risk Data Population CSTM`** (file
`OPS_RISK_DATA_POPULATION_CSTM.sql`). It is a direct copy of
`FSI_CAP_CURRENCY_CONVERSION.N_EXCHANGE_RATE` — no arithmetic
transformation, no scaling.

The join condition that selects which rate row to copy is:
```
FSI_CAP_CURRENCY_CONVERSION.N_MIS_DATE_SKEY = DIM_DATES.N_DATE_SKEY  (run MIS date)
AND FSI_CAP_CURRENCY_CONVERSION.N_RUN_SKEY = DIM_RUN.N_RUN_SKEY       (current run)
AND FSI_CAP_CURRENCY_CONVERSION.V_CCY_CODE = STG_OPS_RISK_DATA.V_CCY_CODE
```

So the rate is the one configured for the row's natural currency (`V_CCY_CODE`)
on the current MIS date and run.

If `FSI_CAP_CURRENCY_CONVERSION` has no matching row, `N_EXCHANGE_RATE` is
NULL — and every reporting-CCY column derived from it (N_NET_INTEREST_INCOME,
N_LOANS_ADVANCES_AMT, etc.) will also be NULL.

---

## Prompt 2 (Easy — same column, different table)

> Where does `N_NET_INTEREST_INCOME_NCY` in `FCT_OPS_RISK_DATA` come from,
> and is any transformation applied?

### Ground-truth answer

`N_NET_INTEREST_INCOME_NCY` is a **direct copy** of
`STG_OPS_RISK_DATA.N_NET_INTEREST_INCOME`, populated by
`OPS_RISK_DATA_POPULATION_CSTM` during the initial INSERT into
`FCT_OPS_RISK_DATA`. The value is wrapped in `TO_CHAR(...)` in the SELECT
list (because `FCT_OPS_RISK_DATA.N_NET_INTEREST_INCOME_NCY` is defined as
NUMBER, the `TO_CHAR` is just an implicit no-op cast inherited from the
generated SQL — no rounding or formatting).

No CCY conversion or shareholding-% multiplication is applied at this stage.
The `_NCY` suffix marks it as natural-currency. The reporting-currency
counterpart `N_NET_INTEREST_INCOME` is populated later by a separate task.

**Source:** `OPS_RISK_DATA_POPULATION_CSTM.sql`.

---

## Prompt 3 (Medium — conditional value)

> Under what conditions does `N_SHAREHOLDING_PERCENT` in `FCT_OPS_RISK_DATA`
> take the value 1, and when does it take an entity-specific value?

### Ground-truth answer

The value is determined by a CASE expression in
`OPS_RISK_DATA_POPULATION_CSTM`:

```
CASE WHEN DIM_BASEL_CAP_CONSL_APPR.V_BASEL_CAP_CONSL_APPR_CODE = 'PRCS'
     THEN COALESCE(FCT_ENTITY_INFO.N_SHAREHOLDING_PERCENT, 1)
     ELSE 1
END
```

Translated:

- **Value = 1** when the entity's consolidation approach is `'FLCS'` (Full
  Consolidation). Under full consolidation, 100% of the subsidiary's gross
  income is rolled up — no pro-rata.
- **Value = 1** as a fallback when the approach IS `'PRCS'` (Pro-rata
  Consolidation) but `FCT_ENTITY_INFO.N_SHAREHOLDING_PERCENT` is NULL.
- **Value = `FCT_ENTITY_INFO.N_SHAREHOLDING_PERCENT`** (the actual
  ownership %) when the approach is `'PRCS'` and a non-NULL percent is
  configured. This is the only case where pro-rata multiplication actually
  reduces the carried-forward amounts.

The downstream `*_shareholding_Percent_Multiplication*.sql` tasks all then
multiply income/expense columns by this value, so under FLCS those tasks
are effectively no-ops (multiplying by 1).

**Sources:** `OPS_RISK_DATA_POPULATION_CSTM.sql`,
`OR_*_shareholding_Percent_Multiplication.sql`.

---

## Prompt 4 (Medium — value not from the obvious source)

> What entity does `FCT_OPS_RISK_SUMMARY.N_ENTITY_SKEY` represent, and is
> it the same as the entity in the underlying `FCT_OPS_RISK_DATA` rows?

### Ground-truth answer

**No, they are not the same.** `FCT_OPS_RISK_SUMMARY.N_ENTITY_SKEY` is
*not* a passed-through copy of the source row's entity.

In `OPS_RISK_SUMMARY_POPULATION.sql`, the SELECT list contains a
**scalar subquery**:

```
(SELECT n_entity_skey FROM fct_entity_info
   INNER JOIN DIM_DATES ON DIM_DATES.N_DATE_SKEY = FCT_ENTITY_INFO.n_mis_date_skey
  WHERE n_run_skey = '870'
    AND f_cap_consl_parent_entity_ind = 'Y'
    AND DIM_DATES.D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD'))
```

That subquery returns the **capital-consolidation parent entity** for the
run — typically the bank's top-level reporting entity (the holdco).

So every row in `FCT_OPS_RISK_SUMMARY` for a given run carries the same
parent-entity skey, regardless of which subsidiary's data fed in. The
real per-subsidiary detail is preserved in `FCT_OPS_RISK_DATA`, but at the
summary level it has been rolled up to the consolidating parent.

This matters for any downstream report: joining
`FCT_OPS_RISK_SUMMARY.N_ENTITY_SKEY` to entity-level dimensions will
always show the parent, not the subsidiary.

**Source:** `OPS_RISK_SUMMARY_POPULATION.sql`.

---

## Prompt 5 (Medium — aggregation with filter)

> The row count of `FCT_OPS_RISK_SUMMARY` is much smaller than that of
> `FCT_OPS_RISK_DATA` for the same run. What aggregation and what filter
> in `OPS_RISK_SUMMARY_POPULATION` cause the reduction?

### Ground-truth answer

Two effects, multiplicative:

**1. GROUP BY collapse.** The INSERT-SELECT in `OPS_RISK_SUMMARY_POPULATION`
groups by:
```
FCT_OPS_RISK_DATA.n_run_skey,
FCT_OPS_RISK_DATA.n_mis_date_skey,
FCT_OPS_RISK_DATA.n_gaap_skey,
FCT_OPS_RISK_DATA.n_country_skey,
FCT_OPS_RISK_DATA.n_branch_skey
```
Aggregation operators in the SELECT list:
- `MAX(N_BASEL_METHOD_SKEY)` — collapses methodology
- `MAX(N_STANDARD_LOB_SKEY)` — collapses standard LoB
- `SUM(N_RWA_AMT)` — sums the RWA contributions

So three years of `D_FINANCIAL_YEAR` data per entity / LoB collapse into
a single summary row per (run, mis_date, GAAP, country, branch) tuple.

**2. NOT NULL filter.** The WHERE clause includes:
```
AND FCT_OPS_RISK_DATA.n_rwa_amt IS NOT NULL
```
Rows in `FCT_OPS_RISK_DATA` that never received a `N_RWA_AMT` (because
the methodology wasn't assigned, AGI history was insufficient, or any
upstream NULL propagated) are excluded entirely from the summary.

**Source:** `OPS_RISK_SUMMARY_POPULATION.sql`.

---

## Prompt 6 (Medium — divergent paths from one staging column)

> A single value `STG_OPS_RISK_DATA.N_OPERATING_EXPENSES` ends up
> contributing to multiple downstream columns. List every
> `FCT_OPS_RISK_DATA` column it influences and the transformation chain.

### Ground-truth answer

Only **one** direct landing column, but it influences a second column
indirectly via Annual Gross Income (AGI is *gross of* operating expenses,
so operating expenses don't subtract from AGI under Basel — they just need
to be available to derive other components).

**Direct landing:**
- `N_OPERATING_EXPENSES_NCY` — straight copy from
  `STG_OPS_RISK_DATA.N_OPERATING_EXPENSES` in
  `OPS_RISK_DATA_POPULATION_CSTM`.

**Transformations applied later:**
- `N_OPERATING_EXPENSES` (reporting CCY) = `N_OPERATING_EXPENSES_NCY ×
  N_EXCHANGE_RATE` — set by
  `OR_Operating_Income_and_Expense_Attribute_Natural_CCY_Conversion_to_Reporting_CCY.sql`.
- Then × `N_SHAREHOLDING_PERCENT` by
  `OR_Operating_Income_and_Expense_shareholding_Percent_Multiplication.sql`.

**Indirect influence on N_ANNUAL_GROSS_INCOME:** none. Per Basel II
§650, AGI is computed *gross of* operating expenses, so
`N_OPERATING_EXPENSES` is *not* in the AGI formula
(`AGI = N_NET_INTEREST_INCOME + N_NET_NON_INT_INCOME`). Operating
expenses are loaded for reporting and audit completeness, not for
the BIA capital-charge calculation itself.

**Indirect influence on N_CAPITAL_CHARGE:** none.

**Indirect influence on N_RWA_AMT:** none.

So the value lineage is: STG → `_NCY` → reporting-CCY column → shareholding-%
adjusted column. No further use in the BIA arithmetic.

**Sources:** `OPS_RISK_DATA_POPULATION_CSTM.sql`, the two
`OR_Operating_Income_and_Expense_*.sql` files.

---

## Prompt 7 (Hard — multi-step trace with conditional aggregation)

> Trace the value `STG_OPS_RISK_DATA.N_NET_INTEREST_INCOME` for a single
> entity through every column it touches until it lands in
> `FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT` for `'CAP170'`.

### Ground-truth answer

This is the classic end-to-end value trace. There are **8 distinct values**
the original NII number flows through, and 4 different rows in 4 different
tables. Listed in order:

| # | Table.Column | Value (relative to STG NII = X) | Set by |
|---|---|---|---|
| 1 | `STG_OPS_RISK_DATA.N_NET_INTEREST_INCOME` | X | (upstream load `FN_LOAD_OPS_RISK_DATA`) |
| 2 | `FCT_OPS_RISK_DATA.N_NET_INTEREST_INCOME_NCY` | X | `OPS_RISK_DATA_POPULATION_CSTM` |
| 3 | `FCT_OPS_RISK_DATA.N_NET_INTEREST_INCOME` (reporting CCY) | X × R<br>(R = `N_EXCHANGE_RATE`) | `OR_Operating_Income_and_Expense_Attribute_Natural_CCY_Conversion_to_Reporting_CCY` |
| 4 | `FCT_OPS_RISK_DATA.N_NET_INTEREST_INCOME` (post-shareholding) | X × R × S<br>(S = `N_SHAREHOLDING_PERCENT`, =1 for FLCS) | `OR_Operating_Income_and_Expense_shareholding_Percent_Multiplication` |
| 5 | `FCT_OPS_RISK_DATA.N_ANNUAL_GROSS_INCOME` (per-year row) | X × R × S + (NNII × R × S) | `OR Annual Gross Income Calculation - BIA` (TYPE3 rule) |
| 6 | `FCT_OPS_RISK_DATA.N_CAPITAL_CHARGE` | 0.15 × avg over trailing 3 years of MAX(AGI_y, 0) — NII contributes via the year(s) where it was part of a positive AGI | DT `OPS_RISK_CAPITAL_CHARGE_CSTM` |
| 7 | `FCT_OPS_RISK_DATA.N_RWA_AMT` | N_CAPITAL_CHARGE × 12.5 | `Operational RWA Calculation` (TYPE3 rule) |
| 8 | `FCT_OPS_RISK_SUMMARY.N_RWA_AMT` | SUM of step-7 grouped by (run, mis_date, GAAP, country, branch) | `OPS_RISK_SUMMARY_POPULATION` |
| 9 | `FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT` for `V_STD_ACCT_HEAD_ID = 'CAP170'` | SUM of step-8 grouped by (date, run, gaap, std_acct_head, cap_comp_group, entity) | `OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP` |

Two things to flag:

- **NII's contribution at step 6 is not linear in X.** The 3-year
  averaging uses `MAX(AGI, 0)`, so a single year's NII only flows through
  if that year's total AGI was positive. A single negative year drops out
  of both the numerator and the denominator.
- **The aggregation at steps 8 and 9 means the "single entity" framing
  collapses.** By step 8, `N_ENTITY_SKEY` is the consolidation-parent
  entity (see Prompt 4), and CAP170 is the bank-wide ops-risk RWA. So
  this entity's NII is one input among many to a single aggregate value.

**Sources:** all files cited above.

---

## Prompt 8 (Hard — value-condition inversion)

> A row in `FCT_OPS_RISK_DATA` has `N_RWA_AMT = NULL` even though
> `N_NET_INTEREST_INCOME` and `N_NET_NON_INT_INCOME` are both populated
> and positive. List every condition that could explain it, in order of
> the pipeline.

### Ground-truth answer

For `N_RWA_AMT` to be NULL despite valid current-year income, one of the
following must be true. They are listed in pipeline order, so the first
NULL you observe upstream identifies the cause.

1. **`N_BASEL_METHOD_SKEY` was not set to BIA.** The
   `OR Basel Methodology Assignment - BI` rule only stamps the BIA skey
   on rows whose entity is configured for BIA. If the entity is on TSA,
   ASA, or AMA, this row is skipped by every BIA-only downstream rule —
   including the capital-charge DT and the RWA calculation.

2. **`N_ANNUAL_GROSS_INCOME` is NULL.** This happens when *either*
   `N_NET_INTEREST_INCOME` or `N_NET_NON_INT_INCOME` is NULL post-CCY-
   conversion. Even if both are populated for the *current* year, a NULL
   in either column for *any of the 3 years in the window* will block the
   capital-charge DT.

3. **No years in the trailing 3-year window have positive AGI.** The
   capital-charge DT computes `0.15 × Σ MAX(AGI, 0) / count(positive
   years)`. If all three years are zero or negative, the denominator is
   zero and the DT returns NULL (Basel-correct: BIA charge is undefined
   under those conditions).

4. **Insufficient history.** If the entity has fewer than 3 years of
   `D_FINANCIAL_YEAR` rows for this LoB, the rolling window is short.
   Whether this produces NULL or just a partial-window charge depends on
   how the DT was customised (the screenshot indicates the standard
   `FN_OPS_RISK_CAPITAL_CHARGE_CSTM` was replaced — the replacement may
   require all 3 years to be present).

5. **`N_CAPITAL_CHARGE` is NULL or zero.** If anything above produced a
   NULL charge, `Operational RWA Calculation` (which does
   `N_CAPITAL_CHARGE × 12.5`) propagates the NULL forward to
   `N_RWA_AMT`.

6. **`OR Internal LoB to Standard LoB Reclassification` failed.** If
   `N_STANDARD_LOB_SKEY` is NULL, the row may still survive into
   `FCT_OPS_RISK_DATA` but will be excluded from `FCT_OPS_RISK_SUMMARY`
   (which has `N_RWA_AMT IS NOT NULL` in its WHERE clause). This doesn't
   make `N_RWA_AMT` NULL inside `FCT_OPS_RISK_DATA` itself but it does
   prevent the row from contributing to CAP170 at all.

To diagnose a real row, run the triage SQL in Prompt 7 of the previous
benchmark file (`rtie_benchmark_ops_risk_processing.md`). The first NULL
column working left-to-right (NCY → reporting CCY → AGI → charge → RWA)
identifies which stage broke.

---

## Scoring rubric

For each prompt, mark Pass / Partial / Fail. Look specifically for these
trace-quality dimensions:

| Dimension | What to check | Common RTIE failure |
|---|---|---|
| **Direction** | Does RTIE distinguish source vs destination? | Treats a write target as a read source (e.g. saying CAP170 feeds the charge) |
| **Granularity** | Does it list every transformation, or skip steps? | Collapses CCY conversion + shareholding into "applies adjustments" |
| **Conditional logic** | Does it preserve the CASE / WHEN branches? | Says "shareholding %" without noting the FLCS-vs-PRCS split |
| **Aggregation** | Does it state SUM/MAX/GROUP BY explicitly? | Treats `FCT_OPS_RISK_SUMMARY` as 1:1 with `FCT_OPS_RISK_DATA` |
| **Filters** | Does it list the WHERE-clause exclusions that drop rows? | Misses `n_rwa_amt IS NOT NULL`, the `V_LOB_CODE='ABLOR'` filter, etc. |
| **Formula** | When a regulatory formula is involved, is it cited? | Describes data movement without ever stating BIA formula |
| **Schema** | Does it correctly identify which schema each object lives in? | Conflates OFSDMINFO and OFSERM |
| **Object type** | Function vs DT vs TYPE3 rule — is it correct? | Calls a DT a "function" or vice versa |

| #  | Difficulty | Tests dimension(s)                        |
| -- | ---------- | ----------------------------------------- |
| 1  | Easy       | Direction, schema                         |
| 2  | Easy       | Direction, granularity                    |
| 3  | Medium     | Conditional logic                         |
| 4  | Medium     | Aggregation, scalar subquery              |
| 5  | Medium     | Aggregation, filters                      |
| 6  | Medium     | Direction (no propagation through AGI)    |
| 7  | Hard       | All dimensions                            |
| 8  | Hard       | Conditional logic, formula, filters       |

After collecting RTIE's responses, paste them back and we'll diff.
