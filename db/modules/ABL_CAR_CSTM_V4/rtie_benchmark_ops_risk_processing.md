# RTIE benchmark — OPS_RISK_PROCESSING (BIA)

7 graded prompts to evaluate RTIE on the operational-risk pipeline.
Paste each *Prompt* into RTIE verbatim, then compare RTIE's response to the
*Ground-truth answer* below it. Sources are cited so you can re-verify.

Grading rubric (per prompt):
- **Pass** — RTIE matches all the bolded facts in the ground truth.
- **Partial** — gets the right table/column but wrong formula or wrong direction.
- **Fail** — wrong process, wrong schema, hallucinated functions, wrong direction of flow.

---

## Prompt 1 (Easy — column lookup)

> In the ABL Operational Risk – Basic Indicator Approach process, what is the
> value of `N_ALPHA_PERCENT` in `FCT_OPS_RISK_DATA`, and which task populates it?

### Ground-truth answer

`N_ALPHA_PERCENT` is hard-coded to **`0.15`** (15%). It is set by the task
**`ABL Operational Risk Data Population CSTM`** when rows are inserted into
`FCT_OPS_RISK_DATA` from `STG_OPS_RISK_DATA`. The literal `0.15` appears
directly in the SELECT-list of the INSERT statement.

The value 0.15 is the Basel II Basic Indicator Approach **alpha factor** —
the regulatory multiplier applied to the 3-year average of positive Annual
Gross Income to produce the operational-risk capital charge.

**Source:** `functions/OPS_RISK_DATA_POPULATION_CSTM.sql`, INSERT
into `FCT_OPS_RISK_DATA(... N_ALPHA_PERCENT ...) SELECT ..., 0.15, ...`.

---

## Prompt 2 (Easy — table inventory)

> List every table read from or written to by `OPS_RISK_DATA_POPULATION_CSTM`.
> Distinguish reads vs writes.

### Ground-truth answer

**Writes (target):** `FCT_OPS_RISK_DATA` (single INSERT, with `LOG ERRORS INTO
FCT_OPS_RISK_DATA$`).

**Reads (sources, all LEFT/INNER joined to drive the SELECT):**
- `STG_OPS_RISK_DATA` (primary source — raw operational-risk data)
- `DIM_DATES`
- `DIM_RUN`
- `DIM_RUN_IDENTIFIER`
- `DIM_GAAP`
- `DIM_LOB`
- `DIM_GEOGRAPHY`
- `DIM_COUNTRY`
- `DIM_BASEL_CAP_CONSL_APPR`
- `FCT_ENTITY_INFO`
- `FSI_CAP_CURRENCY_CONVERSION`
- `RUN_PARAMETERS`

Filters worth noting: only rows where `STG_OPS_RISK_DATA.V_LOB_CODE = 'ABLOR'`
and the entity has `F_CAP_CONSL_ENTITY_IND = 'Y'` and consolidation approach
in (`'FLCS'`,`'PRCS'`).

**Source:** `functions/OPS_RISK_DATA_POPULATION_CSTM.sql`.

---

## Prompt 3 (Medium — column lineage with transformation)

> How is `N_NET_INTEREST_INCOME` in `FCT_OPS_RISK_DATA` derived from staging?
> Show the transformation step by step.

### Ground-truth answer

Three steps:

1. **Initial load (natural CCY).**
   `OPS_RISK_DATA_POPULATION_CSTM` copies
   `STG_OPS_RISK_DATA.N_NET_INTEREST_INCOME` directly into
   `FCT_OPS_RISK_DATA.N_NET_INTEREST_INCOME_NCY` (the `_NCY` suffix means
   *natural currency*). At this point `FCT_OPS_RISK_DATA.N_NET_INTEREST_INCOME`
   is still NULL.

2. **Reporting-CCY conversion.**
   The TYPE3 task `OR Operating Income and Expense Attribute Natural CCY
   Conversion to Reporting CCY` MERGEs into `FCT_OPS_RISK_DATA` and sets:

   ```
   N_NET_INTEREST_INCOME = N_NET_INTEREST_INCOME_NCY × N_EXCHANGE_RATE
   ```

   `N_EXCHANGE_RATE` was already populated from `FSI_CAP_CURRENCY_CONVERSION`
   in step 1.

