# W88 Diagnostic: Named regulatory computation pre-router

**Branch:** docs/w88-diagnostic-named-computations
**Date:** 2026-05-12
**Investigator:** OFSERM DDL + manifest inspection, direct Oracle probes via SqlGuardian, /v1/stream classifier-recognition probes (no production-code changes)

---

## 0. Pre-diagnostic note: stakeholder test artifact

The prompt references `scratch/stakeholder_test_1_*.md` — no file by that name (or any path matching `stakeholder*`, `BIA*`, `cowork*`) exists in this checkout as of 2026-05-12. The cowork-vs-RTIE BIA evidence the prompt invokes (capital charge 20,386,244,100; RWA 254,828,051,250) is, however, present and reproducible in two places:

- [db/modules/ABL_CAR_CSTM_V4/rtie_benchmark_ops_risk_processing.md](db/modules/ABL_CAR_CSTM_V4/rtie_benchmark_ops_risk_processing.md) — ground-truth prompts and BIA formula.
- [db/modules/ABL_CAR_CSTM_V4/rtie_benchmark_v2_16_questions.md:906](db/modules/ABL_CAR_CSTM_V4/rtie_benchmark_v2_16_questions.md#L906) — CAP170 = 254,828,051,250 confirmed.

And the BIA values are directly observable in the local Oracle:

```text
OFSERM.FCT_OPS_RISK_DATA  N_BASEL_METHOD_SKEY=115  (one of 5 sampled rows)
  N_CAPITAL_CHARGE     = 20,386,244,100
  N_RWA_AMT            = 254,828,051,250
  N_ANNUAL_GROSS_INCOME = 130,717,474,000
```

So the diagnostic does not depend on the missing stakeholder file — the BIA reachability is confirmed empirically below.

---

## 1. Inventory of named regulatory computations

15 items in scope (the 14 from the prompt plus AMA was already listed):

| # | Computation | Acronym |
|---|---|---|
| 1 | Basic Indicator Approach | BIA |
| 2 | Standardised Approach (op risk) | TSA / STD |
| 3 | Alternative Standardised Approach | ASA |
| 4 | Advanced Measurement Approach | AMA |
| 5 | Credit Risk RWA — Standardised | CR-SA |
| 6 | Credit Risk RWA — IRB Foundation | CR-IRB-F |
| 7 | Credit Risk RWA — IRB Advanced | CR-IRB-A |
| 8 | Market Risk RWA — Standardised | MR-STD |
| 9 | Market Risk RWA — Internal Models (VaR-based) | MR-IM |
| 10 | CET1 capital ratio | CET1 |
| 11 | Tier 1 capital ratio | T1 |
| 12 | Total Capital ratio (Capital Adequacy Ratio) | CAR |
| 13 | Leverage Ratio | LR |
| 14 | Liquidity Coverage Ratio | LCR |
| 15 | Net Stable Funding Ratio | NSFR |

The inventory was constructed from `RTIE/db/modules/ABL_CAR_CSTM_V4/manifest.yaml`, the OFSERM DDL ([db/schemas/OFSERM/create_tables.sql](db/schemas/OFSERM/create_tables.sql)), and the loader functions ([db/modules/ABL_CAR_CSTM_V4/functions/](db/modules/ABL_CAR_CSTM_V4/functions/)). No additional named computations beyond the prompt's list were discovered that warrant in-scope status — the manifest tasks beyond these are intermediate transformations (provisions, mitigants, CCY conversions), not stakeholder-facing regulatory outputs.

---

## 2. Per-computation findings

### Reference: methodology-skey mapping (DIM_BASEL_METHODOLOGY)

From `OFSERM.DIM_BASEL_METHODOLOGY WHERE F_LATEST_RECORD_INDICATOR='Y'` (143 distinct latest-record methods total; the ones in scope here):

| SKEY | Code | Description | Risk type |
|---|---|---|---|
| 16  | ORAMA  | Operational Risk - AMA Approach | OR |
| 36  | ORSASA | Operational Risk - Simplified Alternative Standardised Approach | OR |
| 77  | ORSTD  | Operational Risk - Standardized Approach | OR |
| 115 | ORBIA  | Operational Risk - Basic Indicator Approach | OR |
| 94  | MRSTD   | Market Risk - Standardized Approach | MR |
| 106 | MRIM    | Market Risk - Internal Model Approach | MR |
| 132 | MRSTDSM | Market Risk - Standardized - Simplified | MR |
| 133 | MRSTDSCM | Market Risk - Standardized - Scenario Matrix | MR |
| 135 | MRSTDDP | Market Risk - Standardized - Delta plus | MR |
| 1–48 | (various CR codes) | Credit-risk variants (NSFIRB, NSAIRB, SECSTD, SECIRB etc.) | CR |

Full dump in [scratch/w88_probes/oracle_supp.json](scratch/w88_probes/oracle_supp.json) (`op_risk_methods`, `mkt_risk_methods`).

### Reference: CAP-code map for FCT_STANDARD_ACCT_HEAD

Capital ratios and aggregated RWA totals live in `OFSERM.FCT_STANDARD_ACCT_HEAD`, scoped by `DIM_STANDARD_ACCT_HEAD.V_STD_ACCT_HEAD_ID`. Probed CAP-codes (108 rows total in the current run; [scratch/w88_probes/oracle_supp.json](scratch/w88_probes/oracle_supp.json) `all_caps_in_fact`):

| CAP code | Description | Current run value | Source loader |
|---|---|---|---|
| CAP090 | Market Risk RWA | 61,175,589,179 | [MKT_RISK_RWA_STD_ACCT_HEAD_DATA_POP.sql](db/modules/ABL_CAR_CSTM_V4/functions/MKT_RISK_RWA_STD_ACCT_HEAD_DATA_POP.sql) |
| CAP169 | Credit RWA | **658,963,154,349** | (not in functions/ folder; populated via OFSAA T2T) |
| CAP170 | Operational RWA | 254,828,051,250 | [OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP.sql](db/modules/ABL_CAR_CSTM_V4/functions/OPS_RISK_RWA_STD_ACCT_HEAD_DATA_POP.sql) |
| CAP192 | Total Capital Ratio (CAR) | **0.26289311967** | [CS_Capital_Ratio.sql](db/modules/ABL_CAR_CSTM_V4/functions/CS_Capital_Ratio.sql) |
| CAP210 | Total eligible capital | 256,312,062,250 | [CS_Total_Eligible_Capital.sql](db/modules/ABL_CAR_CSTM_V4/functions/CS_Total_Eligible_Capital.sql) |
| CAP214 | Tier 1 Capital Ratio | **0.19286686188** | [CS_Tier_1_Capital_Ratio.sql](db/modules/ABL_CAR_CSTM_V4/functions/CS_Tier_1_Capital_Ratio.sql) |
| CAP838 | Total RWA | 974,966,794,778 | (aggregate of CAP090 + CAP169 + CAP170 + CAP959) |
| CAP841 | Net Common Equity Tier 1 Capital | 188,038,786,146 | [CS_Net_Common_Equity_Tier_1_Capital.sql](db/modules/ABL_CAR_CSTM_V4/functions/CS_Net_Common_Equity_Tier_1_Capital.sql) |
| CAP843 | Leverage Ratio | 0.0 (placeholder, not computed) | (no loader present) |
| CAP935 | Tier 2 Capital Ratio | (absent from fact in this run) | [CS_Tier_2_Capital_Ratio.sql](db/modules/ABL_CAR_CSTM_V4/functions/CS_Tier_2_Capital_Ratio.sql) |
| CAP959 | Regulatory Adjustments Phase-In RWA | 419,940,000 | [REG_ADJUSTMENT_RWA_STD_ACCT_HEAD_DATA_POP.sql](db/modules/ABL_CAR_CSTM_V4/functions/REG_ADJUSTMENT_RWA_STD_ACCT_HEAD_DATA_POP.sql) |
| CAP960 | CET1 Capital Ratio | **0.19286686188** | [CS_Common_Equity_Tier_1_Capital_ratio.sql](db/modules/ABL_CAR_CSTM_V4/functions/CS_Common_Equity_Tier_1_Capital_ratio.sql) |
| CAP1807-CAP1816 | Leverage exposure components | all 0.0 | (no loader in this module) |
| CAP1817 | Minimum Leverage Ratio (threshold) | 0.03 (3%) | seed |

Note: CAP-code is the discriminator for capital ratios and total RWA — `N_BASEL_METHOD_SKEY` is *not* used on FCT_STANDARD_ACCT_HEAD; that's an OPS / MR fact-table dimension.

---

### 1. Basic Indicator Approach (BIA)

**Canonical fact table candidate(s):**
- `OFSERM.FCT_OPS_RISK_DATA` (rank 1; sole candidate). Probed: 57 rows, all `N_BASEL_METHOD_SKEY=115`.
- Aggregated downstream into `OFSERM.FCT_OPS_RISK_SUMMARY` (223 rows; no methodology column at the per-entity level — only `N_BASEL_METHOD_SKEY` carried through).
- Final landing for the aggregated RWA: `OFSERM.FCT_STANDARD_ACCT_HEAD` row where `V_STD_ACCT_HEAD_ID='CAP170'` (Operational RWA total).

**Methodology filter:** `OFSERM.FCT_OPS_RISK_DATA.N_BASEL_METHOD_SKEY = 115` (= `OFSERM.DIM_BASEL_METHODOLOGY` row where `V_BASEL_METHOD_CODE='ORBIA'`). Confirmed in [scratch/w88_probes/oracle_supp.json](scratch/w88_probes/oracle_supp.json) → `method_115`.

**Result columns:**
- `N_CAPITAL_CHARGE` (Basel II §649 — α × 3-yr avg of positive AGI), unit: reporting CCY (PKR in the present run).
- `N_RWA_AMT` (= N_CAPITAL_CHARGE × 12.5), same unit.
- `N_ANNUAL_GROSS_INCOME` (per-year input; not the user-facing "BIA result" but routinely asked).

**Required additional filters:**
- `N_RUN_SKEY` (joined to `DIM_RUN`). Sample run skeys seen in BIA rows: 812, 818, 840.
- `N_MIS_DATE_SKEY` (joined to `DIM_DATES.N_DATE_SKEY`). Recent value in fact: `20251231`. (Note: this is **not** 20260331 — see Anything-unexpected Section 7.)
- `N_ENTITY_SKEY` (single entity per row), `N_LOB_SKEY`, `N_GAAP_SKEY`. Currency in `V_CCY_CODE`.
- Sensible defaults if unspecified: latest `N_MIS_DATE_SKEY` per run, sum across all entities, base currency from `RUN_PARAMETERS`.

**Reachability check:** Fully reachable. From [scratch/w88_probes/oracle_probes.json](scratch/w88_probes/oracle_probes.json):
```
COUNT(*) on FCT_OPS_RISK_DATA = 57; all under method=115.
Σ N_CAPITAL_CHARGE under method=115 = 408,928,819,500
Σ N_RWA_AMT      under method=115 = 5,111,610,243,750
Sample row matches cowork: N_CAPITAL_CHARGE=20,386,244,100; N_RWA_AMT=254,828,051,250.
```

**Current RTIE behavior on this computation:**
- Query: *"What is the operational risk capital charge under Basic Indicator Approach on 2026-03-31?"* ([scratch/w88_probes/c01_bia.json](scratch/w88_probes/c01_bia.json))
- query_type: `DATA_QUERY` ✓
- badge: VERIFIED (false-positive — the SQL executes cleanly, returning `null`)
- schema_searched: `["OFSMDM"]` ← wrong schema
- SQL: `SELECT SUM(N_ANNUAL_GROSS_INCOME * N_ALPHA_PERCENT) AS OPERATIONAL_RISK_CAPITAL_CHARGE FROM ABL_OPS_RISK_DATA WHERE FIC_MIS_DATE = TO_DATE(:mis_date, 'YYYY-MM-DD')`
- Rows: `[[null]]` — the staging table `OFSMDM.ABL_OPS_RISK_DATA` (9 rows; raw load) has no `FIC_MIS_DATE = 2026-03-31`.
- The right answer would be `SELECT SUM(N_CAPITAL_CHARGE) FROM OFSERM.FCT_OPS_RISK_DATA WHERE N_BASEL_METHOD_SKEY=115 AND N_RUN_SKEY=:run AND N_MIS_DATE_SKEY=:date_skey`.

**Categorisation:** WRONG-TABLE + WRONG-FORMULA + WRONG-SCHEMA. The 0.15 × AGI shape is approximately right at the formula level, but it's applied to a staging table that holds raw 3-yr gross-income inputs, not to the fact-row that holds the *computed* per-entity capital charge.

**Coverage gap notes:**
A W88 handler for BIA would need: (a) the canonical fact table = `OFSERM.FCT_OPS_RISK_DATA`, (b) the methodology filter `N_BASEL_METHOD_SKEY=115` (lookup via DIM_BASEL_METHODOLOGY for the SKEY in case it drifts), (c) the result column `N_CAPITAL_CHARGE` (or `N_RWA_AMT` if the user asks for RWA), (d) run-selection default (latest `N_RUN_SKEY` matching the requested MIS date), (e) entity-aggregation default (SUM across N_ENTITY_SKEY unless user names one).

---

### 2. Standardised Approach (TSA / op-risk standardised)

**Canonical fact table candidate(s):**
- Same table as BIA: `OFSERM.FCT_OPS_RISK_DATA`. The schema does NOT split the standardised approach into a separate fact table — methodology is the discriminator.

**Methodology filter:** `N_BASEL_METHOD_SKEY = 77` (`ORSTD`).

**Result columns:**
- TSA formula uses `N_NET_INTEREST_INCOME`/`N_NET_NON_INT_INCOME` per `N_STANDARD_LOB_SKEY` with the β-factor from the table column `N_BETA_FACTOR` (DDL line 1938). Final result still in `N_CAPITAL_CHARGE` / `N_RWA_AMT`.

**Required additional filters:** same as BIA (run, MIS date, entity, standard-LoB).

**Reachability check:** Table exists, but **no rows under method=77 in the current run** ([scratch/w88_probes/oracle_probes.json](scratch/w88_probes/oracle_probes.json) → `ofserm_fct_ops_risk_data_methods` returned only `(115, 57)`). All 57 rows are BIA. So TSA is **table-reachable but currently empty data**.

**Current RTIE behavior:** ([scratch/w88_probes/c02_sa_op.json](scratch/w88_probes/c02_sa_op.json)) Same generic SUM(N_ANNUAL_GROSS_INCOME) on `OFSMDM.ABL_OPS_RISK_DATA`. Returns null. WRONG-TABLE, WRONG-FORMULA.

**Coverage gap notes:** Handler needs the same fact-table mapping as BIA but with method-skey 77. If `N_BETA_FACTOR` is per-LoB, the result is a SUM-by-LoB after applying β × per-LoB AGI, but the OFSERM convention seems to still write the per-LoB result to `N_CAPITAL_CHARGE` on the same fact row, so user-facing query is `SUM(N_CAPITAL_CHARGE) WHERE method=77`. V1 candidate.

---

### 3. Alternative Standardised Approach (ASA)

**Canonical fact table candidate(s):** Same — `OFSERM.FCT_OPS_RISK_DATA`.

**Methodology filter:** `N_BASEL_METHOD_SKEY = 36` (`ORSASA` = "Simplified Alternative Standardised").

**Result columns:** `N_CAPITAL_CHARGE`, `N_RWA_AMT`, `N_LOANS_ADVANCES_AMT` (ASA's signature input — replaces AGI for retail/commercial-banking LoBs in Basel II §652).

**Required additional filters:** same as BIA.

**Reachability check:** Table exists, **no rows under method=36** (same evidence as TSA). Empty data.

**Current RTIE behavior:** ([scratch/w88_probes/c03_asa.json](scratch/w88_probes/c03_asa.json)) `SUM(N_ALPHA_PERCENT * N_ANNUAL_GROSS_INCOME) FROM ABL_OPS_RISK_DATA`. Returns null. WRONG-TABLE, WRONG-FORMULA (ASA uses 0.035 × loans/advances per LoB, not α × AGI).

**Coverage gap notes:** Same shape as TSA. V1 candidate if data is loaded; otherwise handler should honestly decline ("OFSAA run does not currently compute the ASA methodology — no rows in FCT_OPS_RISK_DATA under method=36").

---

### 4. Advanced Measurement Approach (AMA)

**Canonical fact table candidate(s):** Same — `OFSERM.FCT_OPS_RISK_DATA`.

**Methodology filter:** `N_BASEL_METHOD_SKEY = 16` (`ORAMA`).

**Result columns:** `N_CAPITAL_CHARGE`, `N_RWA_AMT`. AMA is model-driven (loss-distribution / scenario), so the input columns are not the standard AGI/loans set — the model output lands directly in `N_CAPITAL_CHARGE`.

**Reachability check:** Table-reachable but no rows under method=16 (same empty-data finding).

**Current RTIE behavior:** ([scratch/w88_probes/c04_ama.json](scratch/w88_probes/c04_ama.json)) `SUM(N_ALPHA_PERCENT) FROM ABL_OPS_RISK_DATA`. Returns null. WRONG-TABLE, WRONG-FORMULA, and α is irrelevant to AMA at all.

**Coverage gap notes:** v1 candidate if a handler is generated; honest-decline candidate if AMA data is never loaded (likely the bank-on-record doesn't use AMA — it ran BIA only).

---

### 5. Credit Risk RWA — Standardised Approach

**Canonical fact table candidate(s):**
- Aggregate total: `OFSERM.FCT_STANDARD_ACCT_HEAD` row where `V_STD_ACCT_HEAD_ID='CAP169'` (Credit RWA). Probed: **658,963,154,349 PKR** is the live total credit RWA in the current run. Note: this CAP-code does NOT split by methodology; it's all credit risk aggregated.
- Per-exposure detail tables: `OFSERM.FCT_NON_SEC_EXPOSURES` and `OFSERM.FCT_SEC_EXPOSURES`. Both referenced extensively by loader functions ([Non_Sec_Risk_Weight_Band_Assignment.sql](db/modules/ABL_CAR_CSTM_V4/functions/Non_Sec_Risk_Weight_Band_Assignment.sql) etc.) but **neither exists in `db/schemas/OFSERM/create_tables.sql`**, and probing the live Oracle returns `DatabaseError` for both. See Section 3 / Section 7.

**Methodology filter:** Per-exposure tables would carry `N_BASEL_METHOD_SKEY`; SA-specific SKEYs in DIM_BASEL_METHODOLOGY include codes like `NSSTDFBA`, `NSSTDLTA`, `NSSTDOP2`, `NSSTDRWAO`, `NSSTDSLM`, `NSCRMSM` (all V_BASEL_RISK_TYPE_ID='CR', V_BASEL_APPROACH_TYPE_ID='NONSTD'). Multiple SKEYs per "Standardised Approach" — this is not a single-skey lookup.

**Result columns:** `N_STD_ACCT_HEAD_AMT` (at CAP169) for the aggregate. No methodology split available at this aggregate level.

**Reachability check:** Aggregate CAP169 = 658,963,154,349 reachable. Per-methodology split via exposure tables: **not reachable** — tables absent from local Oracle. So: aggregate yes, methodology breakdown no.

**Current RTIE behavior:** ([scratch/w88_probes/c05_credit_sa.json](scratch/w88_probes/c05_credit_sa.json)) `SUM(N_LOANS_ADVANCES_AMT) FROM ABL_OPS_RISK_DATA`. WRONG-TABLE (uses op-risk staging for a credit-risk question), WRONG-FORMULA, WRONG-SCHEMA.

**Coverage gap notes:** v1 handler can return total Credit RWA (CAP169) but should honestly decline a methodology-specific question ("methodology breakdown requires FCT_NON_SEC_EXPOSURES / FCT_SEC_EXPOSURES which are not loaded in the local Oracle"). The honest-decline arm is the harder lift here — RTIE today doesn't decline; it fabricates.

---

### 6. Credit Risk RWA — IRB Foundation

**Canonical fact table candidate(s):** `OFSERM.FCT_NON_SEC_EXPOSURES` filtered to F-IRB methodology skeys (NSFIRB=13, NSFIRBLTA=1, NSFIRBCRMCOM=2, NSFIRBMBA=3, NSFIRBSTDM=4, NSFIRBMTM=30, NSFIRBIMM=8, NSFPDEQ=6) — these are observable in [DIM_BASEL_METHODOLOGY dump](scratch/w88_probes/oracle_probes.json), `V_BASEL_APPROACH_TYPE_ID='NONFIRB'`.

**Methodology filter:** Multiple F-IRB skeys (≥ 8 codes mapped to NONFIRB).

**Result columns:** EAD, RWA per exposure — not reachable since the source table isn't here.

**Reachability check:** Same as item 5 — exposure tables don't exist locally.

**Current RTIE behavior:** ([scratch/w88_probes/c06_credit_irb_f.json](scratch/w88_probes/c06_credit_irb_f.json)) Identical fabricated SQL on `ABL_OPS_RISK_DATA`. WRONG-TABLE / WRONG-FORMULA / WRONG-SCHEMA.

**Coverage gap notes:** v2 candidate. v1 handler should honestly decline ("F-IRB credit RWA requires FCT_NON_SEC_EXPOSURES which is not present in the local Oracle catalog").

---

### 7. Credit Risk RWA — IRB Advanced

**Canonical fact table candidate(s):** Same `OFSERM.FCT_NON_SEC_EXPOSURES` filtered to A-IRB skeys (`V_BASEL_APPROACH_TYPE_ID='NONAIRB'`; ≥ 14 codes including NSAIRBLTA=29, NSAIRBSMLTA=5, NSAIRBCRMCOM=23, NSAIMEQ=14, NSAPDEQ=21, etc.).

**Reachability check:** Not reachable (same blocker as items 5/6).

**Current RTIE behavior:** ([scratch/w88_probes/c07_credit_irb_a.json](scratch/w88_probes/c07_credit_irb_a.json)) Same fabrication on `ABL_OPS_RISK_DATA`.

**Coverage gap notes:** v2 candidate; honest-decline shape same as IRB-F.

---

### 8. Market Risk RWA — Standardised

**Canonical fact table candidate(s):**
- Detail: `OFSERM.FCT_MARKET_RISK_EXPOSURES` (exists in DDL, [create_tables.sql:1243](db/schemas/OFSERM/create_tables.sql#L1243)) — contains `N_BASEL_METHOD_SKEY`, `N_RISK_WEIGHT`, `N_SPECIFIC_RISK_CHARGE`, `N_GENERAL_RISK_CHARGE`.
- Summary: `OFSERM.FCT_MARKET_RISK_SUMMARY` (DDL [line 1512](db/schemas/OFSERM/create_tables.sql#L1512)) — has `N_RWA_AMT`, `N_CAPITAL_CHARGE`, `N_BASEL_METHOD_SKEY`.
- Aggregate landing: `FCT_STANDARD_ACCT_HEAD` row at `V_STD_ACCT_HEAD_ID='CAP090'` (Market Risk RWA = 61,175,589,179 in current run).

**Methodology filter:** `N_BASEL_METHOD_SKEY=94` (MRSTD) or one of the sub-variants 132/133/135 (MRSTDSM/MRSTDSCM/MRSTDDP).

**Reachability check:**
- Aggregate CAP090 in `FCT_STANDARD_ACCT_HEAD`: **reachable** (61.18 B PKR).
- `FCT_MARKET_RISK_SUMMARY`: **table exists, 0 rows** ([oracle_probes.json](scratch/w88_probes/oracle_probes.json) `ofserm_fct_market_risk_summary_count`).
- `FCT_MARKET_RISK_EXPOSURES`: not probed but listed in DDL; the empty summary suggests the underlying detail likely is empty/sparse for this run.

**Current RTIE behavior:** ([scratch/w88_probes/c08_market_std.json](scratch/w88_probes/c08_market_std.json)) `SUM(N_LOANS_ADVANCES_AMT) FROM ABL_OPS_RISK_DATA` — WRONG-TABLE / WRONG-FORMULA / WRONG-SCHEMA, plus a different signal: even the *concept* of which fact table to read for "market risk" is not in the catalog the LLM was given (it had only OFSMDM staging).

**Coverage gap notes:** v1 handler can answer aggregate CAP090. Per-methodology breakdown requires `FCT_MARKET_RISK_SUMMARY` to be populated — defer that arm to honest-decline until the OFSAA run starts loading market-risk data.

---

### 9. Market Risk RWA — Internal Models (VaR-based)

**Canonical fact table candidate(s):**
- VaR series detail: `OFSERM.FCT_MR_VAR_DATA` (DDL [line 1893](db/schemas/OFSERM/create_tables.sql#L1893)) — has `N_VAR_VALUE`, `N_SVAR_VALUE`, `N_INCREMENTAL_RISK_CHARGE`, `N_BASEL_VAR_VALUE`.
- Same summary table `FCT_MARKET_RISK_SUMMARY` filtered to `N_BASEL_METHOD_SKEY=106` (MRIM).
- Aggregate also lands in CAP090.

**Reachability check:** `FCT_MR_VAR_DATA` exists, **0 rows**. `FCT_MARKET_RISK_SUMMARY` 0 rows. Aggregate (CAP090) is mixed across methodologies and not separable.

**Current RTIE behavior:** ([scratch/w88_probes/c09_market_im.json](scratch/w88_probes/c09_market_im.json)) Same fabricated SQL on op-risk staging.

**Coverage gap notes:** Honest-decline candidate in v1. The IM-specific data infrastructure exists but isn't populated.

---

### 10. CET1 capital ratio

**Canonical fact table candidate(s):**
- Computed and stored in `FCT_STANDARD_ACCT_HEAD` row where `V_STD_ACCT_HEAD_ID='CAP960'` ("Common Equity Tier 1 Capital ratio"). Reachable in current run: **0.19286686188** (19.29%).
- Loader: [CS_Common_Equity_Tier_1_Capital_ratio.sql](db/modules/ABL_CAR_CSTM_V4/functions/CS_Common_Equity_Tier_1_Capital_ratio.sql). Formula = CAP841 / CAP838 (Net CET1 Capital / Total RWA), verified by grepping the function body.

**Methodology filter:** None — CAP-code is the discriminator; ratios are run-scoped, not methodology-scoped.

**Result columns:** `N_STD_ACCT_HEAD_AMT` (the ratio).

**Required additional filters:** `N_RUN_SKEY`, `N_MIS_DATE_SKEY`, `N_ENTITY`, `N_GAAP_SKEY`. CCY irrelevant (ratio is unitless).

**Reachability check:** Fully reachable. ([scratch/w88_probes/oracle_supp.json](scratch/w88_probes/oracle_supp.json), `all_caps_in_fact` → CAP960).

**Current RTIE behavior:** ([scratch/w88_probes/c10_cet1.json](scratch/w88_probes/c10_cet1.json)) `SUM(N_LOANS_ADVANCES_AMT) / NULLIF(SUM(N_ANNUAL_GROSS_INCOME), 0) FROM ABL_OPS_RISK_DATA` — meaningless ratio of credit loans to ops-risk gross income. WRONG-TABLE / WRONG-FORMULA / WRONG-SCHEMA.

**Coverage gap notes:** Cleanest v1 handler shape. Handler needs only: table=FCT_STANDARD_ACCT_HEAD, CAP=CAP960, result-column=N_STD_ACCT_HEAD_AMT. Run defaults can come from `FSI_CAP_RUN_EXE_PARAMETERS`. Identical shape for items 11/12.

---

### 11. Tier 1 capital ratio

**Canonical fact table candidate(s):** `FCT_STANDARD_ACCT_HEAD` at `V_STD_ACCT_HEAD_ID='CAP214'` ("Tier 1 Capital Ratio"). Current run: **0.19286686188** (identical to CET1 because there's no Additional Tier 1 capital in this run's data — CAP908 Net AT1 = 0).

**Methodology filter:** None.

**Reachability check:** Reachable.

**Current RTIE behavior:** ([scratch/w88_probes/c11_tier1.json](scratch/w88_probes/c11_tier1.json)) `SUM(N_ANNUAL_GROSS_INCOME) / SUM(N_LOANS_ADVANCES_AMT)` on `ABL_OPS_RISK_DATA`. WRONG-EVERYTHING.

**Coverage gap notes:** Same handler shape as CET1, CAP=CAP214.

---

### 12. Total Capital ratio (Capital Adequacy Ratio, CAR)

**Canonical fact table candidate(s):** `FCT_STANDARD_ACCT_HEAD` at `V_STD_ACCT_HEAD_ID='CAP192'` ("Capital Ratio"). Current run: **0.26289311967** (26.29%). Formula CAP210 / CAP838 (Total Eligible Capital / Total RWA) confirmed in [CS_Capital_Ratio.sql](db/modules/ABL_CAR_CSTM_V4/functions/CS_Capital_Ratio.sql).

**Reachability check:** Reachable.

**Current RTIE behavior:** ([scratch/w88_probes/c12_total_cap.json](scratch/w88_probes/c12_total_cap.json)) `SUM(N_LOANS_ADVANCES_AMT) / SUM(N_ANNUAL_GROSS_INCOME) FROM ABL_OPS_RISK_DATA`. WRONG-EVERYTHING.

**Coverage gap notes:** Same handler shape, CAP=CAP192.

---

### 13. Leverage Ratio

**Canonical fact table candidate(s):**
- `FCT_STANDARD_ACCT_HEAD` at `V_STD_ACCT_HEAD_ID='CAP843'` ("Leverage Ratio"). **Value in current run: 0.0** (placeholder, not computed).
- Leverage Exposure components are CAP1807 / CAP1809-1816 (all 0 in current run).
- The minimum-leverage-ratio threshold lives at CAP1817 = 0.03 (seed, not a computed ratio).
- DIM-level: `N_LEVERAGE_EXPOSURE_AMOUNT` appears as a column on `FSI_CAP_BANKING_EXPOSURES` and other tables (DDL [line 2418, 2479, 2777](db/schemas/OFSERM/create_tables.sql)), but no aggregated `FCT_LEVERAGE_RATIO_SUMMARY` table exists.

**Methodology filter:** None.

**Reachability check:** The fact row at CAP843 exists but is **populated with 0.0** — the computation isn't being run in the current OFSAA execution. No loader function for CAP843 was found in `db/modules/ABL_CAR_CSTM_V4/functions/`. So: structurally reachable, but value is meaningless (not computed).

**Current RTIE behavior:** ([scratch/w88_probes/c13_leverage.json](scratch/w88_probes/c13_leverage.json)) Fabricated SQL with a join to `STG_GL_DATA`. WRONG-EVERYTHING.

**Coverage gap notes:** Honest-decline v1 candidate — telling the user "the Leverage Ratio is not computed in this OFSAA run; the placeholder slot at CAP843 reads 0.0" is more accurate than reporting 0.0 as if it were a real number. Handler shape: same as CET1/T1/CAR but with a freshness check ("if value==0 AND no loader writes this CAP code, surface as not-computed").

---

### 14. Liquidity Coverage Ratio (LCR)

**Canonical fact table candidate(s):** **None located in the local indexed corpus.**
- No `FCT_LCR_*` / `STG_LCR_*` / table containing "LCR" in its name in either OFSERM or OFSMDM. Probe: `SELECT OWNER, TABLE_NAME FROM ALL_TABLES WHERE TABLE_NAME LIKE '%LCR%'` returned 0 rows ([oracle_probes.json](scratch/w88_probes/oracle_probes.json), `user_tables_lcr_nsfr_leverage`).
- No CAP-code for LCR in `DIM_STANDARD_ACCT_HEAD`.
- The OFSAA module loaded here (ABL_CAR_CSTM_V4) is the Capital-Adequacy-Ratio module, not the Liquidity-Risk-Management module — LCR likely lives in a separate OFSLRM batch that isn't in this checkout.

**Reachability check:** **Not reachable.** No table candidate exists.

**Current RTIE behavior:** ([scratch/w88_probes/c14_lcr.json](scratch/w88_probes/c14_lcr.json)) `SUM(N_LOANS_ADVANCES_AMT) / NULLIF(SUM(N_LOANS_ADVANCES_AMT + N_DISPOSAL_PROP_INCOME_AMT + N_FEE_INCOME + N_EXTRAORDINARY_INCOME), 0)` — entirely fabricated ratio. WRONG-EVERYTHING.

**Coverage gap notes:** Honest-decline-only candidate. Handler should produce: "LCR is not part of the OFSAA Capital Adequacy module loaded in this RTIE deployment; no fact table containing LCR exists in OFSERM or OFSMDM."

---

### 15. Net Stable Funding Ratio (NSFR)

**Canonical fact table candidate(s):** **None located.** Same finding as LCR. No `*NSFR*` tables in either schema. The only NSFR-related artefact is column `N_NSFR_RESIDUAL_MAT_BAND_SKEY` on `FCT_MITIGANTS` ([create_tables.sql:1821](db/schemas/OFSERM/create_tables.sql#L1821)) — a per-mitigant residual-maturity band, not a fact-row representing the NSFR result.

**Reachability check:** Not reachable.

**Current RTIE behavior:** ([scratch/w88_probes/c15_nsfr.json](scratch/w88_probes/c15_nsfr.json)) `SELECT FIC_MIS_DATE, N_LOANS_ADVANCES_AMT, N_NET_INTEREST_INCOME FROM ABL_OPS_RISK_DATA` — at least query_kind degraded to ROW_LIST rather than AGGREGATE, but returns no rows. WRONG-EVERYTHING.

**Coverage gap notes:** Same as LCR — honest-decline-only.

---

## 3. Cross-cutting findings

### Fact-table coverage matrix

|  | Count |
|---|---|
| Items with a clearly identified canonical fact table in OFSERM | **12 of 15** (all except LCR, NSFR; Leverage Ratio has a placeholder CAP code but no loader) |
| Items reachable in the local Oracle today (table + data) | **6 of 15** — BIA (1), Credit-RWA aggregate (5; CAP169 total only), Market-RWA aggregate (8; CAP090 total only), CET1 (10), T1 (11), CAR (12) |
| Items blocked on data-load (table exists, no rows in this run) | **4 of 15** — TSA (2), ASA (3), AMA (4), MR-IM (9, requires FCT_MR_VAR_DATA which is empty); arguably Tier 2 ratio too (CAP935 in DIM but no fact row this run, though that's a sub-question of item 12) |
| Items blocked on missing dependency (referenced table absent from local Oracle) | **2 of 15** — CR-IRB-F (6), CR-IRB-A (7) — both need FCT_NON_SEC_EXPOSURES which doesn't exist locally; CR-SA (5) methodology breakdown also blocked but aggregate-CAP169 still works |
| Items with no canonical fact table at all | **3 of 15** — Leverage Ratio (effectively; loader missing), LCR (14), NSFR (15) |

### Methodology-filter pattern

`N_BASEL_METHOD_SKEY` is the universal discriminator for items 1-9 (op risk + market risk + credit risk per-exposure). For items 10-12 (capital ratios) and the aggregate RWA totals at CAP090/CAP169/CAP170, the discriminator is `V_STD_ACCT_HEAD_ID` via `DIM_STANDARD_ACCT_HEAD`.

The SKEY → method-name mapping is stable enough for a static registry — `DIM_BASEL_METHODOLOGY` lookups by `N_BASEL_METHOD_SKEY` returned 143 latest-record entries with consistent codes (`ORBIA`, `ORSTD`, `MRSTD`, etc.). However, the SKEYs themselves can theoretically drift across OFSAA upgrades because they're surrogate keys. **A handler that hardcodes `115` for BIA is fragile across schema migrations**; safer to hardcode the *code* `'ORBIA'` and resolve to SKEY at handler-init via `DIM_BASEL_METHODOLOGY` lookup.

CAP-codes (CAP170, CAP192, CAP838, etc.) are seeded constants in OFSAA's domain and considered stable across upgrades.

### Classifier recognition signal

Of the 15 plausible NL queries:
- **15 of 15 classified as DATA_QUERY** ✓ — the orchestrator's classifier handles named-computation phrasing correctly.
- **0 of 15 routed to OFSERM** ✗ — every one defaulted to `schema_searched: ["OFSMDM"]`.
- **15 of 15 returned a VERIFIED badge** despite producing wholly wrong / null results — the validation gate sees "the SQL executed without throwing" and badges accordingly. (Pre-existing observation; this is a W57-class trust gap, surfaced by but not in-scope for W88.)

Phrasings tested were full-name + acronym in parentheses ("Basic Indicator Approach (BIA)", "Liquidity Coverage Ratio (LCR)"). I did not test acronym-only or plain-English forms ("op risk capital under BIA", "what's the BIA charge") — those are likely to either degrade (acronym dropped by the classifier's normalisation) or improve (if the classifier matches longer keyword strings). That's a separate inventory to run if the handler-registry uses keyword-match rather than LLM-driven extraction.

### Filter-defaulting strategy

When the user doesn't specify run / entity / currency:
- **Run:** `FSI_CAP_RUN_EXE_PARAMETERS` exists in OFSERM (DDL [line 3008](db/schemas/OFSERM/create_tables.sql#L3008)) and would be the canonical place to look up "latest production run for MIS date X." Probing whether it's populated and queryable was not part of this diagnostic scope; flag for the fix PR.
- **Entity / GAAP / forecast-date:** the BIA fact data sums across 57 rows / multiple entities cleanly with `SUM`; defaulting to aggregate-across-entity is the right user-facing answer. If the user names an entity, filter; otherwise SUM.
- **Currency:** values are stored in `V_CCY_CODE` (PKR in current run); a handler that displays the amount should display the CCY suffix from `V_CCY_CODE`.
- **MIS date:** the user almost always names a date; absent that, default to MAX(N_MIS_DATE_SKEY) for the resolved run.

---

## 4. Architectural shape candidates

(Per the prompt: not a design proposal. Two-or-three-sentence summaries with diagnostic-supported pros/cons. Toheed decides.)

### A. Static registry of handlers (Python data structure)

Each named computation is a Python dict: `{table, method_skey or cap_code, result_column, default_filters, decline_reason_template}`. Loader code registers handlers at import time.

- **Supports it:** Inventory is small (~15 items) and stable; handler shape is uniform (table + filter + result-column). No need for OFSAA-side configuration. Diagnostic-confirmed: 12/15 follow the exact same shape with CAP-code or method-SKEY as the only varying field.
- **Argues against:** Adding a new computation (e.g., G-SIB surcharge if a future OFSAA module loads it) requires a code change. SKEY values are theoretically fragile across OFSAA upgrades unless handlers resolve by code-string rather than SKEY literal.

### B. Manifest-driven registry (YAML/JSON next to existing module manifests)

Handler definitions live in `db/modules/ABL_CAR_CSTM_V4/named_computations.yaml` (or similar); loader reads at startup like the existing per-module manifest.

- **Supports it:** Mirrors the existing `manifest.yaml` pattern that drives `src/parsing/loader.py`. Adding a computation is a YAML edit, not a code change. Different bank deployments (different OFSAA configs) can ship different manifests without touching `src/`.
- **Argues against:** Diagnostic shows the 15 items are tightly coupled to OFSAA seed CAP-codes and Basel-seeded method-codes — the registry isn't really "per-module"; it's a domain dictionary that applies to any OFSAA Capital-Adequacy deployment. So the per-module structure may be misleading. Pure schema-aware loading is over-engineered for content that's near-static.

### C. Hybrid — registry for the closed-set common cases + LLM fallback

Same as A or B, but if the classifier sees a named-computation phrasing that doesn't match any registry entry, fall through to today's LLM-driven SQL generator with a hint ("you're being asked about a Basel-named computation that isn't in the dictionary; explicitly check `DIM_BASEL_METHODOLOGY` for the method code, and prefer OFSERM.FCT_*").

- **Supports it:** Diagnostic shows the long-tail risk is real — 143 method-SKEYs in DIM_BASEL_METHODOLOGY, only ~12 of which were in the 15-item scope. A user asking about, say, "Equity Exposures - IRB PD/LGD Approach" (SKEY 11) would not hit the registry but would still have a real fact-table answer.
- **Argues against:** The LLM fallback is *exactly* what today's failure mode is — fabrication on wrong tables. A "registry + fallback" without strengthening the fallback's catalog-construction isn't materially better than registry-alone for unknown queries; it just doesn't make registry-known queries any worse. Worth considering only if the fallback is independently improved (e.g., by W35 catalog enrichment that biases the SQL generator toward OFSERM.FCT_*).

---

## 5. Coverage decisions Toheed needs to make

- **Which subset of the 15 ships in W88 v1?** Recommend a v1 subset of the 6 fully-reachable items: **BIA (1), Credit-RWA aggregate (5; CAP169 total only), Market-RWA aggregate (8; CAP090 total only), CET1 (10), T1 (11), CAR (12)**. These 6 all share the same "fact-table + scalar-result" handler shape and all have live data in the current run. Items 2/3/4 (TSA/ASA/AMA), 6/7 (IRB), 9 (MR-IM) are also v1-able as honest-decline handlers (the table exists or methodology exists, the data doesn't). Items 13 (Leverage), 14 (LCR), 15 (NSFR) are v1-able as honest-decline-only ("not part of this OFSAA deployment").

- **Where in the orchestrator does the pre-router fire?** The prompt's framing is "between stage 2 (pre-checks) and stage 3 (SQL Generator)." Concretely that corresponds to a hook in [src/agents/data_query.py](src/agents/data_query.py#L290) between `_resolve_target_schema()` (line 290) and `_build_schema_catalog()` (line 319). If the pre-router matches, it short-circuits with a handler-built SQL+result and the SQL Generator never sees the query. If no match, fall through to today's pipeline. (Alternative: fire inside the classifier prompt, asking it to emit a `named_computation: "BIA"` field — pushes the matching into the LLM. Less crisp.)

- **W88 vs W87 interaction (entity-extraction fallback).** Diagnostic doesn't directly speak to W87 since the test queries here didn't name entities. But: in queries that mention BOTH a named computation AND a named entity ("CET1 ratio for ABL on 2026-03-31"), W88 should take precedence (it knows the canonical computation), then W87 should fire only to scope the WHERE clause (`AND DIM_ORG_STRUCTURE.V_ENTITY_CODE = 'ABL'`). The dependency is one-way: W88 produces the table+result-column; W87 layers in the entity filter.

- **Honest-decline shape.** Existing W45 / W49 patterns are for FUNCTION_LOGIC declines ("function not in graph"). W88 declines are DATA_QUERY semantics ("named computation not loaded in this Oracle instance"). The shape is different enough — user-facing message references regulatory concepts, not function names — that I'd argue for a new template. The fix-PR can crib the *framing* (badge, decline reason, suggested-alternative line) from W45 but the *content* needs to be data-domain-specific.

---

## 6. Effort estimate

Order-of-magnitude, post-diagnostic:

- **Lines of code:** 200–400 lines for the registry data (15 entries × ~10 fields), 200–300 lines for the pre-router logic (NL-phrase matching, handler dispatch, SQL stitching), 150–200 lines for the honest-decline message templates. Total ≈ 600–900 new lines.
- **New / modified files:**
  - New: `src/agents/named_computation_router.py` (or similar) for the registry + dispatcher.
  - Modified: `src/agents/data_query.py` (one hook between `_resolve_target_schema` and `_build_schema_catalog`).
  - Possibly new: `db/registries/named_computations.yaml` (if shape B is chosen).
  - No changes to `src/agents/orchestrator.py` if the pre-router fires inside data_query (recommended).
- **Test surface area:** unit tests on the registry-matcher (NL phrasing → handler), unit tests on each handler's SQL synthesis, integration tests on /v1/stream for at least the 6 v1-reachable items, plus 3 honest-decline cases (LCR, NSFR, Leverage). Estimate 20–30 new tests.
- **Manual canary surface area:** add a W88-canary tier to `tests/canary/canaries.yaml` with the 15 NL queries from this diagnostic ([scratch/w88_probes/](scratch/w88_probes/) — the prompts and expected-table mappings are already captured).

---

## 7. Anything unexpected

1. **MIS-date mismatch — current Oracle data is at 2025-12-31, not 2026-03-31.** The benchmark docs and the prompt both reference `D_CALENDAR_DATE = TO_DATE('20260331','YYYYMMDD')` and `n_run_skey='870'`. But the BIA fact rows in the live Oracle have `N_MIS_DATE_SKEY=20251231` and `N_RUN_SKEY IN (812, 818, 840)`. So either (a) the run-870 / 2026-03-31 snapshot was a planning artefact in the benchmark docs that was never loaded, or (b) the local Oracle was repopulated from an earlier MIS date after the benchmarks were written. The cowork BIA capital-charge value (20,386,244,100) is in our table at `N_RUN_SKEY=812, N_MIS_DATE_SKEY=20251231` — so the value matches even though the date doesn't. **Implication for W88:** any user query naming MIS date 2026-03-31 will return null even from the correct table; the handler needs a "no data for the requested date — latest available is 2025-12-31" path. **Implication for W35:** this is the live data-load state of the local Oracle as of 2026-05-12, not the OFSAA-batch state the benchmarks assume; W35 / data-load work should be aware. Reachability classifications in this diagnostic are based on what's *physically present*, not what the benchmark expects.

2. **OFSERM DDL extract is incomplete for credit-risk per-exposure tables.** `FCT_NON_SEC_EXPOSURES` and `FCT_SEC_EXPOSURES` are referenced by 5+ loader functions (`Non_Sec_*_Band_Assignment.sql`, `RESECURITIZED_DEDUCTIONS_*.sql`, `Country_wise_Total_EAD_*.sql`) but absent from `db/schemas/OFSERM/create_tables.sql`. Live Oracle probe via SqlGuardian → SchemaTools returns `DatabaseError` for both. This affects W35 (schema-aware catalog should know these tables) and the W88 handler set (CR-IRB-F / CR-IRB-A blocked).

3. **Multiple methodology sub-variants per Basel "Standardised Approach" label.** DIM_BASEL_METHODOLOGY has 5 codes mapping to V_BASEL_APPROACH_TYPE_ID='NONSTD' (NSSTDFBA, NSSTDLTA, NSSTDOP2, NSSTDRWAO, NSSTDSLM, NSCRMSM) — these are all "standardised approach for credit risk" but with different option-2/look-through/slotting/etc. semantics. A handler that picks a single SKEY for "credit risk SA" will under-count. The right v1 answer for "credit RWA under SA" is the aggregate at CAP169 (which already sums across all CR methodologies), not a per-SKEY filter on the (absent) exposure tables.

4. **CET1 ratio == Tier 1 ratio in current data (both 0.19286686188).** This is mathematically correct — when Net Additional Tier 1 Capital is 0 (CAP908 = 0 in current run), CET1 and T1 collapse to the same ratio. Worth surfacing in the handler's response context ("Tier 1 ratio = CET1 ratio because the bank holds no Additional Tier 1 instruments in this run") rather than just returning the same number twice.

5. **CAP838 "Total RWA" is not the loaded sum of CAP090+CAP169+CAP170+CAP959.** Arithmetic check: 61.18 B (CAP090) + 658.96 B (CAP169) + 254.83 B (CAP170) + 0.42 B (CAP959) = 975.39 B, vs CAP838 = 974.97 B. Off by ~420 M (~0.04%). Not a W88 concern per se, but means a v1 handler that fabricates CAP838 from its components would diverge from the OFSAA-loaded number. Always read CAP838 directly.

6. **`FCT_MARKET_RISK_SUMMARY` is empty but `CAP090` (Market Risk RWA) is populated at 61.18 B.** The CAP090 row in `FCT_STANDARD_ACCT_HEAD` is populated by [MKT_RISK_RWA_STD_ACCT_HEAD_DATA_POP.sql](db/modules/ABL_CAR_CSTM_V4/functions/MKT_RISK_RWA_STD_ACCT_HEAD_DATA_POP.sql), which the manifest shows reads from `FSI_GL_DATA` (general-ledger source) — bypassing the FCT_MARKET_RISK_SUMMARY aggregation step entirely. In the current run, market-risk is being booked through a GL-aggregate path, not a per-exposure compute path. This explains why aggregate CAP090 is reachable while per-methodology MR-STD vs MR-IM breakdowns are not.

7. **Leverage Ratio at CAP843 = 0.0 with no loader function in `functions/`.** Every other CAP-code with a populated value has a corresponding `CS_*` / `*RWA*` loader in the functions folder. CAP843 is the only "headline" ratio with a placeholder fact row and no loader code — strongly suggests OFSAA's leverage-ratio computation is either disabled in this run config or implemented entirely as OFSAA metadata (which RTIE's manifest doesn't capture). Honest-decline arm should explicitly flag "0.0 is a placeholder, not a computed result."

8. **BIA totals differ across runs by entity-by-entity scope.** Σ N_CAPITAL_CHARGE across all 57 BIA rows = 408.93 B; but the single (run=812, entity=ABL-ish) row = 20.39 B. The 57 rows are spread across 3 runs (812, 818, 840) and multiple entities (`N_ENTITY_SKEY` varying) — so any handler that returns "the BIA charge" without scoping run+entity will return a sum that's not the stakeholder-meaningful number. The cowork answer (20,386,244,100) was correctly scoped to a single (entity, run). Default-scoping rules need to be explicit in the handler.

---

## Appendix: probe artefacts

All probe outputs preserved under [scratch/w88_probes/](scratch/w88_probes/):

- `oracle_probes.json` — 16 direct Oracle probes (table existence, row counts, BIA sample, CAP-code map, all-tables LCR/NSFR search).
- `oracle_supp.json` — 6 supplementary probes (method-SKEY 115 lookup, op-risk / market-risk method dump, full CAP-code dump from FCT_STANDARD_ACCT_HEAD).
- `c01_bia.json` through `c15_nsfr.json` — full /v1/stream `done` payloads for the 15 NL probes.

Driver script: [scratch/w88_probe_driver.py](scratch/w88_probe_driver.py).
