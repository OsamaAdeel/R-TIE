# Run 9 — Post-W84 Scored Measurement

**Date:** 2026-05-12
**Backend SHA:** `8603d9c` (post-W84 merge; verified via `git rev-parse HEAD`)
**Battery:** 16 questions (A1–D4), identical to Run 8 (`scratch/run8/run_battery.py`)
**Runner:** `scratch/run9/run_battery.py`
**Raw captures:** `scratch/run9/q*.json`
**Scoring data:** `scratch/run9/_scoring_data.json`

---

## 1. Preconditions

- **Backend SHA:** `8603d9cec37e4ee019696c41ac84b789437f9b02` ✓
- **/health:** 200
- **Benchmark file unchanged** since Run 8 (same 16 question text strings; verified by re-using Run 8's `QUESTIONS` list verbatim in `run_battery.py`).
- **No FLUSHDB** between Run 8 and Run 9. Redis state is whatever the running backend had loaded.
- **W84 diagnostic block sanity check:** Pre-battery probe on `How does FN_LOAD_OPS_RISK_DATA work?` returned `done.diagnostic = {w81_suppressed: true, w70_anchor: "FN_LOAD_OPS_RISK_DATA", w76_anchor: null}`. ✓

All preconditions met. Battery executed without TIMEOUT or 500. All 16 questions returned a `done` event.

---

## 2. Per-question result table

Trust column legend:
- **AP** = ALIGNED-POSITIVE (VERIFIED, content clean)
- **AN** = ALIGNED-NEGATIVE (UNVERIFIED with GROUNDING-HIGH warning that fits the failure mode)
- **ESC** = ESCAPE (VERIFIED but body contains a fabricated December/pass-through claim not supported by source)
- **OC** = OVER-CAUTIOUS (UNVERIFIED with no GROUNDING-HIGH catch — flagged on a softer signal)

ESC is hand-classified from body content + ground-truth grep of the source file. The automated rubric used by `score_extract.py` cannot tell ESC from AP without reading the body.

| ID | badge | trust | w70_anchor | w81_supp | w76_anchor | delta vs Run 8 |
|----|-------|-------|-----------|---------|-----------|-----------------|
| A1 | UNVERIFIED | AN | ABL_NON_SEC_RISK_WEIGHT_MAP_POP_CSTM | True | null | SAME |
| A2 | VERIFIED   | **ESC** (Dec softener) | CS_GOODWILL_CALCULATION | True | null | SAME (still ESCAPE) |
| A3 | UNVERIFIED | AN | CAP_CONSL_EFFECTIVE_SHAREHOLDING_PERCENT_… | True | null | SAME |
| A4 | UNVERIFIED | AN (pass-through caught) | FN_LOAD_OPS_RISK_DATA | True | null | **RECOVERED** (Run 8: VERIFIED+pass-through ESC) |
| B1 | UNVERIFIED | AN | CS_Deferred_Tax_Asset_Net_of_DTL_Calculation | True | null | SAME |
| B2 | UNVERIFIED | AN | CS_OTHER_INTANGIBLE_ASSETS_NET_OF_DTL_CALCULATION | True | null | SAME |
| B3 | VERIFIED   | **ESC** (whole-fn Dec overgeneralization) | FN_LOAD_OPS_RISK_DATA | True | null | SAME (Run 8 was also ESC, missed in Run 8 scoring) |
| B4 | UNVERIFIED | AN | ALLOCATION_RANK_ASSIGNMENT | True | null | SAME |
| C1 | UNVERIFIED | AN | OPS_RISK_DATA_POPULATION_CSTM | True | null | SAME |
| C2 | UNVERIFIED | AN (sequential-step catches fired) | REP_BANK_PARTY_SHARE_HOLD_PERCENT_CREATION | True | null | **RECOVERED** (Run 8: VERIFIED+month-substitution ESC) |
| C3 | UNVERIFIED | AN | (null — explainer didn't run) | False | null | SAME |
| C4 | UNVERIFIED | AN | CAP_CONSL_EFFECTIVE_SHAREHOLDING_PERCENT_… | True | null | SAME |
| D1 | VERIFIED   | **ESC** (Dec + fabricated pass-through SQL) | INSIGNFCNT_INVST_DED_STD_ACCT_HEAD_DATA_POP | True | null | **NEW-ESCAPE** (Run 8: UNVERIFIED) |
| D2 | UNVERIFIED | AN | OPS_RISK_DATA_POPULATION_CSTM | True | null | SAME |
| D3 | UNVERIFIED | AN | REP_BANK_PARTY_SHARE_HOLD_PERCENT_CREATION | True | null | SAME |
| D4 | UNVERIFIED | AN | CAPITAL_STD_ACCT_HEAD_POP | True | null | SAME |

### w76_anchor consistently null

Every Run 9 question returned `w76_anchor = null` — none of the 16 prompts used the `In <FunctionName>, …` syntax that triggers W76's M1 prefix rule. The benchmark uses `How does X work?`, `Trace …`, `What value does X assign …` patterns. **w76_anchor exposed exactly zero new signal this run.** That's expected per W84's known semantics narrowing (documented at merge time).

### w81_suppressed fires near-universally

15/16 queries hit W81 cross-process suppression. Only C3 didn't — the ABL_CAP_MITIGANT_N_GROUP_ASSET_SIZE retrieval was tight enough to stay within one process. The W81 firing rate is now measurable but **the binary signal alone is not actionable** for W81 Option B prioritization — every multi-function answer suppresses the header. The Option B question ("should we render a multi-process listing header instead of nothing") needs a *per-response severity* signal, not a binary suppression flag.

---

## 3. Aggregate counts

### Category 1 — Trust contract

| | Run 9 | Run 8 |
|---|---|---|
| ALIGNED-POSITIVE | 0 (note: 3 VERIFIED responses all classify as ESCAPE on body inspection) | 4 |
| ALIGNED-NEGATIVE | 13 | 11 |
| ESCAPE (hand-classified) | **3** (A2, B3, D1) | 4 by hand (A2, A4, B3, C2 — B3 unflagged in Run 8 scoring) |
| OVER-CAUTIOUS | 0 | 1 |

**Run 8 hidden ESCAPE retroactively classified:** Reviewing Run 8 captures for the report, B3 had identical fabricated content to Run 9 ("This entire function ONLY runs when the reporting month is December" + CBA branch). Run 8's scoring missed it. The "true" Run 8 escape count is 4, not 3.

### Category 2 — Anchor signal (w70_anchor)

The Run 8 scoring conflated `meta.object_name` (the enriched classifier blob) with the resolved anchor. Now that W84 surfaces `w70_anchor` directly, classification is cleaner. Compared against the asked-about function from each query:

| | Run 9 |
|---|---|
| ANCHOR-CORRECT (matches asked function) | 12/16 |
| ANCHOR-DRIFTED (lands on a sibling in retrieval) | 3/16 (C2, D1, D3 — all VARIABLE_TRACE / pipeline questions) |
| N/A (explainer didn't run) | 1/16 (C3) |

**Anchor drift is concentrated in trace / pipeline questions** (C2, D1, D3, partially D4). The semantic top-1 cascade isn't the right signal for "trace X across multiple functions" queries.

### Category 3 — Retrieval signal

| | Run 9 |
|---|---|
| RETRIEVAL-GOOD (asked fn in functions_analyzed) | 10/16 |
| RETRIEVAL-PARTIAL (warning mentions "not in retrieved sources") | 6/16 (C1, C2, C3, D2, D3, D4) |

All RETRIEVAL-PARTIAL cases are CAP-code / pipeline questions — the same class W82 was scoped against.

### Category 4 — Pattern fires (Run 9 vs Run 8)

| Pattern | Run 9 fires | Run 8 fires |
|---|---|---|
| W78a heading-citation catch | 0/16 | 0/16 |
| W81 cross-process suppression | **15/16** | 0/16 (W84 made it visible — Run 8 couldn't see it) |
| W83a December paraphrase catch (warning fired) | 8/16 | 8/16 |
| W82-class sequential-step fabrication caught | 2/16 | 1/16 |
| W70b narrative drift catch | 1/16 | 1/16 |

**The W83a catch count is identical (8/16) but the SET differs:** Run 8 caught A1, A3, B1, B2, B4, C1, C4, D4 (8). Run 9 caught the same eight. **Run 8 missed A2/A4/B3/D1; Run 9 misses A2/B3/D1 and adds A4 (different paraphrase form).** The eight cases W83a catches reliably are "strict-form" matches (`only runs when the reporting month is december` — exact phrase). The cases it misses are paraphrases / softeners / overgeneralizations.

---

## 4. Delta vs Run 8

Drift summary (16 questions):

| Direction | Count | Cases |
|---|---|---|
| SAME-RESULT (badge + trust unchanged) | 13 | A1, A2, A3, B1, B2, B3, B4, C1, C3, C4, D2, D3, D4 |
| RECOVERED (Run 8 ESC → Run 9 AN) | 2 | A4, C2 |
| NEW-ESCAPE (Run 8 not-ESC → Run 9 ESC) | 1 | D1 (Run 8 was UNVERIFIED; Run 9 went VERIFIED with same-class fabrications) |
| DRIFTED other | 0 | — |

**Drift floor (LLM non-determinism on identical code):** 3/16 questions flipped between runs (A4, C2, D1). That's a ~19% drift rate. For future runs, treat any single-question delta as within noise unless it persists across two runs.

**Direction of drift:**
- A4 and C2 recovered toward better grounding. Plausibly the model produced slightly different prose that triggered different W57 checks.
- D1 drifted toward worse grounding. The Run 8 capture had `OPS_RISK_DATA_POPULATION_CSTM not in retrieved sources` warning (caught it as UNVERIFIED). Run 9 produced different fabricated narration (fabricated SQL with `INTO some_variable`) that no detector caught.

The drift is not symmetric across runs — the *content* of the model output varies, and the *detectors fire only on specific surface patterns*. A different paraphrase escapes / different detectors catch.

---

## 5. W83B diagnostic analysis (the new section)

### ESCAPE: A2 — `CS_Goodwill_Calculation` "particularly when … December" softener

**Diagnostic block**
```json
{ "w70_anchor": "CS_GOODWILL_CALCULATION",
  "w76_anchor": null,
  "w81_suppressed": true,
  "hierarchy_header_present": false }
```

**Failure surface** (verbatim from body):
> "The CS_Goodwill_Calculation function is designed to compute and merge goodwill-related capital adjustments … This function is executed under specific conditions, **particularly when the reporting month is December**, which is crucial for year-end financial reporting and regulatory compliance."

And the closing line:
> "This function is executed specifically in December, aligning with year-end reporting requirements …"

**Ground truth:** `grep -c -iE "(EXTRACT\(MONTH|TO_CHAR.*'MM'|MONTH *= *12|december)" CS_Goodwill_Calculation.sql` → **0**. The source has no month gate, no December reference, nothing date-conditional. Pure fabrication.

**W83B detector implication.** The detector has everything it needs from W84:
1. `w70_anchor.function = "CS_GOODWILL_CALCULATION"` — tells the detector exactly which source to fetch.
2. Phase 1 source loader can fetch that function's Redis key (`graph:source:OFSERM:CS_GOODWILL_CALCULATION`) or fall back to `db/modules/.../CS_Goodwill_Calculation.sql`.
3. Apply a *source-side* December predicate: does the source body contain any of `EXTRACT(MONTH …) = 12`, `TO_CHAR(…, 'MM') = '12'`, `MONTH(…) = 12`, or a literal `'12'` immediate near a date function?
4. If source-side predicate is **false** AND body contains any December/month-12/year-end phrase → **fire**.

The signal differentiating legitimate from fabricated isn't the body text — it's the **mismatch between body claim and source presence**. This is the same shape as the existing W57 pass-through guard (Check 3 in `_w57_check_pass_through_phrase`), just applied to the December predicate.

### ESCAPE: B3 — `FN_LOAD_OPS_RISK_DATA` whole-function December overgeneralization

**Diagnostic block**
```json
{ "w70_anchor": "FN_LOAD_OPS_RISK_DATA",
  "w76_anchor": null,
  "w81_suppressed": true,
  "hierarchy_header_present": false }
```

**Failure surface:**
> "**This entire function ONLY runs when the reporting month is December.** The adjustment for N_ANNUAL_GROSS_INCOME for V_LOB_CODE='CBA' on a non-ABLIBG entity occurs under specific conditions outlined in the function."

**Ground truth:** Source has `IF TO_NUMBER (EXTRACT (MONTH FROM TO_DATE (CQD, 'DD-MON-RR'))) = 12` — but it gates ONE conditional block, not the entire function. The function has many other branches outside the IF that run regardless of month. So:

- "function has December gating somewhere" → TRUE
- "entire function ONLY runs in December" → FALSE

This is harder for W83B than A2. A simple source-side existence check would accept the December claim and miss the overgeneralization.

**W83B detector implication.** Two-stage:
1. **Stage 1 (cheap):** if `grep -c "month.*= *12"` in source is 0 → fire on any December body claim. Catches A2-style escapes cleanly.
2. **Stage 2 (harder):** if source has December gating but only in localized branches, body claims of the form `"entire function ONLY runs … December"` are overgeneralizations. Detect by combining body regex (`"entire function only runs"`, `"the function … only runs"`) with structural source analysis (does the December conditional wrap the whole function body, or just a nested IF block?).

Stage 2 is significantly harder and probably needs source AST inspection (which RTIE has via `src/parsing/query_engine.py`), not just regex. **Recommend Stage 1 only for W83B v1 and accept that overgeneralization cases like B3 require a follow-up.**

`w81_suppressed=True` does NOT correlate uniquely with December fabrications — it's True on 15/16 responses. So it's not a useful gating signal for the detector.

### ESCAPE: D1 — `Trace N_NET_INTEREST_INCOME …` fabricated SQL + drifted anchor

**Diagnostic block**
```json
{ "w70_anchor": "INSIGNFCNT_INVST_DED_STD_ACCT_HEAD_DATA_POP",
  "w76_anchor": null,
  "w81_suppressed": true,
  "hierarchy_header_present": false }
```

**Failure surface:** Multiple problems in one response:
1. `"This entire function ONLY runs when the reporting month is December"` — December overgeneralization (the anchor function is not December-gated).
2. Fabricated SQL block: `SELECT N_NET_INTEREST_INCOME INTO some_variable FROM STG_OPS_RISK_DATA WHERE V_STD_ACCT_HEAD_ID = 'CAP170';` — not real source; the line citation `(Lines 54-349)` is the same three times for three different "steps", which is a citation-padding pattern.
3. Anchor drift: `w70_anchor = INSIGNFCNT_INVST_DED_STD_ACCT_HEAD_DATA_POP` is irrelevant to a NII→CAP170 trace. The cascade fell to semantic top-1 because `w76_anchor=null` and `bi_routing` didn't fire. **The retrieved set included real NII-bearing functions (OPS_RISK_DATA_POPULATION_CSTM, FN_LOAD_OPS_RISK_DATA) but the anchor cascade picked the wrong one.**

**W83B detector implication.** D1 reveals that W83B alone is insufficient. The fabricated SQL is a **separate failure class** that W82 was supposed to catch (`sequential-step fabrication`) — but in Run 9 the citations are line ranges, not function-to-function arrows, so W82 didn't fire. The detector landscape for trace queries needs at least:
- A December gate check (handles the December clause)
- A line-citation-padding check (same line cited as 3 different "steps")
- An anchor-vs-asked-function check (when the user names a specific column / pipeline, the anchor should land on a function that actually references it — w70_anchor + functions_analyzed gives the detector everything to verify this)

The **third check is now buildable directly from W84 signals:** if the body claims to trace `X` and `w70_anchor.function` doesn't appear in the body or doesn't contain `X` in its source, that's a routing-correctness escape distinct from a content escape. Worth scoping as W84-derived guard rather than rolling it into W83B.

### Recovery: A4 (Run 8 ESCAPE → Run 9 ALIGNED-NEGATIVE)

Same body content surface ("passes through unchanged"), but Run 9's W57 pass-through guard fired: `"GROUNDING-HIGH: response contains template phrase 'pass-through' but cited source for 'FN_LOAD_OPS_RISK_DATA' does not support it"`. That detector is the right shape for W83B to emulate — it's a source-side check anchored on `w70_anchor.function`.

### Recovery: C2 (Run 8 ESCAPE → Run 9 ALIGNED-NEGATIVE — different failure mode caught)

Run 9 C2 produced different prose without the December substitution paraphrase that Run 8 captured. Instead it produced a sequential-step fabrication that the W82 sequential-step check caught (`response presents 'CAP_CONSL_…' → 'FSI_STD_…' as sequential steps, but at least one is not in retrieved sources`). Run-over-run, the model finds a different way to be wrong, and a different detector catches it. The W83B detector should not assume the December form is the only way the model evades grounding.

---

## 6. Anything unexpected

1. **B3 was an unflagged Run 8 escape.** Reviewing Run 8 captures for the report, B3 had the exact same fabrication ("This entire function ONLY runs when the reporting month is December") and was scored VERIFIED both times. The Run 8 scoring focused on A2/A4/C2 because those were the user-cited cases; B3 slipped through the manual review. Worth noting that the true Run 8 escape count was 4, not 3.

2. **W81 is now near-universal (15/16).** This was the question Run 8 couldn't answer. Now answered: cross-process suppression fires on essentially every multi-function response. As a binary signal it's not useful for prioritizing W81 Option B. The Option B decision needs a *per-response* multi-process listing signal — how many processes contributed, what fraction of citations come from each — not just "did suppression fire."

3. **`w76_anchor` returned null on every question.** The benchmark battery uses no `In X, …` prefix queries, so the W76 M1 rule never fired. M2 alias-fallback didn't fire either. This is a structural property of the benchmark, not a regression — but it means w76_anchor exposed zero new signal this run. If we want to measure W76 routing on a future battery, add at least 2–3 prefix-style prompts.

4. **`w70_anchor` is the most useful W84 field by far.** On 12/16 questions it lets us verify the cascade landed on the asked-about function. On the 3 drift cases (C2, D1, D3) it pinpoints exactly where routing went wrong. This is the field W83B and a future "anchor-vs-asked" check should consume.

5. **`hierarchy_header_present` is False on every question.** That's downstream of W81 firing — the renderer suppresses the header when multi_source spans processes. Consistent with the W81 firing rate.

6. **3/16 drift between runs on identical code.** A4 and C2 recovered; D1 regressed. The model's prose varies enough between runs that surface-pattern detectors catch different cases each time. **Implication for future runs:** treat any single-question delta as within noise; only persistent escapes (A2, B3 — escape on both runs) are reliable W83B targets.

7. **A4 and B3 both anchor on FN_LOAD_OPS_RISK_DATA, both make whole-function December claims, but A4 gets caught (pass-through guard) and B3 doesn't.** The pass-through guard fires because A4's body contains "passes through" / "pass-through" template phrases. B3's body avoids those phrases and just makes the December claim with a CBA-branch-specific narration. Same anchor, same overgeneralization class, different detector coverage. W83B's December check would catch both.

---

## Headline for Toheed

- **Run 9 ESCAPEs:** A2 (same as Run 8 — the canonical W83B target), B3 (was already escaping in Run 8, missed in scoring), D1 (new in Run 9, also a fabricated-SQL + anchor-drift case).
- **Recovered:** A4 (pass-through guard fired), C2 (sequential-step catch fired).
- **W83B v1 priority:** stage-1 source-side December predicate, gated on `w70_anchor.function`. Handles A2 cleanly. Handles overgeneralization (B3) only partially; full B3 fix needs source AST inspection — defer to v2.
- **Separate from W83B:** an anchor-vs-asked-function check (D1 class) is its own work item — natural to build now that W84 exposes the anchor on the wire.
- **Drift floor:** ~19% (3/16 questions flipped). Use this as the noise threshold for future runs.

---

## Update (2026-05-12, post-W83B merge)

D1's anchor-vs-asked failure class addressed by **W85** (merge SHA pending). The check fires `GROUNDING-ANCHOR-MISMATCH-HIGH` when the W70 cascade anchor differs from the function the user explicitly named in their query. Gated so CAP-code (BI routing), column, and table queries don't false-positively trip it. Lives in [src/agents/logic_explainer.py](src/agents/logic_explainer.py) as `_w57_check_anchor_vs_asked_mismatch`.

Run 9 D1 ground truth: anchor was `INSIGNFCNT_INVST_DED_STD_ACCT_HEAD_DATA_POP`; query named no functions (only column `N_NET_INTEREST_INCOME` and tables). On the post-W85 backend D1 will continue to badge UNVERIFIED via other catches — W85 itself stays silent on D1 because there's no asked function to compare. The check is more useful for queries like Canary A from the W83B run (`How does CS_Goodwill_Calculation work?` with cascade landing on `CS_GOODWILL_NET_OF_DTL_CALCULATION`), where the user names a real function and the cascade picks a sibling.