3. **Shareholding-% adjustment.**
   The TYPE3 task `OR Operating Income and Expense shareholding Percent
   Multiplication` MERGEs in again and multiplies by
   `N_SHAREHOLDING_PERCENT` (which was set from
   `FCT_ENTITY_INFO.N_SHAREHOLDING_PERCENT` in step 1, but only when the
   consolidation approach is `'PRCS'` — otherwise it is `1`):

   ```
   N_NET_INTEREST_INCOME = N_NET_INTEREST_INCOME × N_SHAREHOLDING_PERCENT
   ```

**Sources:** `OPS_RISK_DATA_POPULATION_CSTM.sql`,
`OR_Operating_Income_and_Expense_Attribute_Natural_CCY_Conversion_to_Reporting_CCY.sql`,
`OR_Operating_Income_and_Expense_shareholding_Percent_Multiplication.sql`.

---

## Prompt 4 (Medium — formula derivation)

> How is `N_CAPITAL_CHARGE` in `FCT_OPS_RISK_DATA` calculated under the Basic
> Indicator Approach? Give the formula and the columns it consumes.

### Ground-truth answer

**Formula (Basel II §649–650):**

```
N_CAPITAL_CHARGE = 0.15 × ( Σ MAX(N_ANNUAL_GROSS_INCOME, 0) over the prior 3 financial years )
                          / ( count of those 3 years where N_ANNUAL_GROSS_INCOME > 0 )
```

In plain words: 15% of the average **positive** annual gross income across
the previous three years. Years with zero or negative AGI are excluded from
*both* the numerator and the denominator.

**Inputs:**
- `N_ALPHA_PERCENT` = 0.15 (from `OPS_RISK_DATA_POPULATION_CSTM`)
- `N_ANNUAL_GROSS_INCOME` for the current year and the two preceding
  `D_FINANCIAL_YEAR` rows for the same entity / LoB
- Where `N_ANNUAL_GROSS_INCOME = N_NET_INTEREST_INCOME + N_NET_NON_INT_INCOME`
  (Basel BCBS-128 §650; populated by the TYPE3 rule
  `OR Annual Gross Income Calculation - Basic Indicator Approach`)

**Implementation:** the custom Data Transform **`OPS_RISK_CAPITAL_CHARGE_CSTM`**
(per the screenshot, this DT replaces what was originally
`FN_OPS_RISK_CAPITAL_CHARGE_CSTM`). The DT is registered in OFSAA's metadata
(table `AAI_DT_DEFINITION`), not as a standalone PL/SQL function in the
`functions/` folder — searches of OFSERM confirm no function by that name.

---

## Prompt 5 (Medium — process flow)

> What is the role of `DIM_BASEL_METHODOLOGY` in the OPS_RISK_PROCESSING flow,
> and which task assigns the methodology key?

### Ground-truth answer

`DIM_BASEL_METHODOLOGY` is the lookup that tells OFSAA which Basel
operational-risk approach a row should be measured under: BIA, TSA (The
Standardised Approach), ASA, or AMA. The fact column it's joined to is
`FCT_OPS_RISK_DATA.N_BASEL_METHOD_SKEY`.

The key is set by the TYPE3 task **`OR Basel Methodology Assignment - BI`**
(see Task ID `1336007781936` in your screenshot, type TYPE3). This task
assigns the BIA methodology skey to rows whose entity / portfolio is
configured for BIA in OFSAA's setup tables (typically
`FSI_SETUP_PARAMETERS_DETAILS` and `FCT_REG_RUN_LEGAL_ENTITY_BASEL_APPR`).

`DIM_BASEL_METHODOLOGY` is then read (left-joined) by every downstream
task in the BIA flow — the CCY conversions, the shareholding-%
multiplications, and `OPS_RISK_SUMMARY_POPULATION` — so that subsequent
calculations can filter to BIA-only rows.

**Source:** join clauses in all 9 `OR_*` MERGE files plus
`OPS_RISK_SUMMARY_POPULATION.sql`.

---

## Prompt 6 (Hard — end-to-end trace)

> Trace `N_STD_ACCT_HEAD_AMT` in `FCT_STANDARD_ACCT_HEAD` for standard
> account head `'CAP170'` all the way back to its source columns in
> `STG_OPS_RISK_DATA`. List every task in order.

### Ground-truth answer

Eleven steps. Standard account head `'CAP170'` is the OFSAA seeded ID for
**Operational Risk RWA**, so this trace covers the entire BIA pipeline.

