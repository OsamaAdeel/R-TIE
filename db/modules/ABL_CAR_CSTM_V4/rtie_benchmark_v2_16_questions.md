# RTIE benchmark v2 — 16 questions across 4 categories

**Scope:** ABL_CAR_CSTM_V4 batch in OFSERM. Functions taken from across the
manifest (Capital Structure, Mitigants, Ops Risk, etc.) so we exercise
different code patterns.

**Workflow:** paste each *Prompt* into RTIE verbatim, save the response,
score against the ground truth using the "Distinguishing markers" list.

**Format per question:**
- **Prompt** — exact text to paste into RTIE
- **Ground truth** — answer with function names, line numbers, excerpts
- **Difficulty** — Easy / Medium / Hard
- **Distinguishing markers** — what a correct response must contain
  (and what a fabricated one will likely get wrong)

---

# Category A — Function-name questions

Format: "How does FN_X work?" — table names deliberately not given. RTIE
must figure out what objects each function reads/writes from the function
name alone.

---

## A1 — Simple

**Prompt**
> How does `ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM` work?

**Ground truth**

It is a single-statement function: `INSERT /*+APPEND*/ INTO FSI_RW_MAP_MASTER (...)`.

The SELECT is a 4-way Cartesian product (no join predicates between the
tables) of `DIM_BASEL_ASSET_CLASS`, `DIM_BASEL_CREDIT_RATING`, `DIM_DATES`
and an inline `(SELECT 'L' F_ST_LT_RATING_FLAG FROM sys.DUAL)`. The only
filter is `DIM_DATES.D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD')`, so
the result is "every (asset_class, rating) pair for the run MIS date,
flagged as long-term".

Inserted column expressions:
- `N_BASEL_TYPE_SKEY` ← `TO_CHAR(DIM_BASEL_ASSET_CLASS.N_BASEL_ASSET_CLASS_SKEY)`
- `N_BASEL_RATING` ← `TO_CHAR(DIM_BASEL_CREDIT_RATING.N_BASEL_RATING)`
- `N_MIS_DATE_SKEY` ← `TO_CHAR(DIM_DATES.N_DATE_SKEY)`
- `F_BASEL_TYPE_IND` ← literal `'A'`
- `F_ST_LT_RATING_FLAG` ← literal `'L'` (from the DUAL subquery)
- `N_RUN_SKEY` ← literal `'870'`

Error handling: `LOG ERRORS INTO FSI_RW_MAP_MASTER$ (...) REJECT LIMIT 50`,
the loop returns `'OK'` on success or `'FAIL: <SQLERRM>'` on exception.

**Source:** `functions/ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM.sql`.

**Difficulty:** Easy

**Distinguishing markers:**
- Must name the target as `FSI_RW_MAP_MASTER` (single INSERT target).
- Must identify the literal `'L'` for `F_ST_LT_RATING_FLAG` (not a column lookup).
- Must NOT claim the function applies risk weights — it only seeds an empty
  cross-join of asset class × rating; the actual risk-weight values are
  populated by other rules later.

---

## A2 — Medium

**Prompt**
> How does `CS_Goodwill_Calculation` work?

**Ground truth**

Single MERGE into `FCT_STANDARD_ACCT_HEAD`, restricted to rows where
`DIM_STANDARD_ACCT_HEAD.V_STD_ACCT_HEAD_ID = 'CAP012'` (Goodwill standard
account head).

The merge match keys are `(N_RUN_SKEY, N_STD_ACCT_HEAD_SKEY,
N_MIS_DATE_SKEY, N_ENTITY, N_CAP_COMP_GROUP_SKEY, N_GAAP_SKEY,
N_FORECAST_DATE_SKEY)`.

The UPDATE sets `TT.N_STD_ACCT_HEAD_AMT` from a CASE:
- `COND_10 = 10` always (the WHERE clause filters to CAP012, so the
  inner-CASE WHEN ... = 'CAP012' is always true).
- `EXP_10 = MAX(coalesce(... 'CAP1506' amt, 0)) + MAX(coalesce(... 'CAP1507' amt, 0))`,
  joined via a self-join named `CAPITAL_ACCOUNTING` on
  `(run, mis_date, gaap, forecast_date)`.
- `EXP_11 = MIN(N_STD_ACCT_HEAD_AMT)` — dead code (unreachable given the
  WHERE filter), inherited from OFSAA's auto-generated CASE-with-fallback.

Run/date scope: `DIM_DATES.D_CALENDAR_DATE = TO_DATE('20260331')` and
`DIM_RUN.N_RUN_SKEY = '870'`.

**Business meaning:** Goodwill (CAP012) = standalone Goodwill (CAP1506) +
Investment Goodwill (CAP1507). It's a sum, computed from the same fact
table self-joined on (run, mis_date, gaap, forecast_date).

**Source:** `functions/CS_Goodwill_Calculation.sql`.

**Difficulty:** Medium

**Distinguishing markers:**
- Must identify CAP012 as the target std_acct_head_id.
- Must state that the formula is `CAP1506 + CAP1507` (sum, not subtraction
  or any threshold logic).
- Must note that the source data comes from `FCT_STANDARD_ACCT_HEAD`
  itself via a self-join (aliased `CAPITAL_ACCOUNTING`) — not from a
  separate goodwill table.
- A correct answer may flag the dead `ELSE` branch; a hallucinating
  answer will treat it as live.

---

## A3 — Medium

**Prompt**
> How does `Cap_Consl_Effective_Shareholding_Percent_for_an_Entity_Based_on_Consolidation_Approach` work?

**Ground truth**

Single MERGE into `FCT_ENTITY_INFO`. It overrides `N_SHAREHOLDING_PERCENT`
to **1** for entities meeting any of three OR conditions:

```sql
COND_10 = 10 WHEN
  ( DIM_BASEL_CAP_CONSL_APPR.V_BASEL_CAP_CONSL_APPR_CODE = 'FAGR'
    AND COALESCE(FCT_ENTITY_INFO.f_regulatory_entity_ind,'Y') = 'Y' )
  OR
  ( DIM_BASEL_CAP_CONSL_APPR.V_BASEL_CAP_CONSL_APPR_CODE = 'FLCS'
    AND COALESCE(FCT_ENTITY_INFO.f_regulatory_entity_ind,'Y') = 'Y' )
  OR
  ( 1=1 AND COALESCE(FCT_ENTITY_INFO.f_regulatory_entity_ind,'Y') = 'N' )
```

