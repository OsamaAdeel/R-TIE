# Run 8 report (placeholder)

Run 8 (2026-05-11) was executed before W84 landed. The raw captures
(`q*.json`), summary (`_summary.txt`), and scoring data
(`_scoring_data.json`) in this directory are the canonical artifacts;
no formal report was written at the time.

---

## Reclassification (added 2026-05-12 during W83B prep)

While preparing Run 9, B3 (`FN_LOAD_OPS_RISK_DATA`, CBA branch
question) was reviewed against ground truth and found to have been
escaping in Run 8 the same way it escaped in Run 9:

> Body claim: "This entire function ONLY runs when the reporting
> month is December." Source has a *localized* `EXTRACT(MONTH …) =
> 12` conditional, not whole-function gating. Body overgeneralizes.

The original Run 8 scoring marked B3 ALIGNED (badged VERIFIED, no
GROUNDING-HIGH warnings, treated as a clean pass). On review, B3 is
an ESCAPE of the overgeneralization class — same shape as the W83C
weakness logged in `docs/RTIE_Weakness_Log.md`.

**True Run 8 ESCAPE count: 4/16 (25%)**, not the originally-scored
3/16 (19%). The four are A2, A4, B3, C2.

The Run 9 drift floor of 3/16 (~19%) stands — that's the run-over-
run flip rate on identical backend code, independent of the
reclassification.

Source: `scratch/run9/run9_report.md` Section 4 + Section 6.

---

## Pointer to richer Run 9 artifacts

For full per-question breakdowns of trust contract, anchor signal,
retrieval signal, pattern fires, and the W83B diagnostic analysis,
see `scratch/run9/run9_report.md`. Run 9 used the W84 diagnostic
block to surface anchor state directly from the SSE response, which
Run 8 could not.