| # | Task                                                                | What it does                                                                                                          |
| - | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| 1 | (Upstream) `FN_LOAD_OPS_RISK_DATA`                                  | Populates `STG_OPS_RISK_DATA` with raw NII, NNII, operating expenses, provisions, etc., per `D_FINANCIAL_YEAR`.       |
| 2 | `ABL Operational Risk Data Population CSTM` (CSTM T2T)              | Inserts `FCT_OPS_RISK_DATA`. Loads `_NCY` columns from STG, sets `N_ALPHA_PERCENT = 0.15`, joins exchange rate & shareholding %. |
| 3 | 5 × `OR ... Natural CCY Conversion to Reporting CCY` (TYPE3)        | MERGE: `<col> = <col>_NCY × N_EXCHANGE_RATE` for Balance-sheet, Operating, Non-Operating, Other Income, Provisioning attributes. |
| 4 | 4 × `OR ... shareholding Percent Multiplication` (TYPE3)            | MERGE: `<col> = <col> × N_SHAREHOLDING_PERCENT`.                                                                       |
| 5 | `OR Internal LoB to Standard LoB Reclassification` (TYPE2)          | Sets `N_STANDARD_LOB_SKEY`.                                                                                            |
| 6 | `OR Basel Methodology Assignment - BI` (TYPE3)                      | Sets `N_BASEL_METHOD_SKEY` to BIA.                                                                                     |
| 7 | `OR Annual Gross Income Calculation - Basic Indicator Approach` (TYPE3) | Computes `N_ANNUAL_GROSS_INCOME = N_NET_INTEREST_INCOME + N_NET_NON_INT_INCOME` per row.                            |
| 8 | `OPS_RISK_CAPITAL_CHARGE_CSTM` (custom DT)                          | Computes `N_CAPITAL_CHARGE = 0.15 × 3-year average of positive AGI`.                                                   |
| 9 | `Operational RWA Calculation` (TYPE3)                               | Computes `N_RWA_AMT = N_CAPITAL_CHARGE × 12.5` (Basel scaling factor 1/0.08).                                          |
| 10 | `OPS_RISK_SUMMARY_POPULATION` (T2T)                                | Aggregates `SUM(FCT_OPS_RISK_DATA.N_RWA_AMT)` into `FCT_OPS_RISK_SUMMARY.N_RWA_AMT`, grouped by entity / std-LoB / GAAP / branch / country. |
| 11 | `OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP` (T2T)                        | Writes `SUM(FCT_OPS_RISK_SUMMARY.N_RWA_AMT)` into `FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT` for `V_STD_ACCT_HEAD_ID = 'CAP170'`, capital-comp-group `'OTH'`. |

So a row in `STG_OPS_RISK_DATA` (`N_NET_INTEREST_INCOME`,
`N_NET_NON_INT_INCOME`, etc., natural CCY) → CCY-converted →
shareholding-% adjusted → AGI computed → 3-yr-averaged at α=0.15 →
multiplied by 12.5 → aggregated → posted to CAP170.

**Sources:** `OPS_RISK_DATA_POPULATION_CSTM.sql`,
the 9 `OR_*` MERGE files,
`OPS_RISK_SUMMARY_POPULATION.sql`,
`OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP.sql`,
plus the manifest task ordering.

---

## Prompt 7 (Hard — diagnostic)

> A user reports that for MIS date 31-DEC-2025 and Run 812, `N_CAPITAL_CHARGE`
> is NULL for one specific entity even though `STG_OPS_RISK_DATA` has rows for
> that entity. Walk through the most likely failure points in OPS_RISK_PROCESSING.

### Ground-truth answer

There are six places this can break, in pipeline order. Investigate top-down.

1. **Row never made it into `FCT_OPS_RISK_DATA`.** The task
   `OPS_RISK_DATA_POPULATION_CSTM` filters out rows where:
   - `FCT_ENTITY_INFO.F_CAP_CONSL_ENTITY_IND <> 'Y'`
   - `DIM_BASEL_CAP_CONSL_APPR.V_BASEL_CAP_CONSL_APPR_CODE` not in
     (`'FLCS'`,`'PRCS'`)
   - `STG_OPS_RISK_DATA.V_LOB_CODE <> 'ABLOR'`
   - `STG_OPS_RISK_DATA.V_DATA_PROCESSING_TYPE` doesn't match the entity's
     `n_basel_consl_optn_type_skey` (1=>'C', 2=>'A')
   Run a count of `FCT_OPS_RISK_DATA` rows for that entity / run / date —
   if zero, you're stuck at this step.