The same condition appears in the WHERE clause, so only entities matching
one of those branches enter the MERGE source — the `EXP_11` (= existing
N_SHAREHOLDING_PERCENT) branch is unreachable.

In words:
- Regulatory entities (`f_regulatory_entity_ind = 'Y'` or NULL) under
  **Full Aggregation (FAGR)** → 100%
- Regulatory entities under **Full Consolidation (FLCS)** → 100%
- **Non-regulatory entities** (`f_regulatory_entity_ind = 'N'`) → 100%
  regardless of approach

**Match keys:** `(N_RUN_SKEY, N_ENTITY_SKEY, N_MIS_DATE_SKEY,
N_FORECAST_DATE_SKEY)`.

**Run/date scope:** `D_CALENDAR_DATE = TO_DATE('20260331')`, `N_RUN_SKEY = '870'`.

**Business meaning:** Under FAGR or FLCS, the parent fully consolidates the
subsidiary's balance sheet — pro-rata adjustment is inappropriate, so the
effective shareholding for ops-risk / RWA arithmetic is 100%. Non-regulatory
entities also default to 100% so they don't shrink in pro-rata calculations.

**Source:** `functions/Cap_Consl_Effective_Shareholding_Percent_for_an_Entity_Based_on_Consolidation_Approach.sql`.

**Difficulty:** Medium

**Distinguishing markers:**
- Must list all three OR-branches (FAGR + reg=Y, FLCS + reg=Y, reg=N).
- Must state the assigned value is the literal **1** (`EXP_10 = (1)`), not
  a column lookup.
- Must note this function does NOT touch entities under PRCS (Pro-rata) —
  for those, the original `N_SHAREHOLDING_PERCENT` survives untouched.
- A common hallucination: treating the function as a generic
  "shareholding %" calculator that handles all approaches. It only forces
  100% for the three listed cases.

---

## A4 — Long / multi-step

**Prompt**
> How does `FN_LOAD_OPS_RISK_DATA` work?

**Ground truth**

A multi-statement procedural function (~360 lines) in OFSERM. Returns 1 on
success, 0 on exception. Signature: `FN_LOAD_OPS_RISK_DATA(P_V_BATCH_ID
VARCHAR2, P_V_MIS_DATE VARCHAR2) RETURN NUMBER`.

**Step 0 — December gate (line 33):**
```sql
IF TO_NUMBER(EXTRACT(MONTH FROM TO_DATE(CQD,'DD-MON-RR'))) = 12 THEN
```
If the run MIS month isn't December, the body is skipped — the function
returns 1 and does nothing.

**Step 1 — Reset staging (line 198):** `DELETE FROM STG_OPS_RISK_DATA WHERE
FIC_MIS_DATE = CQD; COMMIT;`