2. **CCY conversion silently produced NULLs.** If
   `FSI_CAP_CURRENCY_CONVERSION` has no row for the entity's
   `V_CCY_CODE` for that MIS date / run, then `N_EXCHANGE_RATE` is NULL,
   and the MERGEs in step 3 of the pipeline produce NULL for every
   reporting-CCY income/expense column. Check
   `SELECT N_EXCHANGE_RATE, N_NET_INTEREST_INCOME_NCY, N_NET_INTEREST_INCOME
   FROM FCT_OPS_RISK_DATA WHERE n_entity_skey = ...`.

3. **Methodology not assigned.** If `OR Basel Methodology Assignment - BI`
   didn't set `N_BASEL_METHOD_SKEY` (entity isn't configured for BIA in
   `FSI_SETUP_PARAMETERS_DETAILS` /
   `FCT_REG_RUN_LEGAL_ENTITY_BASEL_APPR`), the entity falls outside the
   BIA filter on later steps and `N_CAPITAL_CHARGE` stays NULL.

4. **AGI is NULL.** `OR Annual Gross Income Calculation - BIA` requires
   non-NULL `N_NET_INTEREST_INCOME` and `N_NET_NON_INT_INCOME` post-CCY
   conversion. If either is NULL (driven by step 2 or by missing staging
   columns), AGI is NULL and the charge can't be computed.

5. **Insufficient years of history for the 3-yr window.** The DT
   `OPS_RISK_CAPITAL_CHARGE_CSTM` requires at least one positive-AGI year
   in the trailing 3 (per Basel: charge is undefined if all 3 years are
   non-positive). Newly onboarded entities or those with only one historical
   year may legitimately produce NULL. Check
   `SELECT D_FINANCIAL_YEAR, N_ANNUAL_GROSS_INCOME FROM FCT_OPS_RISK_DATA
   WHERE n_entity_skey = ... ORDER BY D_FINANCIAL_YEAR`.

6. **Run / date scope mismatch.** The whole pipeline filters on
   `n_run_skey = '870'` and
   `DIM_DATES.D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD')` (these are
   bind variables in OFSAA's run-time wrapper but appear as literals in the
   compiled rule SQL we see). If the user is reading the results under a
   different run skey, they'll see NULL.

**Quick triage SQL:**

```sql
SELECT n_run_skey, n_mis_date_skey, d_financial_year,
       n_alpha_percent, n_exchange_rate, n_shareholding_percent,
       n_basel_method_skey,
       n_net_interest_income_ncy,  n_net_interest_income,
       n_net_non_int_income_ncy,    n_net_non_int_income,
       n_annual_gross_income, n_capital_charge, n_rwa_amt
  FROM FCT_OPS_RISK_DATA
 WHERE n_entity_skey = :entity
   AND n_run_skey    = '870'
 ORDER BY d_financial_year;
```

The first column from the right that is NULL points at the failing stage.

---

## How to score

For each prompt, decide Pass / Partial / Fail, then aggregate:

| #  | Topic                                  | Difficulty |
| -- | -------------------------------------- | ---------- |
| 1  | Alpha factor / methodology marker      | Easy       |
| 2  | Read/write inventory                   | Easy       |
| 3  | Single-column lineage with two transforms | Medium  |
| 4  | Capital-charge formula (BIA)           | Medium     |
| 5  | Methodology dimension role             | Medium     |
| 6  | End-to-end trace, 11 steps             | Hard       |
| 7  | Diagnostic / failure-mode reasoning    | Hard       |

Common RTIE failure modes to watch for, based on the answer it gave you on
the capital-charge question:

- **Schema confusion** — placing functions in `OFSDMINFO` when they live in
  OFSERM. Will likely repeat in prompts 2, 6, 7.
- **Name-similarity hallucination** — pulling in unrelated `CS_*` capital
  functions because they share the word "capital". Watch prompts 4, 6.
- **Direction-of-flow inversion** — treating a write target as a read source
  (`FCT_STANDARD_ACCT_HEAD` → input). Watch prompt 6.
- **Missing the regulatory formula** — describing data movement without ever
  stating the BIA formula. Watch prompts 4, 6.
- **Inventing temporal gates** — "December only", quarter-only, etc. Watch
  prompts 1, 2, 7.