**Step 2 — Load AGI from manual upload (lines 203–222):** `INSERT INTO
STG_OPS_RISK_DATA (FIC_MIS_DATE, N_ALPHA_PERCENT, V_GAAP_CODE,
D_FINANCIAL_YEAR, V_LOB_CODE, V_LV_CODE, V_CCY_CODE, V_DATA_PROCESSING_TYPE,
N_ANNUAL_GROSS_INCOME) SELECT ... FROM ABL_OPS_RISK_DATA M WHERE
M.FIC_MIS_DATE = CQD`. Per the comment at line 196 ("*NEW LOGIC OF
OPERATIONAL RISK THROUGH MANUAL FORM/EXCEL UPLOAD (3 MARCH 2026)*"), the
table `ABL_OPS_RISK_DATA` is populated by an Excel upload. Note: only 9
columns get values; NII, NNII, etc. are not loaded.

**Step 3 — Compute deduction values (lines 228–299):** five scalar locals
populated from `STG_GL_DATA` + `STG_GL_LOB_MAPPING`:
- `LN_DEDUCITON_RATIO_1 = SUM(N_AMOUNT_LCY) × MAX(N_DEDUCTION_RATIO)`
  for `V_LOB_CODE='DBS'`, `V_LV_CODE='ABL'` (lines 228–242)
- `LN_DEDUCITON_RATIO_2 = SUM(N_AMOUNT_LCY) × (1 − MAX(N_DEDUCTION_RATIO))` (same query)
- `LN_TOTAL_DEDUCT = SUM(N_AMOUNT_LCY)` (lines 278–285)
- `CBA_DEDUCTION = SUM(N_AMOUNT_ACY)` for GL codes
  `'601010601-0000'`, `'601010701-0000'`, `'601010702-0000'` (lines 270–276)
- ABLIBG variants (lines 247–264, 289–299) joined with
  `OFSERM.VW_JURISDICTION_BR_MAP`

Then (line 305):
- `TOT1 = LN_TOTAL_DEDUCT + (-1 × LN_DEDUCITON_RATIO_1)`
- `LN_SUB_TOTAL_ABLIBG = LN_TOTAL_DEDUCT_ABLIBG + (-1 × LN_DEDUCT_RATIO_ABLIBG_1)`

**Step 4 — Adjust AGI for CBA / RBA LoBs (lines 309–344):**

UPDATE 1 (non-ABLIBG, lines 309–324):
```sql
UPDATE STG_OPS_RISK_DATA OPS
   SET OPS.N_ANNUAL_GROSS_INCOME = CASE
        WHEN OPS.V_LOB_CODE = 'CBA' THEN NVL(OPS.N_ANNUAL_GROSS_INCOME + TOT1 + CBA_DEDUCTION, 0)
        WHEN OPS.V_LOB_CODE = 'RBA' THEN NVL(OPS.N_ANNUAL_GROSS_INCOME, 0) + LN_DEDUCITON_RATIO_1
       END
 WHERE OPS.FIC_MIS_DATE = CQD
   AND OPS.V_LOB_CODE IN ('CBA','RBA')
   AND OPS.V_LV_CODE <> 'ABLIBG';
```

UPDATE 2 (ABLIBG entity, lines 329–344): same shape but uses the ABLIBG
deduction locals.

**Step 5 — Exit (lines 347–352):** `END IF; CNUMBER := 1; RETURN CNUMBER;`

Exception block sets `CNUMBER := 0` and logs `LV_MESSAGE_E := CQD || ' ' || SQLERRM`.

**Source:** `user_source` for `FN_LOAD_OPS_RISK_DATA` in OFSERM (function not present in `functions/` folder).

**Difficulty:** Hard

**Distinguishing markers:**
- Must mention the **December gate at line 33** — without it, RTIE failed
  to read the function body.
- Must identify **`ABL_OPS_RISK_DATA` as the manual-upload source** (not GL
  data — though GL is used for the deduction adjustments).
- Must identify the AGI adjustments are **only for V_LOB_CODE IN ('CBA','RBA')** —
  not all LoBs.
- Must NOT claim the function calculates the operational-risk capital charge
  or RWA (those happen later in the BIA pipeline).
- Must NOT cite `CS_Capital_Buffer_Amount_Calculation` or any `CS_*` capital
  structure function as related — they are a different process.

---

# Category B — Single-function-body questions

Format: "In FN_X, what condition triggers Y?" — answers come from reading
FN_X only. Picked for interesting conditional logic, dead branches, or
non-obvious behaviour.

---

## B1 — Dead branch (high signal)

**Prompt**
> In `CS_Deferred_Tax_Asset_Net_of_DTL_Calculation`, when does the `EXP_11`
> branch fire, and what does it set?

**Ground truth**

**The `EXP_11` branch never fires.** It is dead code.

The MERGE source-side has both an inner CASE filter and a WHERE clause:

```sql
WHERE (1=1)
  AND (DIM_DATES.D_CALENDAR_DATE = TO_DATE('20260331') AND DIM_RUN.n_run_skey='870')
  AND (((DIM_STANDARD_ACCT_HEAD.V_STD_ACCT_HEAD_ID = 'CAP943')))
```

Combined with the inner CASE:

```sql
COND_1366184992216_10 = MIN(CASE WHEN
  DIM_STANDARD_ACCT_HEAD.V_STD_ACCT_HEAD_ID = 'CAP943' THEN 10 ELSE 11 END)
```

Because the WHERE clause already restricts to CAP943, every row in the
source has `COND_10 = 10`. The merge UPDATE is:

```sql
UPDATE SET TT.N_STD_ACCT_HEAD_AMT =
  CASE WHEN COND_10 = 10 THEN EXP_10 ELSE EXP_11 END
```

So the `ELSE EXP_11` arm is unreachable.

`EXP_11` is defined as `MIN(FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT)` —
i.e. it would just preserve the existing value. This is OFSAA's
auto-generated CASE-with-fallback pattern; the framework always emits both
branches even when the WHERE clause makes one impossible.

The **only** branch that ever runs is `EXP_10`:
```
EXP_10 = MAX(amt for CAP309) − MAX(amt for CAP863)
```
which is Net DTA = Gross DTA − DTL netting allowance (Basel III §69).

**Source:** `functions/CS_Deferred_Tax_Asset_Net_of_DTL_Calculation.sql`.

**Difficulty:** Medium

**Distinguishing markers:**
- Must explicitly say the branch is **dead / unreachable** because the WHERE
  clause filters to CAP943.
- Must NOT invent a business condition under which EXP_11 fires.
- A correct answer ideally cites both the WHERE clause and the inner CASE
  to justify the dead-code claim.

---

## B2 — Jurisdiction-dependent branch

**Prompt**
> Why does `CS_Goodwill_and_Other_Intangible_Assets_Net_of_DTL_Calculation`
> produce different results for Australian banks?

**Ground truth**

The CASE has three branches, ordered to match APRA-jurisdiction first:

```sql
COND_10 = MIN(CASE
  WHEN ((DIM_RUN.V_JURISDICTION_CODE = 'APRA') AND (V_STD_ACCT_HEAD_ID = 'CAP856'))
       THEN 10
  WHEN ((1=1) AND (V_STD_ACCT_HEAD_ID = 'CAP856'))
       THEN 11
  ELSE 12 END)
```

With:
- **EXP_10 (APRA)** = `MAX(CAP971 ∧ cap_comp_group='OTH') + MAX(CAP1894) + MAX(CAP1895) + MAX(CAP1896)` —
  i.e. Goodwill (in OTH group) **plus three additional intangible heads
  (CAP1894–1896)** specific to the APRA prudential standard.
- **EXP_11 (default OOTB)** = `MAX(CAP971 ∧ OTH) + MAX(CAP972 ∧ OTH)` —
  Goodwill + Other Intangibles only.
- **EXP_12** = `MIN(N_STD_ACCT_HEAD_AMT)` — dead (WHERE clause filters to CAP856).

The result CAP856 (Net Goodwill + Other Intangibles after DTL) is therefore
**bigger for APRA banks** because they add three extra intangible-asset
heads. This implements the divergence between APRA Prudential Standard
APS 111 and the OOTB Basel III treatment.

**`DIM_RUN.V_JURISDICTION_CODE = 'APRA'`** is the trigger. APRA is the
Australian Prudential Regulation Authority.

**Source:** `functions/CS_Goodwill_and_Other_Intangible_Assets_Net_of_DTL_Calculation.sql`.

**Difficulty:** Medium

**Distinguishing markers:**
- Must identify **`DIM_RUN.V_JURISDICTION_CODE = 'APRA'`** as the trigger.
- Must list the additional CAP heads in the APRA branch (CAP1894, CAP1895,
  CAP1896) — these are jurisdiction-specific intangibles.
- Must note that the OOTB branch sums CAP971 + CAP972 with a `cap_comp_group='OTH'`
  filter on each.
- The CASE order matters — APRA is checked first, so for an APRA run it
  takes EXP_10 even though the second branch's `1=1 AND CAP856` would also
  match.

---

## B3 — Conditional UPDATE in a procedural function

**Prompt**
> In `FN_LOAD_OPS_RISK_DATA`, what condition triggers the `N_ANNUAL_GROSS_INCOME`
> adjustment for `V_LOB_CODE='CBA'` on a non-ABLIBG entity, and what is added?

**Ground truth**

The UPDATE at lines 309–324 adjusts AGI:

```sql
UPDATE STG_OPS_RISK_DATA OPS
   SET OPS.N_ANNUAL_GROSS_INCOME = CASE
        WHEN OPS.V_LOB_CODE = 'CBA'
          THEN NVL(OPS.N_ANNUAL_GROSS_INCOME + TOT1 + CBA_DEDUCTION, 0)
        WHEN OPS.V_LOB_CODE = 'RBA'
          THEN NVL(OPS.N_ANNUAL_GROSS_INCOME, 0) + LN_DEDUCITON_RATIO_1
       END
 WHERE OPS.FIC_MIS_DATE = CQD
   AND OPS.V_LOB_CODE IN ('CBA','RBA')
   AND OPS.V_LV_CODE <> 'ABLIBG';
```

**Conditions for the CBA branch to fire:**
1. The function's outer December gate (`EXTRACT(MONTH FROM CQD) = 12`) at line 33.
2. `OPS.FIC_MIS_DATE = CQD`.
3. `OPS.V_LOB_CODE = 'CBA'`.
4. `OPS.V_LV_CODE <> 'ABLIBG'`.

**Amount added to AGI:** `TOT1 + CBA_DEDUCTION`, where:
- `TOT1 = LN_TOTAL_DEDUCT − LN_DEDUCITON_RATIO_1` (line 305) — the portion of
  DBS-LoB deductions not absorbed by the ratio.
- `CBA_DEDUCTION = SUM(STG_GL_DATA.N_AMOUNT_ACY)` for GL codes
  `'601010601-0000'`, `'601010701-0000'`, `'601010702-0000'` (lines 270–276),
  filtered to `V_LV_CODE='ABL'` and the current MIS date.

The `NVL(... , 0)` means a NULL existing AGI is treated as 0 before adding.

**Source:** `user_source.text` for `FN_LOAD_OPS_RISK_DATA`, lines 270–276,
305, 309–324.

**Difficulty:** Hard

**Distinguishing markers:**
- Must list all three filters: **December (outer)**, **`V_LOB_CODE='CBA'`**,
  **`V_LV_CODE <> 'ABLIBG'`**.
- Must give the formula `TOT1 + CBA_DEDUCTION` correctly.
- Must give the three GL codes for `CBA_DEDUCTION` (`601010601-0000`,
  `601010701-0000`, `601010702-0000`).
- Must NOT confuse with the second UPDATE (lines 329–344) which is for
  `V_LV_CODE = 'ABLIBG'`.

---

## B4 — Multi-branch CASE driving DENSE_RANK

**Prompt**
> In `Allocation_Rank_Assignment`, what determines whether a row's
> allocation rank is computed with the simple ordering vs. the complex
> haircut-and-fund-class ordering?

**Ground truth**

The driver is `FSI_OPTIMIZER_PROCESSING.V_EXP_MIT_POOL_CARDINALITY` — the
relationship cardinality between exposure and mitigants in the pool:

```sql
COND_10 = CASE
  WHEN V_EXP_MIT_POOL_CARDINALITY = '1-1' THEN 10
  WHEN V_EXP_MIT_POOL_CARDINALITY = '1-N' THEN 11
  ELSE 12
END
```

- **`'1-1'` (one exposure ↔ one mitigant) → simple ranking (EXP_10).**
  `DENSE_RANK() OVER (PARTITION BY n_pool_id ORDER BY <std_mitigant_type
  with UNCOV pushed to last>, F_DRAWN_UNDRAWN_IND)`. Just sort by mitigant
  type with uncovered pieces last.

- **`'1-N'` (one exposure ↔ multiple mitigants) → complex ranking (EXP_11).**
  `DENSE_RANK() OVER (PARTITION BY n_pool_id ORDER BY F_DRAWN_UNDRAWN_IND,
  COALESCE(n_risk_weight, N_CAPITAL_UL),
  <fund-class/financial-class hierarchy>,
  n_net_mitigant_value × (1 − vol_haircut − forex_haircut) × maturity_haircut DESC,
  N_MITIGANT_SKEY)`. This is the real Basel CRM optimisation logic — rank
  mitigants by drawn/undrawn status, then by RW (or unsecured capital
  charge), then by fund class (FUND BASED / NON-FUND BASED with sub-tiers),
  then by haircut-adjusted mitigant value descending (largest effective
  collateral first).

- **else (cardinality is anything other than `'1-1'` or `'1-N'`) → EXP_12,**
  which is `(FSI_OPTIMIZER_PROCESSING.N_ALLOCATION_RANK)` — i.e. preserve
  existing rank. Note: this branch is filtered OUT by the inline
  `WHERE (COND_10 <> 12)` on the source subquery, so it's effectively dead.

There is also a precondition on every row: `V_EXPOSURE_TYPE = 'NON-SEC'`
**AND** the haircut-adjusted residual factor
`(1 − vol_haircut − forex_haircut) × maturity_haircut > 0` — rows where the
mitigant's effective value would be ≤ 0 are excluded.

**Source:** `functions/Allocation_Rank_Assignment.sql`.

**Difficulty:** Hard

**Distinguishing markers:**
- Must identify **`V_EXP_MIT_POOL_CARDINALITY`** as the discriminator.
- Must distinguish `'1-1'` (simple) from `'1-N'` (complex) ordering.
- Must mention the haircut formula `(1 − vol − forex) × maturity > 0` as the
  row-filter precondition.
- Must note the `'NON-SEC'` exposure-type filter.
- A common hallucination would be to ascribe the rank to mitigant type
  alone or to risk weight alone — the actual ordering is multi-key.

---

# Category C — Column-in-function questions

Format: "How does FN_X compute COL_Y?" — column must be written by FN_X.
No cross-function chain reasoning required.

---

## C1 — Literal value

**Prompt**
> What value does `OPS_RISK_DATA_POPULATION_CSTM` assign to `N_ALPHA_PERCENT`,
> and where in the function does this happen?

**Ground truth**

Hard-coded literal **`0.15`** in the SELECT list of the single INSERT
statement, position 6 of 27 inserted columns:

```sql
INSERT /*+APPEND*/ INTO FCT_OPS_RISK_DATA(
   N_COUNTRY_SKEY, N_MIS_DATE_SKEY, N_GAAP_SKEY, N_BRANCH_SKEY, N_LOB_SKEY,
   N_ALPHA_PERCENT,                                   -- ← target
   N_EXCHANGE_RATE, N_RUN_SKEY, ...
)
SELECT ...
   to_char(DIM_LOB.N_LOB_SKEY),
   0.15,                                              -- ← assignment
   FSI_CAP_CURRENCY_CONVERSION.N_EXCHANGE_RATE,
   '870',
   ...
```

The value 0.15 is the Basel II Basic Indicator Approach **alpha factor**
(BCBS-128 §649). It signals that this fact row is BIA-bound. The column
gets the same literal value for every row inserted, regardless of entity,
LoB, or year.

**Source:** `functions/OPS_RISK_DATA_POPULATION_CSTM.sql`.

**Difficulty:** Easy

**Distinguishing markers:**
- Must give the value **0.15** explicitly.
- Must say it's a **literal** in the SELECT list, not a column lookup or a
  computed value.
- A correct answer ideally connects 0.15 to the **BIA alpha factor**.

---

## C2 — Conditional value

**Prompt**
> How does `OPS_RISK_DATA_POPULATION_CSTM` compute `N_SHAREHOLDING_PERCENT`?

**Ground truth**

A two-branch CASE expression in the SELECT list:

```sql
CASE
  WHEN DIM_BASEL_CAP_CONSL_APPR.V_BASEL_CAP_CONSL_APPR_CODE = 'PRCS'
       THEN COALESCE(FCT_ENTITY_INFO.N_SHAREHOLDING_PERCENT, 1)
  ELSE 1
END
```

Branch resolution depends on the entity's Basel capital-consolidation
approach (joined via `FCT_ENTITY_INFO.N_BASEL_CAP_CONSL_APPR_SKEY ↔
DIM_BASEL_CAP_CONSL_APPR.N_BASEL_CAP_CONSL_APPR_SKEY`):

| Approach code | Description           | Value                                                                |
| ------------- | --------------------- | -------------------------------------------------------------------- |
| `'PRCS'`      | Pro-rata Consolidation | `FCT_ENTITY_INFO.N_SHAREHOLDING_PERCENT` if non-NULL, else **1**    |
| `'FLCS'`      | Full Consolidation    | **1** (literal)                                                      |
| any other     | (filtered out by WHERE clause `V_BASEL_CAP_CONSL_APPR_CODE IN ('FLCS','PRCS')`) | n/a |

So in practice the column is **1 under FLCS** and **the entity's
shareholding-% under PRCS** (with a NULL-safe fallback of 1).

**Source:** `functions/OPS_RISK_DATA_POPULATION_CSTM.sql`.

**Difficulty:** Medium

**Distinguishing markers:**
- Must show the **CASE** with both branches.
- Must identify **`PRCS`** as the trigger for the entity-specific value
  and **`FLCS`** as the constant-1 case.
- Must mention the `COALESCE(..., 1)` — i.e. NULL shareholding under PRCS
  also defaults to 1.
- A common hallucination is to claim the value comes from a percent column
  on a dimension table — it's the FACT `FCT_ENTITY_INFO`.

---

## C3 — Cross-currency-converted column

**Prompt**
> How does `ABL_CAP_MITIGANT_DATA_POPULATION` compute `N_GROUP_ASSET_SIZE`?

**Ground truth**

The column is computed in the SELECT list of the single INSERT into
`FSI_CAP_MITIGANTS`:

```sql
FSI_CAP_PARTY_FINANCIALS.N_GROUP_ASSET_SIZE * A_FSI_CAP_CURRENCY_CONVERSION.N_EXCHANGE_RATE
```

Important nuance: there are **two aliases of `FSI_CAP_CURRENCY_CONVERSION`** in the joins:
- `FSI_CAP_CURRENCY_CONVERSION` — joined on `STG_MITIGANTS.V_CCY_CODE` (the mitigant's natural currency).
- `A_FSI_CAP_CURRENCY_CONVERSION` — joined on `FSI_CAP_PARTY_FINANCIALS.V_CCY_CODE` (the party's reporting currency for its financials).

`N_GROUP_ASSET_SIZE` uses the **`A_` alias** because group asset size is a
party-financials attribute and should be converted using the rate for the
party's reporting currency, not the mitigant's currency. The same alias
applies to `N_ISSUER_ANNUAL_SALES`. Other columns like `N_MITIGANT_VALUE`
use the non-aliased `FSI_CAP_CURRENCY_CONVERSION` (the mitigant's CCY).

If the rate row is missing (the join is LEFT OUTER on
`N_RUN_SKEY = '870' AND V_CCY_CODE = ...`), the rate is NULL and the
product is NULL.

**Source:** `functions/ABL_CAP_MITIGANT_DATA_POPULATION.sql`.

**Difficulty:** Medium

**Distinguishing markers:**
- Must identify the source as `FSI_CAP_PARTY_FINANCIALS.N_GROUP_ASSET_SIZE`
  (not `STG_MITIGANTS`).
- Must identify the multiplier as the **`A_FSI_CAP_CURRENCY_CONVERSION`** alias.
- Must mention the dual-alias pattern — that's what makes this column
  non-trivial. RTIE might say "exchange rate" but miss that it's the
  party-currency rate, not the mitigant-currency rate.

---

## C4 — Same column, different function

**Prompt**
> How does `Cap_Consl_Effective_Shareholding_Percent_for_an_Entity_Based_on_Consolidation_Approach`
> compute `N_SHAREHOLDING_PERCENT`?

**Ground truth**

The MERGE UPDATE sets `N_SHAREHOLDING_PERCENT = CASE WHEN COND_10 = 10 THEN
EXP_10 ELSE EXP_11 END` with:
- `EXP_10 = (1)` — literal **1**
- `EXP_11 = MIN(FCT_ENTITY_INFO.N_SHAREHOLDING_PERCENT)` — preserve existing

But the source-side WHERE clause filters to exactly the same condition that
makes `COND_10 = 10`, so `EXP_11` is dead code (same OFSAA pattern as the
CS_* MERGEs). In practice, every matched row gets **`N_SHAREHOLDING_PERCENT = 1`**.

The condition for matching:

```sql
( BASEL_CAP_CONSL_APPR_CODE = 'FAGR' AND COALESCE(f_regulatory_entity_ind,'Y') = 'Y' )
OR
( BASEL_CAP_CONSL_APPR_CODE = 'FLCS' AND COALESCE(f_regulatory_entity_ind,'Y') = 'Y' )
OR
( COALESCE(f_regulatory_entity_ind,'Y') = 'N' )
```

**Note:** This function and `OPS_RISK_DATA_POPULATION_CSTM` (C2) both touch
`N_SHAREHOLDING_PERCENT` but on different tables (`FCT_ENTITY_INFO` vs
`FCT_OPS_RISK_DATA`). This function runs upstream of the OPS_RISK pipeline
and overrides the entity-level shareholding to 100% for the listed cases —
which is then read by `OPS_RISK_DATA_POPULATION_CSTM` to drive its own
CASE.

**Source:** `functions/Cap_Consl_Effective_Shareholding_Percent_for_an_Entity_Based_on_Consolidation_Approach.sql`.

**Difficulty:** Medium

**Distinguishing markers:**
- Must say the assigned value is **1** (literal).
- Must list **all three OR-branches** of the trigger condition.
- Must distinguish this function (writes to `FCT_ENTITY_INFO`) from
  `OPS_RISK_DATA_POPULATION_CSTM` (writes to `FCT_OPS_RISK_DATA`). RTIE
  may conflate them since the column name is the same.

---

# Category D — Chain questions

Format: "Trace COL_Y" / "What's the full pipeline that populates COL_Y" —
multi-function reasoning, may cross subprocesses.

---

## D1 — Trace where the data is actually NULL (high-signal customisation)

**Prompt**
> Trace `N_NET_INTEREST_INCOME` from `STG_OPS_RISK_DATA` to its final
> landing in `FCT_STANDARD_ACCT_HEAD` for `V_STD_ACCT_HEAD_ID = 'CAP170'`.

**Ground truth — two-part answer**

**Part 1 — the wired path (what the SQL says).** Three writers feed
`FCT_OPS_RISK_DATA.N_NET_INTEREST_INCOME` and one downstream chain
aggregates it into CAP170:

| Stage | Writer / file | What it does |
| ----- | ------------- | ------------ |
| 1 | `OPS_RISK_DATA_POPULATION_CSTM` (file) | INSERT, copies `STG_OPS_RISK_DATA.N_NET_INTEREST_INCOME` into `FCT_OPS_RISK_DATA.N_NET_INTEREST_INCOME_NCY`. Reporting-CCY column starts NULL. |
| 2 | `OR_Operating_Income_and_Expense_Attribute_Natural_CCY_Conversion_to_Reporting_CCY` (file) | MERGE: `N_NET_INTEREST_INCOME = N_NET_INTEREST_INCOME_NCY × N_EXCHANGE_RATE`. |
| 3 | `OR_Operating_Income_and_Expense_shareholding_Percent_Multiplication` (file) | MERGE: `N_NET_INTEREST_INCOME = N_NET_INTEREST_INCOME × N_SHAREHOLDING_PERCENT`. |
| 4 | TYPE3 rule `OR Annual Gross Income Calculation - BIA` (OFSAA metadata) | Sets `N_ANNUAL_GROSS_INCOME = N_NET_INTEREST_INCOME + N_NET_NON_INT_INCOME`. |
| 5 | DT `OPS_RISK_CAPITAL_CHARGE_CSTM` | `N_CAPITAL_CHARGE = 0.15 × avg of positive AGI over trailing 3 years`. |
| 6 | TYPE3 `Operational RWA Calculation` | `N_RWA_AMT = N_CAPITAL_CHARGE × 12.5`. |
| 7 | `OPS_RISK_SUMMARY_POPULATION` (file) | `INSERT ... SUM(N_RWA_AMT)` into `FCT_OPS_RISK_SUMMARY`, filtered to `N_RWA_AMT IS NOT NULL`. |
| 8 | `OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP` (file) | `INSERT ... SUM(FCT_OPS_RISK_SUMMARY.N_RWA_AMT)` into `FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT` for `V_STD_ACCT_HEAD_ID = 'CAP170'`, capital-comp-group `'OTH'`. |

**Part 2 — what the live data shows (the customisation gotcha).** In
this installation `STG_OPS_RISK_DATA.N_NET_INTEREST_INCOME` is NULL for
all 77 staging rows, and `FCT_OPS_RISK_DATA.N_NET_INTEREST_INCOME` is NULL
for all 57 fact rows. Reason: `FN_LOAD_OPS_RISK_DATA` (the upstream
loader) only populates `N_ANNUAL_GROSS_INCOME` from a manual Excel
upload (`ABL_OPS_RISK_DATA`); it never sets NII or NNII on the staging
table. So **stages 1–4 above are syntactically wired but never produce
non-NULL values**. The actual contribution to CAP170 in this installation
flows via `N_ANNUAL_GROSS_INCOME` directly — see D2.

**Sources:** `OPS_RISK_DATA_POPULATION_CSTM.sql`,
`OR_Operating_Income_and_Expense_*` files, `OPS_RISK_SUMMARY_POPULATION.sql`,
`OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP.sql`, `FN_LOAD_OPS_RISK_DATA` source,
live counts on `STG_OPS_RISK_DATA` (77/77 NII NULL) and
`FCT_OPS_RISK_DATA` (57/57 NII NULL).

**Difficulty:** Hard

**Distinguishing markers:**
- A correct answer **flags the customisation**: the wired chain doesn't
  carry a non-NULL value because the staging column is never populated.
- Must list at least the three direct writers in `FCT_OPS_RISK_DATA`
  (initial load + CCY conversion + shareholding multiplication) before
  it touches AGI.
- Must mention the SUM aggregation at stages 7 and 8.
- Must end at `V_STD_ACCT_HEAD_ID = 'CAP170'` with the cap-comp-group
  `'OTH'` filter.
- A fabricated answer will likely give the OOTB textbook chain without
  flagging the live-data NULL.

---

## D2 — The actual populated path

**Prompt**
> What is the full pipeline that populates `N_ANNUAL_GROSS_INCOME` for
> ABL Pakistan entities ending up in `FCT_OPS_RISK_DATA`?

**Ground truth**

Five steps. The path is *customised* — it bypasses the OOTB OFSAA
NII+NNII formula by pre-computing AGI upstream in PL/SQL and loading it
as a single column.

1. **Manual Excel upload → `ABL_OPS_RISK_DATA`.** Operational-risk
   officers maintain per-LoB / per-financial-year AGI in an upload form;
   that populates the table `ABL_OPS_RISK_DATA` for the current MIS date.
2. **`FN_LOAD_OPS_RISK_DATA` (December only, line 33 gate).**
   - Lines 198–199: `DELETE FROM STG_OPS_RISK_DATA WHERE FIC_MIS_DATE = CQD;`
   - Lines 203–222: `INSERT INTO STG_OPS_RISK_DATA (..., N_ANNUAL_GROSS_INCOME) SELECT ..., TO_NUMBER(N_ANNUAL_GROSS_INCOME) FROM ABL_OPS_RISK_DATA WHERE FIC_MIS_DATE = CQD;`
3. **GL deduction adjustment (still in `FN_LOAD_OPS_RISK_DATA`).** Lines
   228–305 compute scalar deductions from `STG_GL_DATA` +
   `STG_GL_LOB_MAPPING`. Then UPDATE 1 (lines 309–324, non-ABLIBG):
   - For `V_LOB_CODE='CBA'`: AGI += `TOT1 + CBA_DEDUCTION`
   - For `V_LOB_CODE='RBA'`: AGI += `LN_DEDUCITON_RATIO_1`
   UPDATE 2 (lines 329–344, ABLIBG): same pattern with ABLIBG variants.
4. **`OPS_RISK_DATA_POPULATION_CSTM`.** Inserts to `FCT_OPS_RISK_DATA`,
   filtered to `STG_OPS_RISK_DATA.V_LOB_CODE='ABLOR'`. Note: in the
   `INSERT` column list, **`N_ANNUAL_GROSS_INCOME` is not present** — the
   value is not transferred at this step.
5. **TYPE3 rule `OR Annual Gross Income Calculation - Basic Indicator
   Approach`.** Registered in OFSAA's metadata (file not in `functions/`),
   this rule sets `FCT_OPS_RISK_DATA.N_ANNUAL_GROSS_INCOME` for the
   current run. In the OOTB OFSAA template the formula is `NII + NNII`;
   in this customised setup, since NII/NNII are NULL, the rule effectively
   functions as a passthrough copy from `STG_OPS_RISK_DATA.N_ANNUAL_GROSS_INCOME`
   joined back via the entity / LoB / financial-year keys.

Live data confirms: `STG_OPS_RISK_DATA.N_ANNUAL_GROSS_INCOME` is filled in
77/77 rows; `FCT_OPS_RISK_DATA.N_ANNUAL_GROSS_INCOME` in 57/57 rows.

**Sources:** `FN_LOAD_OPS_RISK_DATA` (DB),
`OPS_RISK_DATA_POPULATION_CSTM.sql`, manifest task `OR Annual Gross Income
Calculation - Basic Indicator Approach`.

**Difficulty:** Hard

**Distinguishing markers:**
- Must identify **`ABL_OPS_RISK_DATA`** as the originating source (manual upload).
- Must mention the **December gate** of FN_LOAD_OPS_RISK_DATA.
- Must mention the CBA / RBA UPDATE adjustments — without them, the
  pipeline description is incomplete.
- Must NOT claim AGI is computed from NII+NNII in this batch (it can
  mention OOTB OFSAA as a contrast).

---

## D3 — Trace a column written by multiple functions

**Prompt**
> Trace how `N_SHAREHOLDING_PERCENT` is set across the OPS_RISK_PROCESSING
> flow. Which functions read it, which write it, and how?

**Ground truth**

The column appears in two tables — and the ops-risk flow uses both.

**Stage 1 — write to `FCT_ENTITY_INFO`:** `Cap_Consl_Effective_Shareholding_Percent_for_an_Entity_Based_on_Consolidation_Approach`
runs in the upstream Capital Structure setup. It MERGEs over
`FCT_ENTITY_INFO` setting `N_SHAREHOLDING_PERCENT = 1` for entities under
FAGR, FLCS, or non-regulatory entities (see C4). For PRCS regulatory
entities, the column is **untouched** (whatever was loaded from STG
remains).

**Stage 2 — read from `FCT_ENTITY_INFO`, write to `FCT_OPS_RISK_DATA`:**
`OPS_RISK_DATA_POPULATION_CSTM` reads `FCT_ENTITY_INFO.N_SHAREHOLDING_PERCENT`
and writes `FCT_OPS_RISK_DATA.N_SHAREHOLDING_PERCENT` via the CASE in C2:
```
CASE WHEN APPR='PRCS' THEN COALESCE(FCT_ENTITY_INFO.N_SHAREHOLDING_PERCENT, 1) ELSE 1 END
```
So:
- FLCS entity → 1 (regardless of what stage 1 set)
- PRCS entity (regulatory) → whatever stage 1 left in `FCT_ENTITY_INFO`
- PRCS entity (non-regulatory) → 1 (because stage 1 forced it to 1)

**Stage 3 — read from `FCT_OPS_RISK_DATA`, no writes:** Each of the 9 ops-risk
TYPE3 MERGE files (the four `*_shareholding_Percent_Multiplication.sql` and
the five `*_Natural_CCY_Conversion_to_Reporting_CCY.sql`) selects
`N_SHAREHOLDING_PERCENT` for joining and conditional logic, but only the
shareholding-multiplication ones use it as a multiplier:
- `OR_Balance_sheet_Attribute_Natural_CCY_Conversion_to_Reporting_CCY.sql`,
  `OR_Operating_Income_and_Expense_Attribute_Natural_CCY_Conversion_to_Reporting_CCY.sql`,
  `OR_Non_Operating_Income_and_Expense_Attribute_Natural_CCY_Conversion_to_Reporting_CCY.sql`,
  `OR_Other_Income_and_Expense_Attribute_Natural_CCY_Conversion_to_Reporting_CCY.sql`,
  `OR_Provisioning_Attribute_Natural_CCY_Conversion_to_Reporting_CCY.sql` —
  reference but do not multiply
- `Shareholding_Percent_Multiplication_of_Balance_sheet_Attribute_for_Operational_Risk.sql`,
  `OR_Operating_Income_and_Expense_shareholding_Percent_Multiplication.sql`,
  `OR_Non_Operating_Income_and_Expense_shareholding_Percent_Multiplication.sql`,
  `OR_Other_Income_and_Expense_shareholding_Percent_Multiplication.sql` —
  multiply income/expense columns by `N_SHAREHOLDING_PERCENT`

**Net effect:**
- Under FLCS: every multiplication is `× 1` (no-op).
- Under PRCS reg: the entity's true ownership % shrinks the gross-income
  components.
- Under PRCS non-reg: forced to 1 by stage 1, so multiplication is still
  `× 1`.

**Sources:** the `Cap_Consl_*` file, `OPS_RISK_DATA_POPULATION_CSTM.sql`,
the four `*_shareholding_Percent_Multiplication*.sql` files.

**Difficulty:** Hard

**Distinguishing markers:**
- Must distinguish the **two tables** (`FCT_ENTITY_INFO` and
  `FCT_OPS_RISK_DATA`) — both are involved.
- Must mention the upstream `Cap_Consl_*` writer that overrides to 1 for
  FAGR/FLCS/non-reg.
- Must list at least one of the four `*_shareholding_Percent_Multiplication*`
  consumers.
- Must note that under FLCS the downstream multiplications are no-ops.

---

## D4 — Multi-source aggregation into CAP170

**Prompt**
> What is the full pipeline that produces `N_STD_ACCT_HEAD_AMT` for
> `V_STD_ACCT_HEAD_ID = 'CAP170'` in `FCT_STANDARD_ACCT_HEAD`?

**Ground truth**

CAP170 is the standard account head ID for **Operational Risk RWA**. It is
populated by **one writer**: `OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP.sql`.

Looking inside that file, the SELECT computes:

```sql
CASE WHEN DIM_RUN.V_PRODUCT = 'CREC'
     THEN STG_STANDARD_ACCT_HEAD.N_AMOUNT_RCY
     ELSE SUM(COALESCE(FCT_OPS_RISK_SUMMARY.N_RWA_AMT, 0))
END
```

So there are two upstream paths depending on the run product:

**Path A — `DIM_RUN.V_PRODUCT = 'CREC'` (Capital Reporting):** the value
is read directly from `STG_STANDARD_ACCT_HEAD.N_AMOUNT_RCY` for
`V_STD_ACCT_HEAD_ID = 'CAP170'`. This is for runs that ingest pre-computed
RWA from an external source rather than calculating it.

**Path B — non-CREC runs:** the value is `SUM(FCT_OPS_RISK_SUMMARY.N_RWA_AMT)`.
Tracing backward:

1. `FN_LOAD_OPS_RISK_DATA` → `STG_OPS_RISK_DATA.N_ANNUAL_GROSS_INCOME`
   (manual Excel upload + GL deduction adjustments — D2).
2. `OPS_RISK_DATA_POPULATION_CSTM` → inserts `FCT_OPS_RISK_DATA` (sets
   alpha=0.15, exchange rate, shareholding %, copies _NCY columns).
3. CCY-conversion + shareholding-% TYPE3 rules → enrich `FCT_OPS_RISK_DATA`.
4. TYPE3 `OR Annual Gross Income Calculation - BIA` → sets
   `FCT_OPS_RISK_DATA.N_ANNUAL_GROSS_INCOME`.
5. DT `OPS_RISK_CAPITAL_CHARGE_CSTM` → `FCT_OPS_RISK_DATA.N_CAPITAL_CHARGE
   = 0.15 × 3-yr-avg of positive AGI`.
6. TYPE3 `Operational RWA Calculation` → `FCT_OPS_RISK_DATA.N_RWA_AMT =
   N_CAPITAL_CHARGE × 12.5`.
7. `OPS_RISK_SUMMARY_POPULATION` → aggregates step 6 into
   `FCT_OPS_RISK_SUMMARY.N_RWA_AMT`, GROUP BY `(run, mis_date, gaap, country, branch)`,
   filter `N_RWA_AMT IS NOT NULL`.
8. `OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP` → inserts CAP170 row in
   `FCT_STANDARD_ACCT_HEAD` with `cap_comp_group_skey` for code `'OTH'`,
   `N_STD_ACCT_HEAD_AMT = SUM(step 7)`.

**Live verification:** for run 818 / 2025-12-31 in this DB,
`FCT_STANDARD_ACCT_HEAD.N_STD_ACCT_HEAD_AMT` for CAP170 = **254,828,051,250**.

**Sources:** all functions named above plus `STG_STANDARD_ACCT_HEAD`
(if Path A), live row in `FCT_STANDARD_ACCT_HEAD`.

**Difficulty:** Hard

**Distinguishing markers:**
- Must identify **`OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP`** as the single
  writer to CAP170.
- Must surface the **two-branch CASE** (CREC vs non-CREC).
- Must list at least the BIA chain (FN_LOAD → STG → FCT_OPS_RISK_DATA →
  charge → ×12.5 → SUMMARY → CAP170).
- Must mention the `cap_comp_group = 'OTH'` filter in the final write.
- Must NOT cite `CS_Capital_Buffer_Amount_Calculation` /
  `CS_Net_Tier_2_Capital_Calculation` / `CS_Total_Eligible_Capital` as
  contributors to CAP170 — those write to capital-structure heads
  (CAP832-840 etc.), not CAP170.

---

# Scoring summary

| Cat | # | Topic | Difficulty | Key gotcha |
| --- | - | ----- | ---------- | ---------- |
| A | 1 | Risk-weight map (cross-join) | Easy | Cartesian product, not risk-weight values |
| A | 2 | Goodwill (CAP012 = CAP1506+CAP1507) | Medium | Self-join on FCT_STANDARD_ACCT_HEAD |
| A | 3 | Cap-Consl shareholding override | Medium | Three OR-branches, value = literal 1 |
| A | 4 | FN_LOAD_OPS_RISK_DATA | Hard | December gate, manual upload, GL deductions |
| B | 1 | Dead branch in DTA Net of DTL | Medium | EXP_11 unreachable |
| B | 2 | APRA jurisdiction in Goodwill+Intangibles | Medium | Extra heads CAP1894-1896 |
| B | 3 | CBA conditional update in FN_LOAD | Hard | TOT1 + CBA_DEDUCTION, three GL codes |
| B | 4 | Allocation rank cardinality switch | Hard | 1-1 simple vs 1-N complex DENSE_RANK |
| C | 1 | N_ALPHA_PERCENT literal | Easy | 0.15 (BIA alpha) |
| C | 2 | N_SHAREHOLDING_PERCENT in OPS_RISK_DATA_POP | Medium | PRCS vs FLCS CASE |
| C | 3 | N_GROUP_ASSET_SIZE in mitigants | Medium | A_FSI_CAP_CURRENCY_CONVERSION alias |
| C | 4 | N_SHAREHOLDING_PERCENT in Cap_Consl_* | Medium | Same column name, different table |
| D | 1 | NII trace to CAP170 (NULL in practice) | Hard | Live data is NULL — flag customisation |
| D | 2 | AGI pipeline (manual upload path) | Hard | ABL_OPS_RISK_DATA + GL deductions |
| D | 3 | Shareholding cross-table trace | Hard | FCT_ENTITY_INFO override + downstream multiplications |
| D | 4 | CAP170 full pipeline | Hard | CREC vs non-CREC, single writer |

**High-signal questions** (correct answer needs the customisation insight):
- **A4** (must catch December gate)
- **B1** (must catch dead branch)
- **D1** (must catch NULL data)
- **D2** (must identify manual upload path)
- **D4** (must NOT cite CS_* capital structure functions)

After RTIE answers, paste the responses back and we'll diff against the
ground truth.
